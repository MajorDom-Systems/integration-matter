from typing import Any
from enum import Enum
from pydantic import BaseModel, Field
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
    # Args to send when this command is used as the device's one-tap main parameter and needs
    # them (e.g. a setpoint). A command parameter's data_type is `none`, which already satisfies
    # ParameterState.can_be_main_parameter, so no `default_value` is needed for the flag — this
    # only carries *what to send*. NOTE: nothing in the hub reads this yet (the app reads the
    # top-level `default_value`, not integration_data); it's dead until the app consumes it or
    # the design collapses onto `default_value`. See the zigbee model for the same note.
    default_arguments: dict[str, Any] | None = None


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
