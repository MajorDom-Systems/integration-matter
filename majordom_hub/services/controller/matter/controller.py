import asyncio
import contextlib
import inspect
import logging
from typing import Type, override
from uuid import UUID

from aiohttp import ClientSession
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
from majordom_hub.schemas.device import CredentialsType, Discovery, ProvidedCredentials
from majordom_hub.schemas.parameter import ParameterRole
from majordom_hub.services.controller.framework.abstract_controller import AbstractController

from .exceptions import MatterConnectionError, MatterUnexpectedError, MatterUnsupportedParameter, MatterNotFoundParameter
from .mapper import MatterMapper
from .matter_spec import MAIN_PARAMETER_BY_CLUSTER, DefaultParams
from .model import (
    MatterDevice,
    MatterDeviceIntegrationData,
    MatterDeviceState,
    MatterParameter,
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
        self._background_tasks: list[asyncio.Task] = []
        self._node_availability: dict[UUID, bool] = {}
        self._matter_client_session = ClientSession()
        self._matter_client = MatterClient(matter_server_url, self._matter_client_session)
        await self._matter_client.connect()

        init_ready = asyncio.Event()
        self._background_tasks.append(asyncio.create_task(self._matter_client.start_listening(init_ready=init_ready)))
        await init_ready.wait()

        self._background_tasks.append(asyncio.create_task(self._matter_discovery_loop()))

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
                discovery_id = self._mapper.device_uuid(node.node_id, node.device_info.productName)
                discovery = Discovery(
                    id=discovery_id,
                    integration=NonEmptyStr(self.name),
                    expected_credentials_options=[CredentialsType.none],
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

        # Cancel the discovery loop and listener started in start() — otherwise they keep
        # running against a matter client/event loop that stop() is about to tear down,
        # which surfaces as "Future attached to a different loop" once the next test/run
        # starts a fresh event loop.
        for task in getattr(self, "_background_tasks", []):
            task.cancel()
        for task in getattr(self, "_background_tasks", []):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background_tasks = []

        if not self._matter_client_session or not self._matter_client:
            return
        await self._matter_client.disconnect()
        await self._matter_client_session.close()

    # -------------------------------------------------------------------------
    # Private: guards
    # -------------------------------------------------------------------------

    def _require_matter_client(self) -> MatterClient:
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")
        return self._matter_client

    def _require_node(self, device: MatterDevice) -> MatterNode:
        node = self._require_matter_client().get_node(device.node_id)
        if not node:
            raise MatterUnexpectedError(f"Node for device {device.node_id} not found")
        return node

    # -------------------------------------------------------------------------
    # Public device operations
    # -------------------------------------------------------------------------

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None):
        self._require_matter_client()

        if not credentials or credentials.type not in discovery.expected_credentials_options:
            raise MatterUnexpectedError(
                f"Credentials type {credentials.type if credentials else None!r} is not one of the "
                f"types this discovery advertised: {discovery.expected_credentials_options}"
            )

        if credentials.type is CredentialsType.qr:
            commission_node = await self._matter_client.commission_with_code(str(credentials.value))
        elif credentials.type is CredentialsType.code:
            if discovery.transport == "BLE":
                # commission_on_network only works for devices already reachable over IP
                # (matter-server's own docstring: "for advanced usecases only, use
                # commission_with_code for regular commissioning") — a BLE-discovered
                # device has no IP yet, so it must go through commission_with_code instead,
                # which chip-tool routes over BLE automatically based on the code.
                commission_node = await self._matter_client.commission_with_code(str(credentials.value))
            else:
                commission_node = await self._matter_client.commission_on_network(int(credentials.value))
        else:
            raise MatterUnexpectedError("This credentials type is not supported")

        self._majordom_descoveries.pop(discovery.id)
        node = self._matter_client.get_node(commission_node.node_id)
        device_id = self._mapper.device_uuid(node.node_id, node.device_info.productName)

        async with self.dependencies.make_device_repository() as device_repository:
            device = await device_repository.state(discovery.id, MatterDeviceState)
            assert device
            device.id = device_id

            if not device.integration_data:
                device.integration_data = MatterDeviceIntegrationData(node_id=node.node_id)

            for endpoint_id, endpoint in node.endpoints.items():
                for cluster_id, cluster in endpoint.clusters.items():
                    if hasattr(cluster, "Commands"):
                        for parameter in self._mapper.parse_commands(device_id, endpoint_id, cluster_id, cluster, node):
                            device.parameters.append(MatterParameterState(**parameter.__dict__, value=b""))

                    if hasattr(cluster, "Attributes"):
                        for parameter in self._mapper.parse_attributes(device_id, endpoint_id, cluster_id, cluster, endpoint, node):
                            value = node.get_attribute_value(
                                endpoint_id, cluster_id,
                                parameter.integration_data.attribute_id,
                            )
                            value = self._mapper.apply_attribute_scale(cluster_id, parameter.integration_data.attribute_id, value)
                            device.parameters.append(MatterParameterState(**parameter.__dict__).with_value(self._mapper.normalize_value(value)))

            main_parameter_id, default_value = self._get_main_parameter(device.id, node)
            device.main_parameter = main_parameter_id
            if main_parameter_id and default_value is not None:
                main_parameter = next((p for p in device.parameters if p.id == main_parameter_id), None)
                if main_parameter is None:
                    device.main_parameter = None
                elif main_parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                    main_parameter.with_default_value(self._mapper.normalize_value(default_value))
                else:
                    main_parameter.integration_data.default_arguments = default_value

            await device_repository.save(device, discovery.id)

        await self.dependencies.output.controller_did_connect_device(self, device_id)
        self._subscription(device_id, node)
        return device_id

    async def unpair(self, device: MatterDevice):
        await self._require_matter_client().remove_node(device.node_id)

    async def identify(self, device: MatterDevice):
        command = Identify.Commands.Identify()
        node = self._require_node(device)
        for endpoint_id in node.endpoints.keys():
            if node.has_cluster(3, endpoint_id):
                await self._matter_client.send_device_command(device.node_id, endpoint_id, command)

    async def fetch(self, device: MatterDevice):
        node = self._require_node(device)

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
                    value = self._mapper.apply_attribute_scale(cluster_id, attribute_id, value)
                    parameter_id = self._mapper.attribute_parameter_uuid(device.id, endpoint_id, cluster_id, attribute_id)
                    events.append(DeviceParameterChangedEvent(
                        device_id=device.id,
                        parameter_id=parameter_id,
                        value=value if isinstance(value, str | int | float | bool) else None,
                    ))

        await self.dependencies.output.controller_did_receive_device_events(self, events)

    async def send_command(self, command: DeviceCommand, device: MatterDevice, parameter: MatterParameter):
        try:
            node = self._require_node(device)

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
        if attribute_path not in node.node_data.attributes:
            return
    
        endpoint = node.endpoints[endpoint_id]
        cluster = endpoint.clusters[cluster_id]
        attribute_cls = next(
            (a for _, a in inspect.getmembers(cluster.Attributes, inspect.isclass)
             if issubclass(a, ClusterAttributeDescriptor)
             and getattr(a, "attribute_id", -1) == parameter.integration_data.attribute_id),
            None
        )
    
        value = self._mapper.parse_data_for_attribute(attribute_cls, command.value) if attribute_cls else command.value
        await self._matter_client.write_attribute(node.node_id, attribute_path, value)

    # -------------------------------------------------------------------------
    # Private: node parsing
    # -------------------------------------------------------------------------

    def _get_main_parameter(self, device_id: UUID, node: MatterNode) -> tuple[UUID | None, DefaultParams]:
        for endpoint_id, endpoint in node.endpoints.items():
            for spec in MAIN_PARAMETER_BY_CLUSTER:
                if spec.cluster_id not in endpoint.clusters:
                    continue

                if spec.cluster_id == 0x00000202:  # FanControl
                    supported_attribute_ids: list[int] = node.get_attribute_value(endpoint_id, spec.cluster_id, 0xFFFB) or []
                    if supported_attribute_ids and spec.command_or_attribute_id not in supported_attribute_ids:
                        continue
                    return (
                        self._mapper.attribute_parameter_uuid(device_id, endpoint_id, spec.cluster_id, spec.command_or_attribute_id),
                        spec.default_params,
                    )

                accepted_command_ids: list[int] = node.get_attribute_value(endpoint_id, spec.cluster_id, 0xFFF9) or []
                if accepted_command_ids and spec.command_or_attribute_id not in accepted_command_ids:
                    continue

                return (
                    self._mapper.command_parameter_uuid(device_id, endpoint_id, spec.cluster_id, spec.command_or_attribute_id),
                    spec.default_params,
                )
        return None, None

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
            expected_credentials_options=self._mapper.define_credentials_options(
                node.commissioning_mode,
                node.pairing_hint,
                node.pairing_instruction,
                bool(node.addresses)
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
                    parameter_id = self._mapper.attribute_parameter_uuid(device_id, endpoint_id, cluster_id, attribute.attribute_id)
                    self._matter_client.subscribe_events(
                        self._make_callback(device_id, parameter_id, cluster_id, attribute.attribute_id),
                        EventType.ATTRIBUTE_UPDATED,
                        node.node_id,
                        attribute_path,
                    )

        # node.available flips when the device drops off/rejoins the Matter fabric while
        # the Hub keeps running (not just at Hub startup) — NODE_UPDATED fires on that and
        # other node-info changes, so track the last known value and only act on a real
        # transition instead of re-signalling on every unrelated update.
        self._node_availability[device_id] = node.available
        self._matter_client.subscribe_events(
            self._make_availability_callback(device_id),
            EventType.NODE_UPDATED,
            node.node_id,
        )

    def _make_callback(self, device_id: UUID, parameter_id: UUID, cluster_id: int, attribute_id: int):
        def callback(event_type, new_value):
            event = DeviceParameterChangedEvent(
                device_id=device_id,
                parameter_id=parameter_id,
                value=self._mapper.apply_attribute_scale(cluster_id, attribute_id, new_value),
            )
            asyncio.create_task(
                self.dependencies.output.controller_did_receive_device_events(self, [event])
            )
        return callback

    def _make_availability_callback(self, device_id: UUID):
        def callback(event_type, updated_node: MatterNode):
            was_available = self._node_availability.get(device_id)
            is_available = updated_node.available
            if is_available == was_available:
                return
            self._node_availability[device_id] = is_available
            if is_available:
                asyncio.create_task(self.dependencies.output.controller_did_connect_device(self, device_id))
            else:
                asyncio.create_task(self.dependencies.output.controller_did_lose_device(self, device_id))
        return callback