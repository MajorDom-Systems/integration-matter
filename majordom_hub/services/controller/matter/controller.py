import asyncio
import inspect

from aiohttp import ClientSession
from chip.clusters.ClusterObjects import ClusterCommand
from chip.clusters.Objects import Identify
from uuid import UUID
from matter_server.client import MatterClient
from matter_server.common.models import CommissionableNodeData

from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.base import NonEmptyStr
from majordom_hub.config import matter_server_url
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import Discovery, CredentialsValue, CredentialsType
from majordom_hub.services.controller.framework.abstract_controller import AbstractController


from .model import MatterDevice, MatterParameter, MatterParameterTypeEnum
from .mapper import MatterMapper


class MatterController(AbstractController):
    __matter_client: MatterClient
    __matter_client_session: ClientSession
    __majordom_descoveries: dict[UUID, Discovery] = dict()

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
        device = await self.__matter_client.commision_with_code(credentials)
        self.__majordom_descoveries.pop(discovery.id)
        
        device_id = self.__mapper.matter_id_to_uuid(f"{device.device_info.productName}_{device.device_info.productID}")
        await self.dependencies.output.controller_did_connect_device(self, device_id)

    async def unpair(self, device: MatterDevice):
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

        if discovery_id in self.__majordom_descoveries:
            print(f'{self.name} Discovered known device: {node.device_name or node.instance_name}')
            return

        mj_discovery_info = Discovery(
            id=discovery_id,
            integration = NonEmptyStr(self.name),
            credentials = CredentialsType.code.with_mask("DDDDDDDDDDDDDDD"),
            expiration = None,
            transport = NonEmptyStr("IP" if node.addresses else "BLE"),
            device_name = NonEmptyStr(node.device_name or node.instance_name or "Unknown"),
            device_manufacturer = f"Vendor {node.vendor_id}" if node.vendor_id else None,
            device_category = node.device_type,
            device_icon = None,
        )

        self.__majordom_descoveries[discovery_id] = mj_discovery_info

        await self.dependencies.output.controller_did_receive_discovery(self, mj_discovery_info)
