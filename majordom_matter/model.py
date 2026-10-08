from enum import StrEnum
from typing import Any
from uuid import UUID

from majordom_integration_sdk.schemas.base import Base
from majordom_integration_sdk.schemas.device import Device, DeviceState, Parameter, ParameterState
from pydantic import BaseModel, Field


class MatterParameterTypeEnum(StrEnum):
    attribute = "attribute"
    command = "command"


class MatterDeviceIntegrationData(Base):
    # Defaults to 0 (the "not yet commissioned" sentinel): the Hub seeds a provisional device
    # with empty integration_data before pair_device runs, and the read-back must validate as a
    # MatterDeviceState. pair_device sets the real node_id once matter-server commissions the node.
    node_id: int = 0
    black_list: list[UUID] = Field(default_factory=list)


class MatterParameterIntegrationData(BaseModel):
    endpoint_id: int
    cluster_id: int
    is_client: bool = False
    command_id: int | None = None
    attribute_id: int | None = None
    type: MatterParameterTypeEnum  # "command" or "attribute"
    # Args (keyed by field name) to send when this command is used as the device's one-tap main
    # parameter and needs them (e.g. a setpoint). A command with arguments is a `struct`, so it
    # also gets them as an id-keyed `default_value` to satisfy ParameterState.can_be_main_parameter;
    # send_command applies these when a command arrives with no value. See the zigbee model.
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


class MatterDeviceState(MatterDevice, DeviceState):
    parameters: list[MatterParameterState]
