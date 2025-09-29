from enum import Enum
from pydantic import BaseModel
from typing import Any

from majordom_hub.schemas.device import Device, Parameter



class MatterParameterTypeEnum(str, Enum):
    attribute = "attribute"
    command = "command"


class MatterDeviceIntegrationData(BaseModel):
    node_id: int
    identify_endpoint_id: int


class MatterParameterIntegrationData(BaseModel):
    endpoint_id: int
    cluster_id: int
    is_client: bool = False
    command_id: int | None = None
    attribute_id: int | None = None
    value: Any | None = None


class MatterDevice(Device):
    integration_data: MDeviceIntegrationData


class MatterParameter(Parameter):
    integration_data: MParameterIntegrationData
    type: MParameterTypeEnum  # "command" or "attribute"
