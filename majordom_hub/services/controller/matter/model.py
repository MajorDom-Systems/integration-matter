from enum import Enum
from pydantic import BaseModel
from typing import Any

from majordom_hub.schemas.device import Device, Parameter, DeviceState, ParameterState



class MatterParameterTypeEnum(str, Enum):
    attribute = "attribute"
    command = "command"


class MatterDeviceIntegrationData(BaseModel):
    node_id: int


class MatterParameterIntegrationData(BaseModel):
    endpoint_id: int
    cluster_id: int
    is_client: bool = False
    command_id: int | None = None
    attribute_id: int | None = None
    type: MatterParameterTypeEnum  # "command" or "attribute"


class MatterDevice(Device):
    integration_data: MatterDeviceIntegrationData


class MatterParameter(Parameter):
    integration_data: MatterParameterIntegrationData


class MatterParameterState(ParameterState):
    integration_data: MatterParameterIntegrationData


class MatterDeviceState(Device, DeviceState):
    parameters: list[MatterParameterState]
