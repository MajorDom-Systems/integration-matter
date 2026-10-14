"""Controller wiring tests against the in-process FakeMatterClient — no matter-server, no MVD.

These run in the default (non-docker) CI. They exercise discovery, pairing + parameter mapping,
commands, and attribute events through the REAL controller and mapper against a faithful canned
on-off-light node. The heavy real-device coverage (all 30 device types, real commissioning) lives
in the dockerized MVD suite, test_controller.py.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsType, Discovery, ProvidedCredentials
from majordom_integration_sdk.testing import build_test_dependencies

from majordom_matter import MatterController
from majordom_matter.mapper import MatterMapper
from majordom_matter.model import MatterDevice, MatterDeviceIntegrationData, MatterDeviceState, MatterParameterTypeEnum
from majordom_matter.testing import FakeMatterClient

FAKE_NODE_ID = 46  # matches fixtures/on_off_light_node.json


@pytest_asyncio.fixture
async def controller(monkeypatch):
    """A started MatterController whose matter-server client is the in-process fake."""
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    deps = build_test_dependencies(documents_folder=Path(tempfile.mkdtemp()), integration="Matter")
    controller = MatterController(deps)
    await controller.start()
    yield controller, deps.output
    await controller.stop()


async def _wait(predicate, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def _commission(controller, repository) -> UUID:
    """Seed the provisional row the Hub would create, then pair — returns the real device id."""
    await _wait(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))
    await _pair(controller, discovery, ProvidedCredentials(type=CredentialsType.code, value="20202021"))
    return controller.dependencies.output.connected_devices[-1]


async def _pair(controller, discovery: Discovery, credentials: ProvidedCredentials) -> None:
    repository = controller.dependencies.make_device_repository
    async with repository() as repo:
        await repo.save(
            MatterDeviceState(
                id=discovery.id,
                name="Test",
                room_id=UUID(int=1),
                transport="IP",
                integration="Matter",
                manufacturer=None,
                parameters=[],
                integration_data=MatterDeviceIntegrationData(node_id=0),
            )
        )
    await controller.pair_device(discovery, credentials)


async def test_discovers_the_canned_node(controller):
    ctrl, _output = controller
    await _wait(lambda: bool(ctrl.discoveries))
    discovery = next(iter(ctrl.discoveries.values()))
    assert discovery.integration == "Matter"
    assert discovery.transport == "IP"
    assert CredentialsType.code in discovery.expected_credentials_options


async def test_pairs_and_maps_parameters(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)

    assert output.connected_devices == [device_id]
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id)
    assert device is not None and device.integration_data.node_id == FAKE_NODE_ID
    # The on-off-light exposes the OnOff cluster (endpoint 13) → at least a toggle command maps.
    assert state is not None and len(state.parameters) > 0


async def test_send_command_reaches_the_client(controller):
    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)

    command_param = next(p for p in state.parameters if p.integration_data.type is MatterParameterTypeEnum.command)
    await ctrl.send_command(
        DeviceCommand(device_id=device_id, parameter_id=command_param.id, value=None), device, command_param
    )
    # The fake records the outgoing command against the node's endpoint.
    assert ctrl._matter_client.sent_commands, "send_command should reach the matter client"
    node_id, endpoint_id, _cmd = ctrl._matter_client.sent_commands[-1]
    assert node_id == FAKE_NODE_ID and endpoint_id == command_param.integration_data.endpoint_id


async def test_attribute_update_becomes_an_event(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    before = len(output.events)

    # Simulate the device pushing a new OnOff value; the subscription should surface an event.
    ctrl._matter_client.fire_attribute_update("13/6/0", True)
    await _wait(lambda: len(output.events) > before)

    event = output.events[-1]
    assert event.device_id == device_id


async def test_unpair_removes_the_node(controller):
    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    await ctrl.unpair(device)
    assert FAKE_NODE_ID in ctrl._matter_client.removed_nodes


async def test_fetch_emits_events(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    before = len(output.events)
    await ctrl.fetch(device)
    assert len(output.events) > before, "fetch should emit a DeviceParameterChange per attribute"


async def test_identify_sends_identify_command(controller):
    from chip.clusters.Objects import Identify

    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    await ctrl.identify(device)
    # The on-off-light carries the Identify cluster (3) on its application endpoint.
    assert any(isinstance(cmd, Identify.Commands.Identify) for _n, _ep, cmd in ctrl._matter_client.sent_commands), (
        "identify should send an Identify command to the node's Identify cluster"
    )


async def test_start_populates_discoveries_and_stop_clears(controller):
    # start() ran in the fixture and should have surfaced the canned node as a discovery;
    # stop() must clear that state (background tasks + discoveries) without error.
    ctrl, _output = controller
    await _wait(lambda: bool(ctrl.discoveries))
    assert ctrl.discoveries, "start() should surface the discovered node"

    await ctrl.stop()
    assert ctrl.discoveries == {}, "stop() should clear discoveries"


async def _move_to_level_parameter(ctrl, device_id: UUID):
    from chip.clusters.Objects import LevelControl

    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)
    param_id = ctrl._mapper.command_parameter_uuid(
        device_id, 13, LevelControl.id, LevelControl.Commands.MoveToLevel.command_id
    )
    return device, state, next(p for p in state.parameters if p.id == param_id)


async def test_command_with_arguments_is_a_struct(controller):
    from majordom_integration_sdk.schemas.parameter import ParameterDataType

    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    _device, _state, param = await _move_to_level_parameter(ctrl, device_id)
    assert param.data_type == ParameterDataType.struct
    assert param.fields


async def test_struct_value_reaches_the_device_by_field_id_or_name(controller):
    from chip.clusters.Objects import LevelControl
    from chip.tlv import uint

    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    device, _state, param = await _move_to_level_parameter(ctrl, device_id)
    field_id = ctrl._mapper.command_field_uuid(device_id, 13, LevelControl.id, 0x00, "level")

    for value in ({str(field_id): 7}, {"level": 7}):
        await ctrl.send_command(DeviceCommand(device_id=device_id, parameter_id=param.id, value=value), device, param)
        _node_id, _endpoint_id, cmd = ctrl._matter_client.sent_commands[-1]
        assert cmd == LevelControl.Commands.MoveToLevel(level=uint(7))


async def test_struct_command_main_parameter_taps_its_default_arguments(controller, monkeypatch):
    from chip.clusters.Objects import LevelControl
    from chip.tlv import uint

    from majordom_matter.matter_spec import MainParameterSpec

    monkeypatch.setattr(
        "majordom_matter.controller.MAIN_PARAMETER_BY_CLUSTER",
        {LevelControl.id: MainParameterSpec(0x00, {"level": 9})},
    )
    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    device, state, param = await _move_to_level_parameter(ctrl, device_id)
    assert state.main_parameter == param.id and state.can_set_main_parameter(param.id)
    field_id = ctrl._mapper.command_field_uuid(device_id, 13, LevelControl.id, 0x00, "level")
    assert param.default_value == {str(field_id): 9}
    assert MatterDeviceState.model_validate(state.model_dump(mode="json")).parameters_dict[param.id].default_value == (
        {str(field_id): 9}
    )

    tap = param.main_cycle[0]
    await ctrl.send_command(DeviceCommand(device_id=device_id, parameter_id=param.id, value=tap), device, param)
    assert ctrl._matter_client.sent_commands[-1][2] == LevelControl.Commands.MoveToLevel(level=uint(9))


async def _discovery_over(ctrl, transport: str) -> Discovery:
    """The canned node's discovery re-advertised over `transport`, offering both code and QR."""
    await _wait(lambda: bool(ctrl.discoveries))
    discovery = next(iter(ctrl.discoveries.values())).model_copy(
        update={"transport": transport, "expected_credentials_options": [CredentialsType.code, CredentialsType.qr]}
    )
    ctrl._majordom_descoveries[discovery.id] = discovery
    return discovery


def _spy_commissioning(ctrl) -> tuple[AsyncMock, AsyncMock]:
    client = ctrl._matter_client
    client.commission_with_code = AsyncMock(wraps=client.commission_with_code)
    client.commission_on_network = AsyncMock(wraps=client.commission_on_network)
    return client.commission_with_code, client.commission_on_network


async def test_qr_on_network_discovery_commissions_network_only(controller):
    # matter-server runs without a Bluetooth adapter in production; a non-network_only QR
    # commission fails there with "Bluetooth commissioning is not available".
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "IP")
    with_code, _on_network = _spy_commissioning(ctrl)

    await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.qr, value="MT:Y.K9042C00KA0648G00"))
    with_code.assert_awaited_once_with("MT:Y.K9042C00KA0648G00", network_only=True)


async def test_qr_on_ble_discovery_keeps_ble_commissioning(controller):
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "BLE")
    with_code, _on_network = _spy_commissioning(ctrl)

    await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.qr, value="MT:Y.K9042C00KA0648G00"))
    with_code.assert_awaited_once()
    assert with_code.await_args is not None and with_code.await_args.kwargs.get("network_only", False) is False


async def test_code_on_network_discovery_commissions_manual_code_network_only(controller):
    # commission_on_network takes the raw setup PIN, not the 11-digit manual pairing code.
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "IP")
    with_code, on_network = _spy_commissioning(ctrl)

    await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.code, value="3497-011-2332"))
    with_code.assert_awaited_once_with("34970112332", network_only=True)
    on_network.assert_not_awaited()


@pytest.mark.parametrize(("pairing_hint", "is_on_network"), [(None, True), (0x0002, False)])
def test_code_option_offers_the_matter_manual_code_mask(pairing_hint: int | None, is_on_network: bool):
    mapper = MatterMapper(lambda s: uuid4(), lambda d, s: uuid4())
    options = mapper.define_credentials_options(1, pairing_hint, is_on_network=is_on_network)
    assert options == [CredentialsType.code]
    assert options[0].code_mask == "DDDD-DDD-DDDD"


async def test_raw_setup_pin_on_network_discovery_commissions_on_network(controller):
    # 8 digits is the raw setup PIN, which only commission_on_network accepts.
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "IP")
    with_code, on_network = _spy_commissioning(ctrl)

    await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.code, value="2020-2021"))
    on_network.assert_awaited_once_with(20202021)
    with_code.assert_not_awaited()


async def test_raw_setup_pin_on_ble_discovery_is_rejected(controller):
    # A BLE-only device has no IP yet, and a raw PIN carries no discriminator for a BLE scan.
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "BLE")
    with_code, on_network = _spy_commissioning(ctrl)

    with pytest.raises(ValueError, match="setup PIN"):
        await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.code, value="20202021"))
    with_code.assert_not_awaited()
    on_network.assert_not_awaited()


@pytest.mark.parametrize(
    ("transport", "value", "expected_code"),
    [
        ("BLE", "3497-011-2332", "34970112332"),
        ("IP", "3497 0112 3320 0000 0001 2", "349701123320000000012"),
        ("IP", "MT:Y.K9042C00KA0648G00", "MT:Y.K9042C00KA0648G00"),
    ],
)
async def test_manual_and_qr_codes_entered_as_code_commission_with_code(
    controller, transport: str, value: str, expected_code: str
):
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, transport)
    with_code, on_network = _spy_commissioning(ctrl)

    await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.code, value=value))
    with_code.assert_awaited_once_with(expected_code, network_only=transport != "BLE")
    on_network.assert_not_awaited()


@pytest.mark.parametrize("value", ["12345", "123456789012", "not a code"])
async def test_pairing_code_of_unknown_length_is_rejected(controller, value: str):
    ctrl, _output = controller
    discovery = await _discovery_over(ctrl, "IP")
    with_code, on_network = _spy_commissioning(ctrl)

    with pytest.raises(ValueError, match="pairing code"):
        await _pair(ctrl, discovery, ProvidedCredentials(type=CredentialsType.code, value=value))
    with_code.assert_not_awaited()
    on_network.assert_not_awaited()


async def test_timed_invoke_follows_the_command(controller, monkeypatch):
    from chip.clusters.Objects import AdministratorCommissioning, OnOff

    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)
    timeouts: list[int | None] = []

    async def send_device_command(node_id, endpoint_id, command, timed_request_timeout_ms=None, **kwargs):
        timeouts.append(timed_request_timeout_ms)

    monkeypatch.setattr(ctrl._matter_client, "send_device_command", send_device_command)
    for endpoint_id, cluster_id, command_id in (
        (0, AdministratorCommissioning.id, AdministratorCommissioning.Commands.RevokeCommissioning.command_id),
        (13, OnOff.id, OnOff.Commands.Toggle.command_id),
    ):
        param_id = ctrl._mapper.command_parameter_uuid(device_id, endpoint_id, cluster_id, command_id)
        param = next(p for p in state.parameters if p.id == param_id)
        await ctrl.send_command(DeviceCommand(device_id=device_id, parameter_id=param.id, value=None), device, param)
    assert timeouts == [1000, None]


async def test_hidden_main_candidate_does_not_block_a_visible_one(controller, monkeypatch):
    from chip.clusters.Objects import LevelControl, OnOff

    from majordom_matter.matter_spec import MainParameterSpec

    monkeypatch.setattr(
        "majordom_matter.controller.MAIN_PARAMETER_BY_CLUSTER",
        {
            LevelControl.id: MainParameterSpec(LevelControl.Commands.Step.command_id, None),  # a `setting`
            OnOff.id: MainParameterSpec(OnOff.Commands.Toggle.command_id, None),
        },
    )
    ctrl, _output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        state = await repo.state(device_id, MatterDeviceState)
    assert state.main_parameter == ctrl._mapper.command_parameter_uuid(device_id, 13, OnOff.id, 0x02)
    assert state.can_set_main_parameter(state.main_parameter)


async def test_fetch_normalizes_values_like_the_subscription(controller):
    from chip.clusters.Objects import BasicInformation, Descriptor, NetworkCommissioning

    ctrl, output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
    last_network_id = ctrl._mapper.attribute_parameter_uuid(device_id, 0, NetworkCommissioning.id, 0x06)
    server_list = ctrl._mapper.attribute_parameter_uuid(device_id, 0, Descriptor.id, 0x01)
    capability_minima = ctrl._mapper.attribute_parameter_uuid(device_id, 0, BasicInformation.id, 0x13)

    ctrl._matter_client.fire_attribute_update("0/49/6", b"\x01\x02\xff")  # bytes
    await _wait(lambda: any(e.parameter_id == last_network_id for e in output.events))
    subscribed = next(e.value for e in output.events if e.parameter_id == last_network_id)
    before = len(output.events)
    await ctrl.fetch(device)
    fetched = {e.parameter_id: e.value for e in output.events[before:]}

    assert fetched[last_network_id] == subscribed == "AQL/"
    assert fetched[server_list] == [29, 31, 40, 42, 48, 49, 50, 51, 52, 60, 62, 63, 64]
    assert fetched[capability_minima] == {"caseSessionsPerFabric": 3, "subscriptionsPerFabric": 65535}


async def test_subscription_parses_raw_json_like_fetch(controller):
    from chip.clusters.Objects import BasicInformation, ElectricalEnergyMeasurement
    from matter_server.common.models import EventType

    ctrl, output = controller
    device_id = await _commission(ctrl, ctrl.dependencies.make_device_repository)
    async with ctrl.dependencies.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    def subscribed(parameter_id: UUID):
        return next((e.value for e in reversed(output.events) if e.parameter_id == parameter_id), None)

    # matter-server pushes raw JSON: a struct keyed by field id, not by name.
    capability_minima = ctrl._mapper.attribute_parameter_uuid(device_id, 0, BasicInformation.id, 0x13)
    ctrl._matter_client.fire_attribute_update("0/40/19", {"0": 4, "1": 3})
    await _wait(lambda: subscribed(capability_minima) is not None)
    pushed = subscribed(capability_minima)
    before = len(output.events)
    await ctrl.fetch(device)
    fetched = {e.parameter_id: e.value for e in output.events[before:]}
    assert pushed == fetched[capability_minima]
    assert fetched[capability_minima] == {"caseSessionsPerFabric": 4, "subscriptionsPerFabric": 3}

    # A scaled struct: the energy (mWh) is unwrapped and scaled to kWh, as pairing/fetch do.
    cluster_id = ElectricalEnergyMeasurement.id
    energy = ctrl._mapper.attribute_parameter_uuid(device_id, 1, cluster_id, 0x01)
    ctrl._make_callback(device_id, energy, cluster_id, 0x01)(EventType.ATTRIBUTE_UPDATED, {"0": 5_000_000})
    await _wait(lambda: subscribed(energy) is not None)
    parsed = ElectricalEnergyMeasurement.Structs.EnergyMeasurementStruct(energy=5_000_000)
    assert subscribed(energy) == ctrl._mapper.attribute_value(cluster_id, 0x01, parsed) == 5
