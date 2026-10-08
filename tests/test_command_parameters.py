"""Command -> Parameter mapping: a command with arguments is a `struct` whose `fields` are its
arguments, and its value is keyed by those sub-parameter ids (see the SDK's ParameterDataType)."""

import base64
import inspect
from typing import cast, get_type_hints
from uuid import UUID, uuid4, uuid5

from chip.clusters import Objects
from chip.clusters.ClusterObjects import Cluster, ClusterCommand
from chip.clusters.Objects import (
    AccessControl,
    ApplicationLauncher,
    CameraAvStreamManagement,
    DoorLock,
    Identify,
    Messages,
    OnOff,
    ServiceArea,
    Thermostat,
    ThreadNetworkDirectory,
    TlsClientManagement,
)
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterVisibility
from matter_server.client.models.node import MatterNode

from majordom_matter.mapper import MatterMapper, _unwrap_optional

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


def test_bytes_argument_is_base64_data():
    command = Thermostat.Commands.SetActivePresetRequest
    assert _field(Thermostat, command, "presetHandle").data_type == ParameterDataType.data
    data = _mapper().parse_data_for_command(command, {"presetHandle": base64.b64encode(b"\x00\xff").decode()})
    assert data == {"presetHandle": b"\x00\xff"}


def _pin_code_commands() -> list[tuple[type[Cluster], type]]:
    return [
        (cluster, command)
        for _, cluster in inspect.getmembers(Objects, inspect.isclass)
        if issubclass(cluster, Cluster) and hasattr(cluster, "Commands")
        for _, command in inspect.getmembers(cluster.Commands, inspect.isclass)
        if issubclass(command, ClusterCommand)
        and getattr(command, "is_client", True)
        and _unwrap_optional(get_type_hints(command).get("PINCode")) is bytes
    ]


def test_typed_bytes_arguments_are_utf8_text():
    typed = [(cluster, command, "PINCode") for cluster, command in _pin_code_commands()]
    assert len(typed) >= 4  # DoorLock Lock/Unlock/UnlockWithTimeout/Unbolt
    typed += [
        (DoorLock, DoorLock.Commands.SetCredential, "credentialData"),
        (TlsClientManagement, TlsClientManagement.Commands.ProvisionEndpoint, "hostname"),
    ]
    for cluster, command, name in typed:
        assert _field(cluster, command, name).data_type == ParameterDataType.string, (command, name)
        assert _mapper().parse_data_for_command(command, {name: "1234"}) == {name: b"1234"}


def test_bytes_attribute_is_data_and_round_trips():
    attribute = ThreadNetworkDirectory.Attributes.PreferredExtendedPanID
    raw = b"\xde\xad\x00\xbe\xef\x00\x01\x02"

    class _Node:
        def get_attribute_value(self, endpoint_id, cluster_id, attribute_id):
            return [attribute.attribute_id] if attribute_id == 0xFFFB else None

    class _Endpoint:
        def get_attribute_value(self, cluster_id, attribute_id):
            return raw

    mapper = _mapper()
    params = mapper.parse_attributes(
        DEVICE_ID, 1, ThreadNetworkDirectory.id, ThreadNetworkDirectory, _Endpoint(), cast(MatterNode, _Node())
    )
    assert [p.data_type for p in params] == [ParameterDataType.data]
    assert mapper.parse_data_for_attribute(attribute, mapper.normalize_value(raw)) == raw


def test_infrastructure_commands_are_hidden():
    clusters = (
        Objects.IcdManagement,
        Objects.WiFiNetworkManagement,
        Objects.ThreadBorderRouterManagement,
        ThreadNetworkDirectory,
        Objects.JointFabricDatastore,
        Objects.JointFabricAdministrator,
        Objects.TlsCertificateManagement,
        TlsClientManagement,
        Objects.UnitTesting,
    )
    for cluster in clusters:
        params = _mapper().parse_commands(DEVICE_ID, 1, cluster.id, cluster, cast(MatterNode, _FakeNode()))
        assert params and {p.visibility for p in params} == {ParameterVisibility.system}, cluster.__name__


def test_aliro_reader_commands_are_hidden():
    for command in (DoorLock.Commands.SetAliroReaderConfig, DoorLock.Commands.ClearAliroReaderConfig):
        assert _command(DoorLock, command).visibility == ParameterVisibility.system
    assert _command(DoorLock, DoorLock.Commands.UnlockDoor).visibility == ParameterVisibility.user
