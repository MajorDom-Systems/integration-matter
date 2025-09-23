from enum import Enum
from pydantic import BaseModel
from typing import Any

from majordom_hub.schemas.device import Device, Parameter



class MParameterTypeEnum(str, Enum):
    attribute = "attribute"
    command = "command"


class MDeviceIntegrationData(BaseModel):
    node_id: int
    identify_endpoint_id: int


class MParameterIntegrationData(BaseModel):
    endpoint_id: int
    cluster_id: int
    is_client: bool = False
    command_id: int | None = None
    attribute_id: int | None = None
    value: Any | None = None


class MDevice(Device):
    integration_data: MDeviceIntegrationData


class MParameter(Parameter):
    integration_data: MParameterIntegrationData
    display_name: str
    type: MParameterTypeEnum  # "command" or "attribute"
