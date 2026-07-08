import base64

from enum import Enum
from pydantic import BaseModel, Field, field_validator
from uuid import UUID

from majordom_hub.schemas.device import Device, Parameter, DeviceState, ParameterState
from majordom_hub.schemas.base import Base


class MatterParameterTypeEnum(str, Enum):
    attribute = "attribute"
    command = "command"


class MatterDeviceIntegrationData(Base):
    node_id: int
    black_list: list[UUID] = Field(default_factory=list)


class MatterParameterIntegrationData(BaseModel):
    endpoint_id: int
    cluster_id: int
    is_client: bool = False
    command_id: int | None = None
    attribute_id: int | None = None
    type: MatterParameterTypeEnum  # "command" or "attribute"


class MatterDevice(Device):
    integration_data: MatterDeviceIntegrationData

    @property
    def node_id(self) -> int:
        assert self.integration_data
        if isinstance(self.integration_data, dict):
            return self.integration_data["node_id"]
        return self.integration_data.node_id


class MatterParameter(Parameter):
    integration_data: MatterParameterIntegrationData


class MatterParameterState(ParameterState):
    integration_data: MatterParameterIntegrationData
        

class MatterDeviceState(Device, DeviceState):
    parameters: list[MatterParameterState]
