import enum
import inspect
import logging
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from typing import Any, get_args, get_origin, get_type_hints
from uuid import UUID

from chip.clusters.CHIPClusters import ChipClusters
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Types import Nullable, NullValue
from chip.tlv import TLVReader
from majordom_integration_sdk.schemas.device import CredentialsType
from majordom_integration_sdk.schemas.parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)
from matter_server.client.models.node import MatterNode

from .matter_spec import (
    ATTRIBUTE_MIN_STEPS,
    ATTRIBUTE_SCALE,
    ATTRIBUTE_UNITS,
    EVERYDAY_COMMANDS,
    FIELD_TYPE_TO_DATA_TYPE,
    METADATA_SOURCES,
    MIN_MAX_VALUE,
    SYSTEM_ATTRIBUTES,
    SYSTEM_CLUSTERS,
    AttributeKey,
    classify_attribute,
)
from .model import MatterParameter, MatterParameterIntegrationData, MatterParameterTypeEnum


class MatterMapper:
    def __init__(
        self,
        device_uuid: Callable[[str], UUID],
        parameter_uuid: Callable[[UUID, str], UUID],
    ):
        # The controller's framework UUID generators (see AbstractController). Every Matter id —
        # a device from its node/product, a discovery from its commissioning identity, a parameter
        # from its endpoint/cluster/attribute path — is derived through these, so parameters are
        # namespaced under the device and stay identical across pairing, fetch, and live reports.
        self._device_uuid = device_uuid
        self._parameter_uuid = parameter_uuid

    # -------------------------------------------------------------------------
    # Identity: Matter identifiers -> MajorDom UUIDs.
    # Single source of truth for the id string formats — keep every call site going through
    # these instead of formatting the f-strings inline.
    # -------------------------------------------------------------------------

    def discovery_uuid(self, matter_id: str) -> UUID:
        """UUID for a discovery, keyed on its (pre-commissioning) Matter identity string."""
        return self._device_uuid(matter_id)

    def device_uuid(self, node_id: int, product_name: str | None) -> UUID:
        return self._device_uuid(f"{node_id}_{product_name}")

    def attribute_parameter_uuid(self, device_id: UUID, endpoint_id: int, cluster_id: int, attribute_id: int) -> UUID:
        return self._parameter_uuid(device_id, f"attribute_{endpoint_id}/{cluster_id}/{attribute_id}")

    def command_parameter_uuid(self, device_id: UUID, endpoint_id: int, cluster_id: int, command_id: int) -> UUID:
        return self._parameter_uuid(device_id, f"command_{endpoint_id}/{cluster_id}/{command_id}")

    def command_field_uuid(
        self, device_id: UUID, endpoint_id: int, cluster_id: int, command_id: int, field_name: str
    ) -> UUID:
        return self._parameter_uuid(device_id, f"field_{endpoint_id}/{cluster_id}/{command_id}/{field_name}")

    def define_credentials_options(
        self,
        commissioning_mode: int | None = None,
        pairing_hint: int | None = None,
        pairing_instruction: str | None = None,
        is_on_network: bool = False,
    ) -> list[CredentialsType]:
        """Every credentials type this node's pairing hint bitmap says it supports —
        a device can advertise more than one simultaneously (e.g. QR and a manual code),
        so this returns all of them instead of picking just one."""
        if not commissioning_mode or commissioning_mode == 0:
            return [CredentialsType.none]

        hint = pairing_hint or 0
        instruction = (pairing_instruction or "").lower()
        options: list[CredentialsType] = []

        # Bitmask checks based on the Matter spec pairing hint bitmap.
        if is_on_network:
            options.append(CredentialsType.code.with_mask("DDD-DD-DDD"))
        if hint & (0x0004 | 0x0020 | 0x0008):
            options.append(CredentialsType.qr)
        if (
            hint & (0x0002 | 0x0010) or "code" in instruction or "pin" in instruction
        ) and CredentialsType.code not in options:
            options.append(CredentialsType.code.with_mask("DDD-DD-DDD"))
        # bit 0x0001 (Power Cycle Commissioning Mode) isn't representable as a
        # CredentialsType — there's no code/QR/secret majordom can collect for it —
        # so it doesn't contribute an option here.

        return options or [CredentialsType.none]

    def normalize_value(self, value: Any):
        if value is NullValue or isinstance(value, Nullable):
            return None
        return value

    def get_parameter_data_type_from_value(self, value: Any) -> ParameterDataType:
        """Infers ParameterDataType from a Python runtime value."""
        # Matter SDK uses a special sentinel for "no value", not Python None
        try:
            if value is NullValue or isinstance(value, Nullable):
                value = None
                return ParameterDataType.none
        except ImportError:
            pass

        if value is None:
            return ParameterDataType.none

        # bool must be checked before int because bool is a subclass of int.
        if isinstance(value, bool):
            return ParameterDataType.bool

        if isinstance(value, enum.Enum):
            return ParameterDataType.enum

        if isinstance(value, int):
            return ParameterDataType.integer

        if isinstance(value, float):
            return ParameterDataType.decimal

        if isinstance(value, str):
            return ParameterDataType.string

        if isinstance(value, (bytes, bytearray, memoryview)):
            return ParameterDataType.data

        if is_dataclass(value) or isinstance(value, dict):
            return ParameterDataType.struct

        if isinstance(value, (list, tuple, set)):
            return ParameterDataType.struct

        raise ValueError(
            f"Cannot infer ParameterDataType for value of type {type(value)!r}: {value!r}. "
            f"Extend get_parameter_data_type_from_value to handle this type explicitly."
        )

    def apply_attribute_scale(self, cluster_id: int, attribute_id: int, value: Any) -> Any:
        """Converts a raw Matter attribute value into ATTRIBUTE_UNITS' base unit, per ATTRIBUTE_SCALE.
        Unwraps the cumulative-energy struct's `energy` field before scaling, since that's the
        only scaled attribute whose SDK value isn't already a plain number."""
        scale = ATTRIBUTE_SCALE.get(AttributeKey(cluster_id, attribute_id))
        if scale is None:
            return value
        if is_dataclass(value) and hasattr(value, "energy"):
            value = value.energy
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value * scale
        return value

    def resolve_runtime_bounds(
        self, key, cluster_id: int, endpoint, name: str, default_min, default_max
    ) -> tuple[Any, Any]:
        """Metadata priority 1: override a parameter's min/max with the device's own limit
        attributes' runtime VALUES (the *_min/*_max we hide as metadata). Falls back to the passed
        defaults (spec/type) when the device doesn't report a source attribute, warning so a quirk
        or a new device that omits an expected limit shows up in the logs."""
        source = METADATA_SOURCES.get(key)
        if source is None:
            return default_min, default_max
        min_v, max_v = default_min, default_max
        for attr_id, is_min in ((source.min_attr, True), (source.max_attr, False)):
            if attr_id is None:
                continue
            raw = endpoint.get_attribute_value(cluster_id, attr_id)
            resolved = self.normalize_value(raw)
            if resolved is not None:
                if is_min:
                    min_v = resolved
                else:
                    max_v = resolved
            else:
                logging.warning(
                    f"Metadata source cluster {cluster_id:#x} attr {attr_id:#x} "
                    f"({'min' if is_min else 'max'} for {name!r}) not reported — quirk or unsupported; "
                    f"using spec/type default"
                )
        return min_v, max_v

    def get_min_max_value(self, attribute, value) -> tuple[int | None, int | None]:
        """
        Encodes the attribute value to TLV to discover its wire type,
        then looks up the valid numeric range for that type from matter_spec.
        Returns (None, None) if the type is unknown or encoding fails.
        """
        try:
            tlv_bytes = attribute.ToTLV(None, value)
            reader = TLVReader(tlv_bytes)
            reader.get()
            tlv_type = reader.decoding[0]["type"]
            return MIN_MAX_VALUE.get(tlv_type, (None, None))
        except Exception:
            return None, None

    def parse_data_for_command(self, cmd_class: type, data: dict) -> dict:
        import enum

        if not is_dataclass(cmd_class):
            raise TypeError(f"Expected a dataclass cluster-command class, got {cmd_class!r}")
        hints = get_type_hints(cmd_class)
        result = {}
        for field in fields(cmd_class):
            if field.name not in data:
                continue
            raw = data[field.name]
            field_type = hints.get(field.name, field.type)

            # Unwrap Optional / Union
            if get_origin(field_type):
                args = [a for a in get_args(field_type) if a is not type(None)]
                field_type = args[0] if args else field_type

            if raw is None:
                result[field.name] = NullValue
            elif isinstance(field_type, type) and issubclass(field_type, enum.Enum):
                result[field.name] = field_type(int(raw))
            elif field_type in (bytes, bytearray):
                if isinstance(raw, str):
                    result[field.name] = raw.encode("utf-8")
                elif isinstance(raw, (list, tuple)):
                    result[field.name] = bytes(raw)
                else:
                    result[field.name] = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw
            else:
                result[field.name] = raw

        return result

    def parse_data_for_attribute(self, attribute_cls: type, raw: Any) -> Any:
        if raw is None:
            return NullValue

        attr_type = getattr(attribute_cls, "attribute_type", None)
        field_type = getattr(attr_type, "Type", None) if attr_type else None

        # Unwrap Optional[X] / Union[X, None] — same as in parse_data_for_command
        if get_origin(field_type):
            args = [a for a in get_args(field_type) if a is not type(None)]
            field_type = args[0] if args else field_type

        if isinstance(field_type, type) and issubclass(field_type, enum.Enum):
            return field_type(int(raw))

        if field_type in (bytes, bytearray):
            if isinstance(raw, str):
                return raw.encode("utf-8")
            if isinstance(raw, (list, tuple)):
                return bytes(raw)
            return bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw

        return raw

    # -------------------------------------------------------------------------
    # Node parsing: matter SDK cluster data -> MatterParameter
    # -------------------------------------------------------------------------

    def parse_commands(
        self, device_id: UUID, endpoint_id: int, cluster_id: int, cluster, node: MatterNode
    ) -> list[MatterParameter]:
        params = []
        on_system_cluster = cluster_id in SYSTEM_CLUSTERS

        # Only process commands actually supported by this device
        accepted_command_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFF9) or []

        for name, command in inspect.getmembers(cluster.Commands, inspect.isclass):
            if not issubclass(command, ClusterCommand):
                continue

            # Skip response commands (server→client) — only keep client→server commands
            if not getattr(command, "is_client", True):
                continue

            command_id = getattr(command, "command_id", -1)

            # Skip commands not supported by this specific device
            if accepted_command_ids and command_id not in accepted_command_ids:
                continue

            # Command visibility: system-cluster commands hidden; everyday one-tap actions ->
            # user; every other command (schedule/credential/log management) -> setting.
            if on_system_cluster:
                visibility = ParameterVisibility.system
            elif (cluster_id, command_id) in EVERYDAY_COMMANDS:
                visibility = ParameterVisibility.user
            else:
                visibility = ParameterVisibility.setting

            # Reflect on dataclass fields to build typed argument descriptors
            args = []
            if is_dataclass(command):
                command_types = get_type_hints(command)
                for field in fields(command):
                    if field.name.startswith("_"):
                        continue

                    field_type = command_types.get(field.name, field.type)
                    data_type = ParameterDataType.none
                    valid_values = None

                    # Unwrap Optional[X] / Union[X, None] → take the first concrete type
                    if get_origin(field_type):
                        args_ = [a for a in get_args(field_type) if a is not type(None)]
                        field_type = args_[0] if args_ else field_type

                    if isinstance(field_type, type):
                        if issubclass(field_type, enum.Enum):
                            data_type = ParameterDataType.enum
                            # Keys are numeric values sent to device, values are display labels
                            valid_values = {m.value: m.name for m in field_type if "unknown" not in m.name.lower()}
                        else:
                            # Exact match first, then subclass fallback (e.g. custom int wrappers)
                            matched = FIELD_TYPE_TO_DATA_TYPE.get(field_type)
                            if matched is None:
                                for candidate_type, mapped_type in FIELD_TYPE_TO_DATA_TYPE.items():
                                    if issubclass(field_type, candidate_type):
                                        matched = mapped_type
                                        break
                            if matched is not None:
                                data_type = matched

                    args.append(
                        Parameter(
                            id=self.command_field_uuid(device_id, endpoint_id, cluster_id, command_id, field.name),
                            name=field.name,
                            data_type=data_type,
                            unit=ParameterUnit.plain,
                            role=ParameterRole.control,
                            valid_values=valid_values,
                            visibility=ParameterVisibility.setting,
                            integration_data=None,
                        )
                    )

            params.append(
                MatterParameter(
                    id=self.command_parameter_uuid(device_id, endpoint_id, cluster_id, command_id),
                    name=name,
                    data_type=ParameterDataType.none,
                    role=ParameterRole.control,
                    visibility=visibility,
                    fields=args or None,
                    integration_data=MatterParameterIntegrationData(
                        endpoint_id=endpoint_id,
                        cluster_id=cluster_id,
                        command_id=command_id,
                        type=MatterParameterTypeEnum.command,
                    ),
                )
            )

        return params

    def parse_attributes(
        self, device_id: UUID, endpoint_id: int, cluster_id: int, cluster, endpoint, node: MatterNode
    ) -> list[MatterParameter]:
        params = []

        # Only process attributes actually present on this device
        supported_attribute_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFFB) or []

        for name, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
            if not issubclass(attribute, ClusterAttributeDescriptor):
                continue

            attribute_id = getattr(attribute, "attribute_id", -1)

            # Skip system-level attributes (featureMap, clusterRevision, etc.)
            if attribute_id in SYSTEM_ATTRIBUTES:
                continue

            # Skip attributes not present on this specific device
            if supported_attribute_ids and attribute_id not in supported_attribute_ids:
                continue

            raw_value = endpoint.get_attribute_value(cluster_id, attribute_id)

            # Visibility (see the parameter-ux recipe in the docs). System clusters and
            # security material are always hidden. Otherwise: writable attrs are configure-once
            # `setting`s unless curated as everyday controls; read-only attrs are hidden `system`
            # unless curated as live readings (USER_READINGS). This inverts the old
            # "every read-only -> user" flood — uncurated read-onlys (bounds, capabilities, counts)
            # stay hidden and double as metadata sources; the user can still surface any of them.
            key = AttributeKey(cluster_id, attribute_id)
            writable = bool(
                ChipClusters(None)
                .GetClusterInfoById(cluster_id)
                .get("attributes", {})
                .get(attribute_id, {})
                .get("writable")
            )
            # Classification via the priority ladder (see the matter README): safety (system
            # cluster / sensitive crypto) > our hand overrides > harvested HA judgment > fallback
            # (writable -> setting, else system), which WARNS on uncurated attributes.
            spec, source = classify_attribute(
                cluster_id,
                attribute_id,
                name,
                writable=writable,
                in_system_cluster=cluster_id in SYSTEM_CLUSTERS,
            )
            visibility = spec.visibility
            role = spec.role if spec.role is not None else (ParameterRole.control if writable else ParameterRole.sensor)
            if source.startswith("fallback"):
                logging.warning(
                    "Uncurated attribute cluster %#x attr %#x (%s) -> %s (%s); add to OUR_ATTRIBUTE_UX "
                    "or refresh the matter-HA harvest",
                    cluster_id,
                    attribute_id,
                    name,
                    visibility.value,
                    source,
                )

            valid_values = None
            attr_type = attribute.attribute_type
            if attr_type and hasattr(attr_type, "Type") and isinstance(attr_type.Type, type):
                t = attr_type.Type
                if issubclass(t, (enum.Enum, enum.Flag)):
                    # Keys are numeric values sent to device, values are display labels
                    valid_values = {m.value: m.name for m in t}

            min_value, max_value = self.get_min_max_value(attribute, raw_value)  # priority 3: wire-type range
            min_value, max_value = self.resolve_runtime_bounds(  # priority 1: device's own limit attrs
                key, cluster_id, endpoint, name, min_value, max_value
            )
            scale = ATTRIBUTE_SCALE.get(AttributeKey(cluster_id, attribute_id))
            if scale is not None:
                min_value = min_value * scale if min_value is not None else None
                max_value = max_value * scale if max_value is not None else None
            value = self.apply_attribute_scale(cluster_id, attribute_id, raw_value)

            try:
                data_type = self.get_parameter_data_type_from_value(self.normalize_value(value))
            except ValueError:
                # Value type isn't one we know how to represent yet. Skip just this attribute
                # rather than aborting the whole pairing — the device stays usable, only this
                # one parameter is unavailable.
                logging.warning(
                    f"Could not infer data type for {name} "
                    f"(cluster {cluster_id:#x}, attribute {attribute_id:#x}); skipping parameter"
                )
                continue

            params.append(
                MatterParameter(
                    id=self.attribute_parameter_uuid(device_id, endpoint_id, cluster_id, attribute_id),
                    name=name,
                    data_type=data_type,
                    visibility=visibility,
                    min_value=min_value,
                    max_value=max_value,
                    valid_values=valid_values,
                    min_step=ATTRIBUTE_MIN_STEPS.get(AttributeKey(cluster_id, attribute_id)),
                    # Our spec table wins; the harvested HA unit fills gaps it leaves plain.
                    unit=ATTRIBUTE_UNITS.get(key, spec.unit or ParameterUnit.plain),
                    role=role,
                    integration_data=MatterParameterIntegrationData(
                        endpoint_id=endpoint_id,
                        cluster_id=cluster_id,
                        attribute_id=attribute_id,
                        type=MatterParameterTypeEnum.attribute,
                    ),
                )
            )

        return params
