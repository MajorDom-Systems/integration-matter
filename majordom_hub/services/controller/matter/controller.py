import asyncio
import enum
import inspect
import logging
from dataclasses import fields, is_dataclass
from typing import Type, get_args, get_origin, get_type_hints, override
from uuid import UUID

from aiohttp import ClientSession
from chip.clusters.CHIPClusters import ChipClusters
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Objects import Identify
from matter_server.client import MatterClient
from matter_server.client.models.node import MatterNode
from matter_server.common.models import CommissionableNodeData, EventType
from matter_server.common.errors import UnknownError

from majordom_hub.config import matter_server_url
from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.base import NonEmptyStr
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import CredentialsType, CredentialsValue, Discovery
from majordom_hub.schemas.parameter import (
    Parameter,
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)
from majordom_hub.services.controller.framework.abstract_controller import AbstractController

from .exceptions import MatterConnectionError, MatterUnexpectedError, MatterUnsupportedParameter, MatterNotFoundParameter
from .mapper import MatterMapper
from .matter_spec import (
    ATTRIBUTE_MIN_STEPS,
    ATTRIBUTE_UNITS,
    MAIN_PARAMETER_BY_CLUSTER,
    SYSTEM_ATTRIBUTES,
    SYSTEM_CLUSTERS,
)
from .model import (
    MatterDevice,
    MatterDeviceIntegrationData,
    MatterDeviceState,
    MatterParameter,
    MatterParameterIntegrationData,
    MatterParameterState,
    MatterParameterTypeEnum,
)


class MatterController(AbstractController):
    _matter_client: MatterClient
    _matter_client_session: ClientSession
    _majordom_descoveries: dict[UUID, Discovery] = dict()
    _mapper = MatterMapper()

    # -------------------------------------------------------------------------
    # AbstractController interface
    # -------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Matter"

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self._majordom_descoveries

    @property
    @override
    def device_type(self) -> Type[MatterDevice]:
        return MatterDevice

    @property
    @override
    def parameter_type(self) -> Type[MatterParameter]:
        return MatterParameter

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self):
        self._matter_client_session = ClientSession()
        self._matter_client = MatterClient(matter_server_url, self._matter_client_session)
        await self._matter_client.connect()

        init_ready = asyncio.Event()
        asyncio.create_task(self._matter_client.start_listening(init_ready=init_ready))
        await init_ready.wait()

        asyncio.create_task(self._matter_discovery_loop())

        device_nodes: list[int] = []
        async with self.dependencies.make_device_repository() as device_repository:
            for device in await device_repository.get_all(self.name, MatterDevice):
                if node := self._matter_client.get_node(device.integration_data.node_id):
                    self._subscription(device.id, node)
                    device_nodes.append(device.integration_data.node_id)
                else:
                    device.available = False
                    device.last_error = f"Device {device.name} is no longer connected to the Matter network"
                    await device_repository.save(device, device.id)

            for node in self._matter_client.get_nodes():
                if node.node_id in device_nodes:
                    continue
                discovery_id = self._mapper.matter_id_to_uuid(
                    f"{node.node_id}_{node.device_info.productName}"
                )
                discovery = Discovery(
                    id=discovery_id,
                    integration=NonEmptyStr(self.name),
                    credentials=CredentialsType.none,
                    expiration=None,
                    transport=NonEmptyStr("IP"),
                    device_name=NonEmptyStr(node.device_info.productName or "Unknown"),
                    device_manufacturer=node.device_info.vendorName or str(node.device_info.vendorID),
                    device_category=None,
                    device_icon=None,
                )
                self._majordom_descoveries[discovery_id] = discovery
                await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    async def stop(self):
        self._majordom_descoveries.clear()
        if not self._matter_client_session or not self._matter_client:
            return
        await self._matter_client.disconnect()
        await self._matter_client_session.close()

    # -------------------------------------------------------------------------
    # Public device operations
    # -------------------------------------------------------------------------

    async def pair_device(self, discovery: Discovery, credentials: CredentialsValue | None):
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")

        if discovery.credentials is CredentialsType.qr:
            commission_node = await self._matter_client.commission_with_code(str(credentials))
        elif discovery.credentials is CredentialsType.code:
            commission_node = await self._matter_client.commission_on_network(int(credentials))
        else:
            raise MatterUnexpectedError("This credentials type is not supported")

        self._majordom_descoveries.pop(discovery.id)
        node = self._matter_client.get_node(commission_node.node_id)
        device_id = self._mapper.matter_id_to_uuid(f"{node.node_id}_{node.device_info.productName}")

        async with self.dependencies.make_device_repository() as device_repository:
            device = await device_repository.state(discovery.id, MatterDeviceState)
            assert device

            if not device.integration_data:
                device.integration_data = MatterDeviceIntegrationData(node_id=node.node_id)

            for endpoint_id, endpoint in node.endpoints.items():
                for cluster_id, cluster in endpoint.clusters.items():
                    if hasattr(cluster, "Commands"):
                        for parameter in self._parse_commands(device_id, endpoint_id, cluster_id, cluster, node):
                            device.parameters.append(MatterParameterState(**parameter.__dict__, value=b""))

                    if hasattr(cluster, "Attributes"):
                        for parameter in self._parse_attributes(device_id, endpoint_id, cluster_id, cluster, endpoint, node):
                            value = node.get_attribute_value(
                                endpoint_id, cluster_id,
                                parameter.integration_data.attribute_id,
                            )
                            device.parameters.append(MatterParameterState(**parameter.__dict__, value=value))

            device.main_parameter = self._get_main_parameter(device.id, node)
            await device_repository.save(device, discovery.id)
            await device_repository.update_id(discovery.id, device_id)

        await self.dependencies.output.controller_did_connect_device(self, device_id)
        self._subscription(device_id, node)
        return device_id

    async def unpair(self, device: MatterDevice):
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")
        await self._matter_client.remove_node(device.node_id)

    async def identify(self, device: MatterDevice):
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")
        command = Identify.Commands.Identify()
        node = self._matter_client.get_node(device.node_id)
        for endpoint_id in node.endpoints.keys():
            if node.has_cluster(3, endpoint_id):
                await self._matter_client.send_device_command(device.node_id, endpoint_id, command)

    async def fetch(self, device: MatterDevice):
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")

        node = self._matter_client.get_node(device.node_id)
        if not node:
            raise MatterUnexpectedError(f"Node for device {device.node_id} not found")

        events = []
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if not hasattr(cluster, "Attributes"):
                    continue
                for _, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                    if not issubclass(attribute, ClusterAttributeDescriptor):
                        continue
                    attribute_id = getattr(attribute, "attribute_id", -1)
                    value = node.get_attribute_value(endpoint_id, cluster_id, attribute_id)
                    parameter_id = self._mapper.matter_id_to_uuid(
                        f"{device.id}_attribute_{endpoint_id}/{cluster_id}/{attribute_id}"
                    )
                    events.append(DeviceParameterChangedEvent(
                        device_id=device.id,
                        parameter_id=parameter_id,
                        value=value if isinstance(value, str | int | bool) else None,
                    ))

        await self.dependencies.output.controller_did_receive_device_events(self, events)

    async def send_command(self, command: DeviceCommand, device: MatterDevice, parameter: MatterParameter):
        try:
            if not self._matter_client:
                raise MatterConnectionError("Matter client is not started")

            node = self._matter_client.get_node(device.node_id)
            if not node:
                raise MatterUnexpectedError(f"Node for device {device.node_id} not found")

            endpoint_id = parameter.integration_data.endpoint_id
            endpoint = node.endpoints.get(endpoint_id)
            if not endpoint:
                raise MatterUnexpectedError(f"Endpoint {endpoint_id} not found on node {node.node_id}")

            cluster_id = parameter.integration_data.cluster_id
            cluster = endpoint.clusters.get(cluster_id)
            if not cluster:
                raise MatterUnexpectedError(f"Cluster {cluster_id} not found on endpoint {endpoint_id}")

            if parameter.integration_data.type is MatterParameterTypeEnum.command:
                await self._execute_cluster_command(node, endpoint_id, cluster, parameter, command)
            elif parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                await self._write_cluster_attribute(node, endpoint_id, cluster_id, parameter, command)
        except UnknownError as e:
            error = str(e)

            error_map = {
                "0x8b": (MatterNotFoundParameter, "not found on device"),
                "0x81": (MatterUnsupportedParameter, "not supported by device"),
            }

            match = next(
                ((exc, msg) for code, (exc, msg) in error_map.items() if code in error),
                None
            )
            if match is None:
                # Unknown/unmapped Matter error (e.g. Busy 0x9c) — don't swallow it
                logging.error(f"Command '{parameter.name}' failed with unmapped error: {error}")
                raise

            exception_class, message = match
            async with self.dependencies.make_device_repository() as device_repository:
                device_state = await device_repository.state(device.id, MatterDeviceState)
                device_state.parameters = [p for p in device_state.parameters if p.id != parameter.id]
                device.integration_data.black_list.append(parameter.id)
                await device_repository.save(device, device.id)

            logging.error(f"Command '{parameter.name}' is {message} and will be removed")
            raise exception_class(f"Command '{parameter.name}' failed: {message}")

    # -------------------------------------------------------------------------
    # Private: command execution and attribute writing
    # -------------------------------------------------------------------------

    async def _execute_cluster_command(
        self, node, endpoint_id: int, cluster, parameter: MatterParameter, command: DeviceCommand
    ):
        command_id = parameter.integration_data.command_id
        time_requested_timeout = None
        if command_id is None or command_id < 0:
            raise MatterUnexpectedError(f"Invalid command_id: {command_id}")

        if not hasattr(cluster, "Commands"):
            return

        for _, cmd_class in inspect.getmembers(cluster.Commands, inspect.isclass):
            if not issubclass(cmd_class, ClusterCommand):
                continue
            if getattr(cmd_class, "command_id", -1) != command_id:
                continue

            # Only send client-side commands
            if not getattr(cmd_class, "is_client", True):
                continue

            if cluster.id == 0x00000101:  # DoorLock
                time_requested_timeout=1000
            if isinstance(command.value, dict):
                data = self._mapper.parse_data_for_command(cmd_class, command.value)
                await self._matter_client.send_device_command(node.node_id, endpoint_id, cmd_class(**data), timed_request_timeout_ms=time_requested_timeout)
            else:
                await self._matter_client.send_device_command(node.node_id, endpoint_id, cmd_class(), timed_request_timeout_ms=time_requested_timeout)
            return

    async def _write_cluster_attribute(
        self, node, endpoint_id: int, cluster_id: int, parameter: MatterParameter, command: DeviceCommand
    ):
        if parameter.role != ParameterRole.control:
            raise MatterUnexpectedError(f"Parameter '{parameter.name}' is not writable")

        attribute_path = f"{endpoint_id}/{cluster_id}/{parameter.integration_data.attribute_id}"
        if attribute_path in node.node_data.attributes:
            await self._matter_client.write_attribute(node.node_id, attribute_path, command.value)

    # -------------------------------------------------------------------------
    # Private: node parsing
    # -------------------------------------------------------------------------

    def _parse_commands(
        self, device_id: UUID, endpoint_id: int, cluster_id: int, cluster, node: MatterNode
    ) -> list[MatterParameter]:
        params = []
        visibility = ParameterVisibility.system if cluster_id in SYSTEM_CLUSTERS else ParameterVisibility.user

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
                        elif issubclass(field_type, bool):
                            data_type = ParameterDataType.bool
                        elif issubclass(field_type, int):
                            data_type = ParameterDataType.integer
                        elif issubclass(field_type, float):
                            data_type = ParameterDataType.decimal
                        elif issubclass(field_type, str):
                            data_type = ParameterDataType.string

                    args.append(Parameter(
                        id=self._mapper.matter_id_to_uuid(
                            f"{device_id}_field_{endpoint_id}/{cluster_id}/{command_id}/{field.name}"
                        ),
                        name=field.name,
                        data_type=data_type,
                        unit=ParameterUnit.plain,
                        role=ParameterRole.control,
                        valid_values=valid_values,
                        visibility=ParameterVisibility.setting,
                        integration_data=None,
                    ))

            params.append(MatterParameter(
                id=self._mapper.matter_id_to_uuid(f"{device_id}_command_{endpoint_id}/{cluster_id}/{command_id}"),
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
            ))

        return params

    def _parse_attributes(
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

            value = endpoint.get_attribute_value(cluster_id, attribute_id)

            visibility = ParameterVisibility.system
            role = ParameterRole.sensor
            if cluster_id not in SYSTEM_CLUSTERS:
                sdk_cluster = ChipClusters(None).GetClusterInfoById(cluster_id)
                sdk_attribute = sdk_cluster.get("attributes", {}).get(attribute_id, {})
                if sdk_attribute.get("writable"):
                    visibility = ParameterVisibility.setting
                    if cluster_id == 0x00000202:  # FanControl
                        visibility = ParameterVisibility.user
                    role = ParameterRole.control
                else:
                    visibility = ParameterVisibility.user

            valid_values = None
            attr_type = attribute.attribute_type
            if attr_type and hasattr(attr_type, "Type") and isinstance(attr_type.Type, type):
                t = attr_type.Type
                if issubclass(t, (enum.Enum, enum.Flag)):
                    # Keys are numeric values sent to device, values are display labels
                    valid_values = {m.value: m.name for m in t}

            min_value, max_value = self._mapper.get_min_max_value(attribute, value)

            params.append(MatterParameter(
                id=self._mapper.matter_id_to_uuid(
                    f"{device_id}_attribute_{endpoint_id}/{cluster_id}/{attribute_id}"
                ),
                name=name,
                data_type=self._mapper.get_parameter_data_type_from_value(value),
                visibility=visibility,
                min_value=min_value,
                max_value=max_value,
                valid_values=valid_values,
                min_step=ATTRIBUTE_MIN_STEPS.get((cluster_id, attribute_id)),
                unit=ATTRIBUTE_UNITS.get((cluster_id, attribute_id), ParameterUnit.plain),
                role=role,
                integration_data=MatterParameterIntegrationData(
                    endpoint_id=endpoint_id,
                    cluster_id=cluster_id,
                    attribute_id=attribute_id,
                    type=MatterParameterTypeEnum.attribute,
                ),
            ))

        return params

    def _get_main_parameter(self, device_id: UUID, node: MatterNode) -> UUID | None:
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, command_id in MAIN_PARAMETER_BY_CLUSTER:
                if cluster_id in endpoint.clusters:
                    return self._mapper.matter_id_to_uuid(
                        f"{device_id}_command_{endpoint_id}/{cluster_id}/{command_id}"
                    )
        return None

    # -------------------------------------------------------------------------
    # Private: discovery
    # -------------------------------------------------------------------------

    async def _matter_discovery_loop(self, interval: int = 5):
        while True:
            try:
                nodes: list[CommissionableNodeData] = (
                    await self._matter_client.discover_commissionable_nodes()
                )
                for node in nodes:
                    await self._async_matter_did_discover(node)
            except Exception as e:
                logging.error(f"[{self.name}] discovery error: {e}")
            await asyncio.sleep(interval)

    async def _async_matter_did_discover(self, node: CommissionableNodeData):
        discovery_id = self._mapper.matter_id_to_uuid(
            node.instance_name
            or f"{node.vendor_id}_{node.product_id}_{node.addresses[0] if node.addresses else 'unknown'}"
        )
        discovery = Discovery(
            id=discovery_id,
            integration=NonEmptyStr(self.name),
            credentials=self._mapper.define_credentials_type(
                node.commissioning_mode,
                node.pairing_hint,
                node.pairing_instruction,
            ),
            expiration=None,
            transport=NonEmptyStr("IP" if node.addresses else "BLE"),
            device_name=NonEmptyStr(node.device_name or node.instance_name or "Unknown"),
            device_manufacturer=f"Vendor {node.vendor_id}" if node.vendor_id else None,
            device_category=str(node.device_type),
            device_icon=None,
        )
        self._majordom_descoveries[discovery_id] = discovery
        await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    # -------------------------------------------------------------------------
    # Private: subscriptions
    # -------------------------------------------------------------------------

    def _subscription(self, device_id: UUID, node: MatterNode):
        """
        Registers attribute-change callbacks for every attribute on the node.
        Uses _make_callback to capture (device_id, parameter_id) by value —
        without it all callbacks would share the last loop iteration's values.
        """
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if not hasattr(cluster, "Attributes"):
                    continue
                for _, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                    if not issubclass(attribute, ClusterAttributeDescriptor):
                        continue
                    attribute_path = f"{endpoint_id}/{cluster_id}/{attribute.attribute_id}"
                    parameter_id = self._mapper.matter_id_to_uuid(f"{device_id}_attribute_{attribute_path}")
                    self._matter_client.subscribe_events(
                        self._make_callback(device_id, parameter_id),
                        EventType.ATTRIBUTE_UPDATED,
                        node.node_id,
                        attribute_path,
                    )

    def _make_callback(self, device_id: UUID, parameter_id: UUID):
        def callback(event_type, new_value):
            event = DeviceParameterChangedEvent(
                device_id=device_id,
                parameter_id=parameter_id,
                value=new_value,
            )
            asyncio.create_task(
                self.dependencies.output.controller_did_receive_device_events(self, [event])
            )
        return callback