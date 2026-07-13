import enum

from chip.clusters.Types import Nullable, NullValue
from chip.tlv import TLVReader
from dataclasses import fields, is_dataclass
from typing import Any, get_args, get_origin, get_type_hints
from uuid import NAMESPACE_DNS, UUID, uuid5

from majordom_hub.schemas.device import CredentialsType
from majordom_hub.schemas.parameter import ParameterDataType

from .matter_spec import MIN_MAX_VALUE


class MatterMapper:
    def matter_id_to_uuid(self, id: str) -> UUID:
        """Deterministically converts a Matter string identifier to a UUID."""
        return uuid5(NAMESPACE_DNS, id)

    def define_credentials_type(
        self,
        commissioning_mode: int | None = None,
        pairing_hint: int | None = None,
        pairing_instruction: str | None = None,
        is_on_network: bool = False,
    ) -> CredentialsType:
        if not commissioning_mode or commissioning_mode == 0:
            return CredentialsType.none

        hint = pairing_hint or 0
        instruction = (pairing_instruction or "").lower()

        # Bitmask checks based on the Matter spec pairing hint bitmap.
        # Each bit indicates a supported commissioning method.
        if is_on_network:
            return CredentialsType.code.with_mask("DDD-DD-DDD")
        if hint & (0x0004 | 0x0020 | 0x0008):
            return CredentialsType.qr
        if hint & (0x0002 | 0x0010) or "code" in instruction or "pin" in instruction:
            return CredentialsType.code.with_mask("DDD-DD-DDD")
        if hint & 0x0001:
            return CredentialsType.power_cycle

        return CredentialsType.none

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
                    result[field.name] = raw.encode('utf-8')
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
                return raw.encode('utf-8')
            if isinstance(raw, (list, tuple)):
                return bytes(raw)
            return bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw

        return raw