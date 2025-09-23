import inspect

from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from matter_server.client import MatterNode
from uuid import NAMESPACE_DNS, UUID, uuid5
from typing import Any

from majordom_hub.schemas.automation.events import DeviceParameterChangedEvent
from majordom_hub.schemas.parameter import ParameterRole, ParameterDataType

from .model import MParameter, MParameterTypeEnum, MParameterIntegrationData


class MatterMapper():

    def matter_id_to_uuid(self, id: str) -> UUID:
        return uuid5(NAMESPACE_DNS, id)

    def parse_matter_to_majordom_response(self, node: dict, device_id: str) -> list[DeviceParameterChangedEvent]:
        parameter_changed_events: list[DeviceParameterChangedEvent] = []
        for key, value in node:
            parameter_changed_events.append(DeviceParameterChangedEvent(
                device_id=self.matter_id_to_uuid(device_id),
                parameter_id=self.matter_id_to_uuid(key),
                value=value,
            ))
        return parameter_changed_events

    def get_parameter_data_type(self, value: Any) -> ParameterDataType:
        if value is None:
            return ParameterDataType.none
        if isinstance(value, bool):
            return ParameterDataType.bool
        if isinstance(value, int):
            return ParameterDataType.integer
        if isinstance(value, float):
            return ParameterDataType.decimal
        if isinstance(value, str):
            return ParameterDataType.string
        if isinstance(value, (bytes, bytearray, memoryview)):
            return ParameterDataType.data
        return ParameterDataType.none


    def parse_matter_node_paramters_to_commands_and_attributes(self, node: MatterNode) -> list[MParameter]:
        params: list = []

        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if hasattr(cluster, "Commands"):
                    for name, cmd_cls in inspect.getmembers(cluster.Commands, inspect.isclass):
                        if not issubclass(cmd_cls, ClusterCommand):
                            continue
                        cmd_id = getattr(cmd_cls, "command_id", -1)
                        params.append(MParameter(
                            id=self.matter_id_to_uuid(f"{endpoint_id}/{cluster_id}/{cmd_id}"),
                            name=name,
                            data_type=ParameterDataType.none,
                            role=ParameterRole.event,
                            integration_data=MParameterIntegrationData(
                                endpoint_id=endpoint_id,
                                cluster_id=cluster_id,
                                command_id=cmd_id,
                            ),
                            display_name=name,  # Do you need a display name?
                            type=MParameterTypeEnum.command,
                        ))
                
                if hasattr(cluster, "Attributes"):
                    for name, attr_cls in inspect.getmembers(cluster.Attributes, inspect.isclass):
                        if not issubclass(attr_cls, ClusterAttributeDescriptor):
                            continue
                        attr_id = getattr(attr_cls, "attribute_id", -1)
                        value = node.get_attribute_value(endpoint_id, cluster_id, attr_id)
                        params.append(MParameter(
                            id=self.matter_id_to_uuid(f"{endpoint_id}/{cluster_id}/{attr_id}"),
                            name=name,
                            data_type=self.get_parameter_data_type(value),
                            role=ParameterRole.event,
                            integration_data=MParameterIntegrationData(
                                endpoint_id=endpoint_id,
                                cluster_id=cluster_id,
                                attribute_id=attr_id,
                                value=value
                            ),
                            display_name=name,
                            type=MParameterTypeEnum.attribute,
                        ))
        return params