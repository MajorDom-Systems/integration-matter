import asyncio
import inspect

from aiohttp import ClientSession
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Objects import Identify
from functools import partial
from uuid import UUID
from matter_server.client import MatterClient
from matter_server.common.models import CommissionableNodeData, EventType

from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.base import NonEmptyStr
from majordom_hub.config import matter_server_url
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import Discovery, CredentialsValue
from majordom_hub.services.controller.framework.abstract_controller import AbstractController


from .model import MatterDevice, MatterDeviceState, MatterDeviceIntegrationData, MatterParameter, MatterParameterTypeEnum, MatterParameterState
from .mapper import MatterMapper


class MatterController(AbstractController):
    __matter_client: MatterClient
    __matter_client_session: ClientSession
    __majordom_descoveries: dict[UUID, Discovery] = dict()
    __connected_device: dict[UUID, UUID | None] = dict()
    # discovery_id: device_id mapping.
    # If the device is not connected, device_id is None.
    # For removal, lookup is performed by device_id.

    __matter_wifi_ssid: str
    __matter_wifi_secret: str  # in set_wifi_credentials this named is credentials
    __matter_thread_dataset: str


    __mapper = MatterMapper()

    @property
    def name(self) -> str:
        return "Matter"

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self.__majordom_descoveries

    async def start(self):
        self.__matter_client_session = ClientSession()
        self.__matter_client = MatterClient(matter_server_url, self.__matter_client_session)

        event = asyncio.Event()
        asyncio.create_task(self.__matter_client.start_listening(init_ready=event))
        await event.wait()

        await self.__matter_client.set_wifi_credentials(self.__matter_wifi_ssid, self.__matter_wifi_secret)
        await self.__matter_client.set_thread_operational_dataset(self.__matter_thread_dataset)

        await asyncio.create_task(self.__matter_discovery_loop())

    async def stop(self):
        if not self.__matter_client_session or not self.__matter_client:
            return
        await self.__matter_client.disconnect()
        await self.__matter_client_session.close()

    async def pair_device(self, discovery: Discovery, credentials: CredentialsValue | None):
        commission_node = await self.__matter_client.commision_with_code(credentials)
        self.__majordom_descoveries.pop(discovery.id)
        node = self.__matter_client.get_node(commission_node.node_id)
        device_id = self.__mapper.matter_id_to_uuid(f"{node.device_info.productName}_{node.device_info.productID}")
        async with self.dependencies.make_device_repository() as device_repository:
            device = await device_repository.state(device_id, MatterDeviceState)
            if device and (not device.integration_data):
                device.integration_data = MatterDeviceIntegrationData(node_id=node.node_id)
            
            parameters = await self.__mapper.parse_matter_node_paramters_to_commands_and_attributes(self.__matter_client, node)
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
            await device_repository.save(device, device_id)

        self.__connected_device[discovery.id] = device_id
        await self.dependencies.output.controller_did_connect_device(self, device_id)

    async def unpair(self, device: MatterDevice):
        for key, value in self.__connected_device.items():
            if value is device.id:
                self.__connected_device.pop(key)
        await self.__matter_client.remove_node(device.integration_data.node_id)

    async def identify(self, device: MatterDevice):
        command = Identify.Commands.Identify()
        node = self.__matter_client.get_node(device.integration_data.node_id)
        for endpoint_id in node.endpoints.keys():
            if node.has_cluster(3, endpoint_id):  # 3 is Identify cluster id
                await self.__matter_client.send_device_command(device.integration_data.node_id, endpoint_id, command)

    async def fetch(self, device: MatterDevice):
        if not (node := self.__matter_client.get_node(device.integration_data.node_id)):
            raise RuntimeError("Error this device is not found")        
        parameters = await self.__mapper.parse_matter_node_paramters_to_commands_and_attributes(self.__matter_client, node)
        events: list[DeviceParameterChangedEvent] = []
        for parameter in parameters:
            value = None
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
                    value=value,
                )
            )
        await self.dependencies.output.controller_did_receive_device_events(self, events)

    async def send_command(self, command: DeviceCommand, device: MatterDevice, parameter: MatterParameter):
        if not self.__matter_client.get_node(device.integration_data.node_id):
            raise RuntimeError("Error this device is not found")
        node = self.__matter_client.get_node(device.integration_data.node_id)
        endpoint_id = parameter.integration_data.endpoint_id
        if not (endpoint := node.endpoints[endpoint_id]):
            raise ValueError("Endpoint dosent exist")

        cluster_id = parameter.integration_data.cluster_id
        if not (cluster := endpoint.clusters[cluster_id]):
            raise ValueError("Cluster dosent exist")

        if parameter.integration_data.type is MatterParameterTypeEnum.command:
            if parameter.integration_data.command_id is None or parameter.integration_data.command_id < 0:
                raise ValueError("Error")
            if hasattr(cluster, "Commands"):
                for _, cmd_cls in inspect.getmembers(cluster.Commands, inspect.isclass):
                    if not issubclass(cmd_cls, ClusterCommand):
                        continue
                    cmd_id = getattr(cmd_cls, "command_id", -1)
                    if cmd_id == parameter.integration_data.command_id:
                        await self.__matter_client.send_device_command(node.node_id, endpoint_id, cluster_id, cmd_cls())
        
        if parameter.integration_data.type is MatterParameterTypeEnum.attribute:
            attribute_path = f"{endpoint_id}/{cluster_id}/{parameter.integration_data.attribute_id}"
            if attribute_path in node.node_data.attributes.keys():
                await self.__matter_client.write_attribute(node.node_id, attribute_path, command.value)
        
        await self.dependencies.output.controller_did_receive_device_events(self, [])

    async def __matter_discovery_loop(self, interval: int = 30):
        while True:
            try:
                nodes: list[CommissionableNodeData] = await self.__matter_client.discover_commissionable_nodes()
                for node in nodes:
                    await self.__async_matter_did_discover(node)
            except Exception as e:
                print(f"[{self.name}] discovery error: {e}")
            await asyncio.sleep(interval)

    async def __async_matter_did_discover(self, node: CommissionableNodeData):
        discovery_id = self.__mapper.matter_id_to_uuid(node.instance_name or node.device_name or "unknown")

        if discovery_id in self.__connected_device and self.__connected_device[discovery_id]:
            print(f'{self.name} Discovered known device: {node.device_name or node.instance_name}')
            return

        mj_discovery_info = Discovery(
            id=discovery_id,
            integration = NonEmptyStr(self.name),
            credentials = self.__mapper.define_credentials_type(
                node.commissioning_mode,
                node.pairing_hint,
                node.pairing_instruction
            ),
            expiration = None,
            transport = NonEmptyStr("IP" if node.addresses else "BLE"),
            device_name = NonEmptyStr(node.device_name or node.instance_name or "Unknown"),
            device_manufacturer = f"Vendor {node.vendor_id}" if node.vendor_id else None,
            device_category = node.device_type,
            device_icon = None,
        )

        self.__majordom_descoveries[discovery_id] = mj_discovery_info
        self.__connected_device[discovery_id] = None

        await self.dependencies.output.controller_did_receive_discovery(self, mj_discovery_info)

    async def __on_attribute_cahnged(self, device_id: UUID, attribute_path: str, new_value):
        paramter_id = self.__mapper.matter_id_to_uuid(attribute_path)
        event = DeviceParameterChangedEvent(
            device_id,
            paramter_id,
            new_value
        )
        await self.dependencies.output.controller_did_receive_device_events(self, [event])

    def subscription(self, device: MatterDevice):
        node = self.__matter_client.get_node(device.integration_data.node_id)
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if hasattr(cluster, "Attributes"):
                    for name, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                        if not issubclass(attribute, ClusterAttributeDescriptor):
                            continue

                        attribute_path = f"{endpoint_id}/{cluster_id}/{attribute.attribute_id}"

                        self.__matter_client.subscribe_events(
                            lambda event_type, new_value, attr_path=attribute_path:
                                asyncio.create_task(self.__on_attribute_cahnged(device.id, attr_path, new_value))
                            ,
                            EventType.ATTRIBUTE_UPDATED,
                            node.node_id,
                            attribute_path,
                        )
