"""Command -> Parameter mapping: a command with arguments is a `struct` whose `fields` are its
arguments, and its value is keyed by those sub-parameter ids (see the SDK's ParameterDataType)."""

from typing import cast
from uuid import UUID, uuid4, uuid5

from chip.clusters.Objects import (
    AccessControl,
    ApplicationLauncher,
    CameraAvStreamManagement,
    DoorLock,
    Identify,
    Messages,
    OnOff,
    ServiceArea,
)
from majordom_integration_sdk.schemas.parameter import ParameterDataType
from matter_server.client.models.node import MatterNode

from majordom_matter.mapper import MatterMapper

DEVICE_ID = UUID(int=1)


class _FakeNode:
    def get_attribute_value(self, endpoint_id, cluster_id, attribute_id):
        return None  # no AcceptedCommandList -> every client command maps


def _mapper() -> MatterMapper:
    return MatterMapper(lambda s: uuid4(), uuid5)


def _command(cluster, command_cls):
    params = _mapper().parse_commands(DEVICE_ID, 1, cluster.id, cluster, cast(MatterNode, _FakeNode()))
    return next(p for p in params if p.integration_data.command_id == command_cls.command_id)


def _field(cluster, command_cls, name: str):
    return next(f for f in _command(cluster, command_cls).fields if f.name == name)


def test_command_with_arguments_is_a_struct_of_its_fields():
    param = _command(Identify, Identify.Commands.Identify)
    assert param.data_type == ParameterDataType.struct
    assert [(f.name, f.data_type) for f in param.fields] == [("identifyTime", ParameterDataType.integer)]
    assert param.fields[0].id == _mapper().command_field_uuid(DEVICE_ID, 1, Identify.id, 0x00, "identifyTime")


def test_command_without_arguments_stays_a_button():
    param = _command(OnOff, OnOff.Commands.Toggle)
    assert param.data_type == ParameterDataType.none
    assert param.fields is None


def test_nested_struct_and_list_arguments_are_opaque_data():
    assert _field(ApplicationLauncher, ApplicationLauncher.Commands.HideApp, "application").data_type == (
        ParameterDataType.data
    )
    assert _field(DoorLock, DoorLock.Commands.ClearCredential, "credential").data_type == ParameterDataType.data
    assert _field(AccessControl, AccessControl.Commands.ReviewFabricRestrictions, "arl").data_type == (
        ParameterDataType.data
    )
    assert _field(Messages, Messages.Commands.PresentMessagesRequest, "responses").data_type == ParameterDataType.data
    assert _field(ServiceArea, ServiceArea.Commands.SelectAreas, "newAreas").data_type == ParameterDataType.data


def test_nullable_argument_maps_to_its_value_type():
    assert _field(DoorLock, DoorLock.Commands.SetCredential, "userIndex").data_type == ParameterDataType.integer


def test_struct_value_by_field_id_or_name():
    mapper = _mapper()
    field_id = mapper.command_field_uuid(DEVICE_ID, 1, Identify.id, 0x00, "identifyTime")
    by_id = mapper.arguments_by_name(DEVICE_ID, 1, Identify.id, 0x00, Identify.Commands.Identify, {str(field_id): 5})
    by_name = mapper.arguments_by_name(DEVICE_ID, 1, Identify.id, 0x00, Identify.Commands.Identify, {"identifyTime": 5})
    assert by_id == by_name == {"identifyTime": 5}


def test_list_argument_round_trips_as_is():
    command = CameraAvStreamManagement.Commands.SetStreamPriorities
    data = _mapper().parse_data_for_command(command, {"streamPriorities": [1, 2]})
    assert data == {"streamPriorities": [1, 2]}
