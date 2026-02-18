import asyncio
import base64
import inspect

from aiohttp import ClientSession
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Objects import Identify
from functools import partial
from typing import Type, override
from matter_server.client import MatterClient
from matter_server.client.models.node import MatterNode
from matter_server.common.models import CommissionableNodeData, EventType
from uuid import UUID

from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.base import NonEmptyStr
from majordom_hub.config import matter_server_url
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import Discovery, CredentialsType, CredentialsValue
from majordom_hub.schemas.parameter import ParameterRole, ParameterDataType
from majordom_hub.services.controller.framework.abstract_controller import AbstractController


from .model import (
    MatterDevice,
    MatterDeviceState,
    MatterDeviceIntegrationData,
    MatterParameter,
    MatterParameterTypeEnum,
    MatterParameterState,
    MatterParameterIntegrationData
)
from .mapper import MatterMapper


class MatterController(AbstractController):
    _matter_client: MatterClient
    _matter_client_session: ClientSession
    _majordom_descoveries: dict[UUID, Discovery] = dict()
    _connected_device: dict[UUID, UUID | None] = dict()
    # discovery_id: device_id mapping.
    # If the device is not connected, device_id is None.
    # For removal, lookup is performed by device_id.

    _matter_wifi_ssid: str
    _matter_wifi_secret: str  # in set_wifi_credentials this named is credentials
    _matter_thread_dataset: str


    _mapper = MatterMapper()

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

    async def start(self):
        self._matter_client_session = ClientSession()
        self._matter_client = MatterClient(matter_server_url, self._matter_client_session)
        await self._matter_client.connect()
        event = asyncio.Event()
        asyncio.create_task(self._matter_client.start_listening(init_ready=event))
        await event.wait()
        # await self._matter_client.set_wifi_credentials(self._matter_wifi_ssid, self._matter_wifi_secret)
        # await self._matter_client.set_thread_operational_dataset(self._matter_thread_dataset)
        
        asyncio.create_task(self._matter_discovery_loop())
        if self._connected_device:
            for device_id in self._connected_device.values():
                async with self.dependencies.make_device_repository() as device_repository:
                    device = await device_repository.get(device_id, MatterDevice)
                    if not device:
                        continue
                    node = self._matter_client.get_node(device.node_id)
                    self._subscription(device_id, node)

    async def stop(self):
        if not self._matter_client_session or not self._matter_client:
            return
        await self._matter_client.disconnect()
        await self._matter_client_session.close()

    async def pair_device(self, discovery: Discovery, credentials: CredentialsValue | None):
        if discovery.credentials is CredentialsType.qr:
            commission_node = await self._matter_client.commission_with_code(str(credentials))
        elif discovery.credentials is CredentialsType.code:
            commission_node = await self._matter_client.commission_on_network(int(credentials))
        else:
            raise RuntimeError("This credentials type is not supported")
        self._majordom_descoveries.pop(discovery.id)
        node = self._matter_client.get_node(commission_node.node_id)
        device_id = self._mapper.matter_id_to_uuid(f"{node.device_info.productName}_{node.device_info.productID}")
        async with self.dependencies.make_device_repository() as device_repository:
            device = await device_repository.state(discovery.id, MatterDeviceState)
            assert device
            if device and (not device.integration_data):
                device.integration_data = MatterDeviceIntegrationData(node_id=node.node_id)
            
            parameters = await self._parse_matter_entities(node)
            for parameter in parameters:
                value=b''
                if parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                    value = node.get_attribute_value(
                        parameter.integration_data.endpoint_id,
                        parameter.integration_data.cluster_id,
                        parameter.integration_data.attribute_id,
                    )
                device.parameters.append(MatterParameterState(
                    **parameter.__dict__,
                    value=value,
                ))
            await device_repository.save(device, discovery.id)
            await device_repository.update_id(discovery.id, device_id)
        self._connected_device[discovery.id] = device_id
        await self.dependencies.output.controller_did_connect_device(self, device_id)
        self._subscription(device_id, node)
        return device_id

    async def unpair(self, device: MatterDevice):
        for key, value in self._connected_device.items():
            if value is device.id:
                self._connected_device.pop(key)
        await self._matter_client.remove_node(device.node_id)

    async def identify(self, device: MatterDevice):
        command = Identify.Commands.Identify()
        node = self._matter_client.get_node(device.node_id)
        for endpoint_id in node.endpoints.keys():
            if node.has_cluster(3, endpoint_id):  # 3 is Identify cluster id
                await self._matter_client.send_device_command(device.node_id, endpoint_id, command)

    async def fetch(self, device: MatterDevice):
        if not (node := self._matter_client.get_node(device.node_id)):
            raise RuntimeError("Error this device is not found")        
        parameters = await self._parse_matter_entities(node)
        events: list[DeviceParameterChangedEvent] = []
        for parameter in parameters:
            value = b""
            if parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                value = node.get_attribute_value(
                    parameter.integration_data.endpoint_id,
                    parameter.integration_data.cluster_id,
                    parameter.integration_data.attribute_id,
                )
            events.append(
                DeviceParameterChangedEvent(
                    device_id=device.id,
                    parameter_id=parameter.id,
                    value=value if isinstance(value, str | int) else None,
                )
            )
        await self.dependencies.output.controller_did_receive_device_events(self, events)

    async def send_command(self, command: DeviceCommand, device: MatterDevice, parameter: MatterParameter):
        if not self._matter_client.get_node(device.node_id):
            raise RuntimeError("Error this device is not found")
        node = self._matter_client.get_node(device.node_id)
        endpoint_id = parameter.integration_data.endpoint_id
        if not (endpoint := node.endpoints[endpoint_id]):
            raise ValueError("Endpoint dosent exist")

        cluster_id = parameter.integration_data.cluster_id
        if not (cluster := endpoint.clusters[cluster_id]):
            raise ValueError("Cluster dosent exist")
        value = None
        if parameter.integration_data.type is MatterParameterTypeEnum.command:
            if parameter.integration_data.command_id is None or parameter.integration_data.command_id < 0:
                raise ValueError("Error with command_id")
            if hasattr(cluster, "Commands"):
                for _, cmd_cls in inspect.getmembers(cluster.Commands, inspect.isclass):
                    if not issubclass(cmd_cls, ClusterCommand):
                        continue
                    cmd_id = getattr(cmd_cls, "command_id", -1)
                    if cmd_id == parameter.integration_data.command_id:
                        await self._matter_client.send_device_command(node.node_id, endpoint_id, cmd_cls())
        if parameter.integration_data.type is MatterParameterTypeEnum.attribute:
            if parameter.role != ParameterRole.control:
                raise RuntimeError("This parameter not writable")
            attribute_path = f"{endpoint_id}/{cluster_id}/{parameter.integration_data.attribute_id}"
            if attribute_path in node.node_data.attributes.keys():
                await self._matter_client.write_attribute(node.node_id, attribute_path, command.value)
                value = node.get_attribute_value(endpoint_id, cluster_id, parameter.integration_data.attribute_id)
        event = DeviceParameterChangedEvent(
            device_id = device.id,
            parameter_id = parameter.id,
            value = value,
        )
        #await self.dependencies.output.controller_did_receive_device_events(self, [event])

    # helpers

    async def _parse_matter_entities(self, node: MatterNode):
        params: list = []

        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if hasattr(cluster, "Commands"):
                    for name, cmd_cls in inspect.getmembers(cluster.Commands, inspect.isclass):
                        if not issubclass(cmd_cls, ClusterCommand):
                            continue
                        cmd_id = getattr(cmd_cls, "command_id", -1)
                        params.append(MatterParameter(
                            id=self._mapper.matter_id_to_uuid(f"command_{endpoint_id}/{cluster_id}/{cmd_id}"),
                            name=name,
                            data_type=ParameterDataType.none,
                            role=ParameterRole.control,
                            integration_data=MatterParameterIntegrationData(
                                endpoint_id=endpoint_id,
                                cluster_id=cluster_id,
                                command_id=cmd_id,
                                type=MatterParameterTypeEnum.command,
                            ),
                        ))
                
                if hasattr(cluster, "Attributes"):
                    for name, attr_cls in inspect.getmembers(cluster.Attributes, inspect.isclass):
                        if not issubclass(attr_cls, ClusterAttributeDescriptor):
                            continue
                        attr_id = getattr(attr_cls, "attribute_id", -1)
                        value = None
                        try:
                            value = node.get_attribute_value(endpoint_id, cluster_id, attr_id)
                            can_read = True
                        except Exception:
                            can_read = False
                        try:
                            await self._matter_client.write_attribute(
                                node.node_id,
                                f"{endpoint_id}/{cluster_id}/{attr_id}",
                                value
                            )
                            can_write = True
                        except Exception:
                            can_write = False
                        if can_read and can_write:
                            role = ParameterRole.control
                        elif can_read:
                            role = ParameterRole.sensor
                        else:
                            role = ParameterRole.event
                        params.append(MatterParameter(
                            id=self._mapper.matter_id_to_uuid(f"attribute_{endpoint_id}/{cluster_id}/{attr_id}"),
                            name=name,
                            data_type=self._mapper.get_parameter_data_type(value),
                            role=role,
                            integration_data=MatterParameterIntegrationData(
                                endpoint_id=endpoint_id,
                                cluster_id=cluster_id,
                                attribute_id=attr_id,
                                type=MatterParameterTypeEnum.attribute,
                            ),
                        ))
        return params

    async def _matter_discovery_loop(self, interval: int = 5):
        while True:
            try:
                nodes: list[CommissionableNodeData] = await self._matter_client.discover_commissionable_nodes()
                for node in nodes:
                    await self._async_matter_did_discover(node)
            except Exception as e:
                print(f"[{self.name}] discovery error: {e}")
            await asyncio.sleep(interval)

    async def _async_matter_did_discover(self, node: CommissionableNodeData):
        discovery_id = self._mapper.matter_id_to_uuid(node.instance_name or node.device_name or "unknown")

        if (device_id := self._connected_device.get(discovery_id)):
            print(f'{self.name} Discovered known device: {node.device_name or node.instance_name}')

            async with self.dependencies.make_device_repository() as device_repository:
                    device = await device_repository.get(device_id, MatterDevice)
                    if not device:
                        return
                    device_node = self._matter_client.get_node(device.node_id)
                    self._subscription(device_id, device_node)
            return

        mj_discovery_info = Discovery(
            id=discovery_id,
            integration = NonEmptyStr(self.name),
            credentials = self._mapper.define_credentials_type(
                node.commissioning_mode,
                node.pairing_hint,
                node.pairing_instruction
            ),
            expiration = None,
            transport = NonEmptyStr("IP" if node.addresses else "BLE"),
            device_name = NonEmptyStr(node.device_name or node.instance_name or "Unknown"),
            device_manufacturer = f"Vendor {node.vendor_id}" if node.vendor_id else None,
            device_category = str(node.device_type),
            device_icon = None,
        )

        self._majordom_descoveries[discovery_id] = mj_discovery_info
        self._connected_device[discovery_id] = None

        await self.dependencies.output.controller_did_receive_discovery(self, mj_discovery_info)

    def _subscription(self, device_id: UUID, node: MatterNode):
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if hasattr(cluster, "Attributes"):
                    for _, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                        if not issubclass(attribute, ClusterAttributeDescriptor):
                            continue
                        attribute_path = f"{endpoint_id}/{cluster_id}/{attribute.attribute_id}"
                        def on_attribute_changed(dev_id, param_id):
                            def callback(event_type, new_value):
                                event = DeviceParameterChangedEvent(
                                    device_id=dev_id,
                                    parameter_id=param_id,
                                    value=new_value
                                )
                                asyncio.create_task(self.dependencies.output.controller_did_receive_device_events(self, [event]))
                            return callback

                        parameter_id = self._mapper.matter_id_to_uuid("attribute_" + attribute_path)
                        callback = on_attribute_changed(device_id, parameter_id)
                        self._matter_client.subscribe_events(
                            callback,
                            EventType.ATTRIBUTE_UPDATED,
                            node.node_id,
                            attribute_path,
                        )
