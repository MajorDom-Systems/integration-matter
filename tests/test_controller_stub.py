"""Controller wiring tests against the in-process FakeMatterClient — no matterjs-server, no MVD.

These run in the default (non-docker) CI. They exercise discovery, pairing + parameter mapping,
commands, and attribute events through the REAL controller and mapper against a faithful canned
on-off-light node. The heavy real-device coverage (all 30 device types, real commissioning) lives
in the dockerized MVD suite, test_controller.py.
"""

import asyncio
import logging
from dataclasses import replace
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from majordom_integration_sdk import (
    AbstractController,
    Endpoint,
    IntegrationError,
    Peripherals,
    Platform,
    WifiCredentials,
)
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsOption, CredentialsType, Discovery, ProvidedCredentials
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_platform
from matter_server.client.exceptions import CannotConnect
from matter_server.client.models.node import MatterNode
from matter_server.common.errors import UnknownError

from majordom_matter import MatterController
from majordom_matter.exceptions import MatterConnectionError, MatterUnexpectedError
from majordom_matter.mapper import MatterMapper
from majordom_matter.model import MatterDevice, MatterDeviceIntegrationData, MatterDeviceState, MatterParameterTypeEnum
from majordom_matter.testing import FakeMatterClient, FakeMatterServer, load_on_off_light_node

FAKE_NODE_ID = 46  # matches fixtures/on_off_light_node.json

SERVER = Peripherals({"matter": Endpoint("matter", "matter-server", "ws://matter-server")})


def _matter(platform: Platform) -> MatterController:
    controller = MatterController(platform)
    controller.peripherals = SERVER
    controller.output = RecordingControllerOutput()
    return controller


@pytest_asyncio.fixture
async def controller(monkeypatch):
    """A started MatterController whose matterjs-server client is the in-process fake."""
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=FakeMatterServer()))
    monkeypatch.setattr(MatterController, "_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.0)  # at the first failed reconnect
    controller = _matter(build_test_platform())
    await controller.start()
    yield controller, controller.output
    await controller.stop()


async def _wait(predicate, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def _commission(controller) -> UUID:
    """Seed the provisional row the Hub would create, then pair — returns the real device id."""
    await _wait(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))
    await _pair(controller, discovery, ProvidedCredentials(type=CredentialsType.code, value="20202021"))
    return controller.output.connected_devices[-1]


async def _pair(controller, discovery: Discovery, credentials: ProvidedCredentials | None):
    async with controller.platform.device_repository(controller) as repo:
        await repo.save(
            MatterDeviceState(
                id=discovery.id,
                name="Test",
                room_id=UUID(int=1),
                transport="IP",
                integration="matter",
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
    assert discovery.integration == "matter"
    assert discovery.transport == "IP"
    assert CredentialsType.code in discovery.expected_credentials_options


async def test_pairs_and_maps_parameters(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl)

    assert output.connected_devices == [device_id]
    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id)
    assert device is not None and device.integration_data.node_id == FAKE_NODE_ID
    # The on-off-light exposes the OnOff cluster (endpoint 13) → at least a toggle command maps.
    assert state is not None and len(state.parameters) > 0


async def test_send_command_reaches_the_client(controller):
    ctrl, _output = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
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
    device_id = await _commission(ctrl)
    before = len(output.events)

    # Simulate the device pushing a new OnOff value; the subscription should surface an event.
    ctrl._matter_client.fire_attribute_update("13/6/0", True)
    await _wait(lambda: len(output.events) > before)

    event = output.events[-1]
    assert event.device_id == device_id


async def test_unpair_removes_the_node(controller):
    ctrl, _output = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    await ctrl.unpair(device)
    assert FAKE_NODE_ID in ctrl._matter_client.removed_nodes


async def test_fetch_emits_events(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    before = len(output.events)
    await ctrl.fetch(device)
    assert len(output.events) > before, "fetch should emit a DeviceParameterChange per attribute"


async def test_identify_sends_identify_command(controller):
    from chip.clusters.Objects import Identify

    ctrl, _output = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
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

    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)
    param_id = ctrl._mapper.command_parameter_uuid(
        device_id, 13, LevelControl.id, LevelControl.Commands.MoveToLevel.command_id
    )
    return device, state, next(p for p in state.parameters if p.id == param_id)


async def test_command_with_arguments_is_a_struct(controller):
    from majordom_integration_sdk.schemas.parameter import ParameterDataType

    ctrl, _output = controller
    device_id = await _commission(ctrl)
    _device, _state, param = await _move_to_level_parameter(ctrl, device_id)
    assert param.data_type == ParameterDataType.struct
    assert param.fields


async def test_struct_value_reaches_the_device_by_field_id_or_name(controller):
    from chip.clusters.Objects import LevelControl
    from chip.tlv import uint

    ctrl, _output = controller
    device_id = await _commission(ctrl)
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
    device_id = await _commission(ctrl)
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
    # matterjs-server may run without Bluetooth; a non-network_only QR
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
    assert isinstance(options[0], CredentialsOption)
    assert options[0].mask == "DDDD-DDD-DDDD"


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
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
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
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
        state = await repo.state(device_id, MatterDeviceState)
    assert state.main_parameter == ctrl._mapper.command_parameter_uuid(device_id, 13, OnOff.id, 0x02)
    assert state.can_set_main_parameter(state.main_parameter)


async def test_fetch_normalizes_values_like_the_subscription(controller):
    from chip.clusters.Objects import BasicInformation, Descriptor, NetworkCommissioning

    ctrl, output = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
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
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)

    def subscribed(parameter_id: UUID):
        return next((e.value for e in reversed(output.events) if e.parameter_id == parameter_id), None)

    # matterjs-server pushes raw JSON: a struct keyed by field id, not by name.
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


async def test_start_hands_the_hub_networks_to_matter_server(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    controller = _matter(build_test_platform(wifi=WifiCredentials("home", "secret"), thread_dataset="0e08abcd"))
    await controller.start()
    assert cast(FakeMatterClient, controller._matter_client).networks == {
        "wifi": ("home", "secret"),
        "thread": "0e08abcd",
    }
    await controller.stop()


async def _refuse(self: FakeMatterClient, *args: Any):
    raise UnknownError("refused")  # matterjs-server answers with an error


async def test_a_network_matterjs_server_refuses_at_start_leaves_matter_running(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    monkeypatch.setattr(FakeMatterClient, "set_thread_operational_dataset", _refuse)
    controller = _matter(build_test_platform(thread_dataset="0e08abcd"))
    await controller.start()  # it still serves every device it has, and Wi-Fi and Ethernet ones
    await _wait(lambda: bool(controller.discoveries))
    await controller.stop()


async def test_a_network_matterjs_server_refuses_on_reconnect_leaves_matter_connected(controller, monkeypatch):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    monkeypatch.setattr(FakeMatterClient, "set_wifi_credentials", _refuse)
    before, ctrl.platform = ctrl.platform, replace(ctrl.platform, wifi=WifiCredentials("home", "secret"))
    assert await ctrl.platform_facts_did_change(before)  # in place
    output.connected_devices.clear()
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    lost.server.restore()  # its container recreated
    await _wait(lambda: output.connected_devices == [device_id])
    assert ctrl._matter_client is not lost
    assert output.error_log == []


@pytest.mark.parametrize(
    ("facts", "networks"),
    [
        ({"thread_dataset": "0e08abcd"}, {"thread": "0e08abcd"}),
        ({"wifi": WifiCredentials("home", "secret")}, {"wifi": ("home", "secret")}),
    ],
)
async def test_a_new_network_reaches_matter_server_in_place(
    controller, facts: dict[str, Any], networks: dict[str, Any]
):
    ctrl, _ = controller
    before, ctrl.platform = ctrl.platform, replace(ctrl.platform, **facts)
    assert await ctrl.platform_facts_did_change(before)
    await _wait(lambda: cast(FakeMatterClient, ctrl._matter_client).networks == networks)


async def test_a_new_network_matter_server_doesnt_answer_holds_neither_the_hook_nor_matter(controller):
    ctrl, output = controller
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.is_answering = False  # busy or hung, its connection open
    before, ctrl.platform = ctrl.platform, replace(ctrl.platform, thread_dataset="0e08abcd")
    assert await asyncio.wait_for(ctrl.platform_facts_did_change(before), 1)

    server.lose()  # with the call still waiting for its answer
    await _wait(lambda: "server-unreachable" in output.errors)
    server.is_answering = True
    server.restore()
    await _wait(lambda: "server-unreachable" not in output.errors)
    assert cast(FakeMatterClient, ctrl._matter_client).networks == {"thread": "0e08abcd"}


async def test_a_new_network_during_a_reconnect_reaches_the_new_connection(controller, monkeypatch):
    ctrl, _ = controller
    lost = cast(FakeMatterClient, ctrl._matter_client)
    is_listening, is_dumped = asyncio.Event(), asyncio.Event()  # the node dump makes a new connection ready
    start_listening = FakeMatterClient.start_listening

    async def dumping_nodes(self: FakeMatterClient, init_ready: asyncio.Event | None = None):
        is_listening.set()
        await is_dumped.wait()
        await start_listening(self, init_ready)

    monkeypatch.setattr(FakeMatterClient, "start_listening", dumping_nodes)
    lost.lose_server()
    lost.server.restore()
    await asyncio.wait_for(is_listening.wait(), 1)

    before, ctrl.platform = ctrl.platform, replace(ctrl.platform, thread_dataset="0e08abcd")
    assert await ctrl.platform_facts_did_change(before)
    assert ctrl._matter_client is lost  # not the new connection before it is ready: its node dump comes first
    is_dumped.set()
    await _wait(lambda: cast(FakeMatterClient, ctrl._matter_client).networks == {"thread": "0e08abcd"})
    assert lost.networks == {}


async def test_a_new_thread_network_reaches_a_lost_matter_server_once_it_is_back(controller):
    ctrl, output = controller
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    await _wait(lambda: "server-unreachable" in output.errors)
    before, ctrl.platform = ctrl.platform, replace(ctrl.platform, thread_dataset="0e08abcd")
    assert await ctrl.platform_facts_did_change(before)
    lost.server.restore()
    await _wait(lambda: "server-unreachable" not in output.errors)
    assert cast(FakeMatterClient, ctrl._matter_client).networks == {"thread": "0e08abcd"}


async def test_a_device_whose_node_is_gone_doesnt_stop_matter_from_starting(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    controller = _matter(build_test_platform())
    # left by a commissioning that failed, or a reset matterjs-server
    async with controller.platform.device_repository(controller) as repo:
        await repo.save(
            MatterDeviceState(
                id=UUID(int=7),
                name="Bulb",
                room_id=UUID(int=1),
                transport="BLE",
                integration="matter",
                manufacturer=None,
                parameters=[],
                integration_data=MatterDeviceIntegrationData(node_id=0),
            )
        )
    await controller.start()
    assert cast(RecordingControllerOutput, controller.output).lost_devices == [UUID(int=7)]  # the app hears it too
    await controller.stop()


@pytest.mark.parametrize("credentials", [None, ProvidedCredentials(type=CredentialsType.none)])
async def test_a_node_on_the_fabric_without_a_device_is_added_as_it_is(monkeypatch, credentials):
    server = FakeMatterServer()
    server.commissioned = True  # by a pairing that ended before its device was saved, or a device deleted since
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=server))
    controller = _matter(build_test_platform())
    await controller.start()
    (discovery,) = controller.discoveries.values()
    assert discovery.expected_credentials_options == [CredentialsType.none]

    await _pair(controller, discovery, credentials)
    async with controller.platform.device_repository(controller) as repo:
        device = await repo.get(discovery.id, as_=MatterDevice)
    assert device is not None and device.integration_data.node_id == FAKE_NODE_ID
    assert cast(RecordingControllerOutput, controller.output).connected_devices == [discovery.id]
    assert controller.discoveries == {}
    await controller.stop()


async def test_a_device_that_never_finished_commissioning_unpairs(controller):
    controller, _ = controller
    device = MatterDevice(
        id=UUID(int=8),
        name="Bulb",
        room_id=UUID(int=1),
        transport="BLE",
        integration="matter",
        manufacturer=None,
        integration_data=MatterDeviceIntegrationData(node_id=0),
    )
    await controller.unpair(device)
    assert controller._matter_client.removed_nodes == []


async def test_matter_reconnects_once_a_lost_matter_server_is_back(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    output.connected_devices.clear()
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    await _wait(lambda: output.lost_devices == [device_id])
    await asyncio.sleep(0.1)  # a few failed attempts
    lost.server.restore()
    await _wait(lambda: output.connected_devices == [device_id])
    lost_server = IntegrationError("server-unreachable", "Lost the connection to the Matter server, reconnecting.")
    assert output.error_log == [lost_server, "server-unreachable"]  # reported, then resolved

    output.events.clear()
    cast(FakeMatterClient, ctrl._matter_client).fire_attribute_update("13/6/0", True)
    await _wait(lambda: bool(output.events))  # subscribed again on the new connection


async def test_a_matter_server_back_at_the_first_reconnect_reports_no_error(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    output.connected_devices.clear()
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    lost.server.restore()  # e.g. its container recreated
    await _wait(lambda: output.connected_devices == [device_id])
    assert output.error_log == []


async def test_a_lost_matter_server_is_reported_only_once_the_outage_lasted(controller, monkeypatch):
    ctrl, output = controller
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.3)  # 15 s on a hub
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost_at = asyncio.get_running_loop().time()
    lost.lose_server()
    await asyncio.sleep(0.1)  # a few failed attempts: an outage, yet maybe a container recreated
    assert output.error_log == []
    await _wait(lambda: "server-unreachable" in output.errors)
    assert asyncio.get_running_loop().time() - lost_at >= 0.3
    lost.server.restore()
    await _wait(lambda: "server-unreachable" not in output.errors)


async def test_a_matter_server_dropping_every_connection_after_its_node_list_is_reported(controller, monkeypatch):
    """Each connection holds for a moment only: the drops add up to one outage, until a connection holds."""
    ctrl, output = controller
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.3)

    async def dropping(self: FakeMatterClient, init_ready: asyncio.Event | None = None):
        if init_ready is not None:
            init_ready.set()  # its node list
        await asyncio.sleep(0.05)

    monkeypatch.setattr(FakeMatterClient, "start_listening", dropping)
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.lose()
    server.restore()
    await _wait(lambda: "server-unreachable" in output.error_log)  # reported, and cleared at the next reconnect
    lost_server = IntegrationError("server-unreachable", "Lost the connection to the Matter server, reconnecting.")
    assert output.error_log[:2] == [lost_server, "server-unreachable"]


async def test_a_matter_server_dropping_every_connection_is_tried_less_and_less_often(controller, monkeypatch):
    ctrl, _ = controller
    connections = 0

    async def dropping(self: FakeMatterClient, init_ready: asyncio.Event | None = None):
        nonlocal connections
        connections += 1
        if init_ready is not None:
            init_ready.set()  # its node list, then it drops

    monkeypatch.setattr(FakeMatterClient, "start_listening", dropping)
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.lose()
    server.restore()
    await asyncio.sleep(0.3)
    assert connections <= 6  # one outage: back after 0.01, 0.03, 0.07, 0.15 s, not every 10 ms


async def test_a_matter_server_lost_again_soon_after_it_came_back_is_reported_at_once(controller, monkeypatch):
    ctrl, output = controller
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.3)
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.lose()
    await _wait(lambda: "server-unreachable" in output.errors)
    server.restore()
    await _wait(lambda: "server-unreachable" not in output.errors)  # the devices work again

    server.lose()  # the same outage: the connection didn't hold
    await asyncio.sleep(0.05)
    assert "server-unreachable" in output.errors


async def test_a_matter_server_lost_long_after_it_came_back_is_a_new_outage(controller, monkeypatch):
    ctrl, output = controller
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.3)
    monkeypatch.setattr(MatterController, "_STABLE_CONNECTION", 0.2)  # STABLE_RUN (5 min) on a hub
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.lose()
    await _wait(lambda: "server-unreachable" in output.errors)
    server.restore()
    await _wait(lambda: "server-unreachable" not in output.errors)
    await asyncio.sleep(0.3)  # held: the outage ended

    server.lose()
    await asyncio.sleep(0.05)
    assert "server-unreachable" not in output.errors  # not yet: this outage only began


async def test_a_matter_server_dropping_every_connection_before_it_speaks_is_unreachable(controller, monkeypatch):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    output.connected_devices.clear()
    start_listening = FakeMatterClient.start_listening

    async def dropped(self: FakeMatterClient, init_ready: asyncio.Event | None = None):
        return None  # accepted, then closed before its node dump

    monkeypatch.setattr(FakeMatterClient, "start_listening", dropped)
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    lost.server.restore()
    await _wait(lambda: "server-unreachable" in output.errors)
    assert output.lost_devices == [device_id] and output.connected_devices == []

    monkeypatch.setattr(FakeMatterClient, "start_listening", start_listening)
    await _wait(lambda: "server-unreachable" not in output.errors)
    assert output.connected_devices == [device_id]


async def test_a_start_reports_the_devices_on_the_fabric_connected(controller):
    ctrl, output = controller
    device_id = await _commission(ctrl)
    await ctrl.stop()  # the Hub marks its devices unavailable
    output.connected_devices.clear()
    await ctrl.start()
    assert output.connected_devices == [device_id]


async def test_a_start_leaves_a_device_matterjs_server_cant_reach_as_the_hub_marked_it(controller):
    ctrl, output = controller
    await _commission(ctrl)
    server = cast(FakeMatterClient, ctrl._matter_client).server
    await ctrl.stop()  # the Hub marks its devices unavailable, and every device at boot
    output.connected_devices.clear()
    server.node.node_data.available = False  # unplugged meanwhile
    await ctrl.start()
    assert output.connected_devices == [] and output.lost_devices == []  # reported once it is back


async def test_stopping_matter_while_it_reconnects(monkeypatch):
    server = FakeMatterServer()
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=server))
    monkeypatch.setattr(MatterController, "_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(MatterController, "_UNREACHABLE_AFTER", 0.0)
    controller = _matter(build_test_platform())
    await controller.start()
    server.lose()
    output = cast(RecordingControllerOutput, controller.output)
    await _wait(lambda: bool(output.errors))
    await asyncio.sleep(0.1)  # a few failed attempts
    await asyncio.wait_for(controller.stop(), 2)
    assert list(output.errors) == ["server-unreachable"]


async def test_a_matter_server_gone_before_it_is_ready_fails_the_start(monkeypatch):
    class GoneAtOnce(FakeMatterClient):
        async def start_listening(self, init_ready: asyncio.Event | None = None):
            return None

    monkeypatch.setattr("majordom_matter.controller.MatterClient", GoneAtOnce)
    controller = _matter(build_test_platform())

    with pytest.raises(MatterConnectionError):
        await asyncio.wait_for(controller.start(), 2)
    await controller.stop()  # as the runtime does after a failed start


async def test_stopping_matter_after_a_start_that_failed_late_leaves_nothing_running(monkeypatch):
    def unreadable(self: FakeMatterClient) -> list:
        raise RuntimeError("unreadable node dump")

    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    monkeypatch.setattr(FakeMatterClient, "get_nodes", unreadable)
    controller = _matter(build_test_platform())
    before = asyncio.all_tasks()

    with pytest.raises(RuntimeError):
        await controller.start()
    await controller.stop()
    assert [task for task in asyncio.all_tasks() - before if not task.done()] == []


async def test_stopping_matter_after_its_first_listener_failed(monkeypatch):
    class FailingListener(FakeMatterClient):
        async def start_listening(self, init_ready: asyncio.Event | None = None):
            await super().start_listening(init_ready)
            raise RuntimeError("listening failed")  # once its matterjs-server went away

    server = FakeMatterServer()
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FailingListener, server=server))
    monkeypatch.setattr(MatterController, "_RECONNECT_DELAY", 0.01)
    controller = _matter(build_test_platform())
    await controller.start()
    first = controller._matter_client
    server.lose()
    server.restore()
    await _wait(lambda: controller._matter_client is not first)
    await asyncio.wait_for(controller.stop(), 2)


async def test_stopping_matter_reports_no_error(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    controller = _matter(build_test_platform())
    await controller.start()
    await controller.stop()
    await asyncio.sleep(0.1)
    assert cast(RecordingControllerOutput, controller.output).error_log == []


async def test_stopping_matter_ends_the_reports_a_device_started(controller):
    ctrl, _ = controller
    await _commission(ctrl)
    is_reporting, is_ended = asyncio.Event(), asyncio.Event()

    class Hung(RecordingControllerOutput):
        async def controller_did_receive_events(self, controller: AbstractController, events: Any):
            is_reporting.set()
            try:
                await asyncio.Event().wait()
            finally:
                is_ended.set()

    ctrl.output = Hung()
    cast(FakeMatterClient, ctrl._matter_client).fire_attribute_update("13/6/0", True)
    await asyncio.wait_for(is_reporting.wait(), 1)
    await ctrl.stop()
    assert is_ended.is_set()


async def test_stopping_matter_after_a_first_start_that_couldnt_connect_closes_its_session(monkeypatch):
    server = FakeMatterServer()
    server.up = False  # its port answers, its /ws doesn't yet
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=server))
    controller = _matter(build_test_platform())
    with pytest.raises(CannotConnect):
        await controller.start()
    session = controller._matter_client_session
    await controller.stop()  # as the runtime does after a failed start
    assert session.closed


async def test_stopping_matter_before_it_started(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    await _matter(build_test_platform()).stop()  # the runtime stops one whose start never ran


async def test_stopping_matter_closes_its_session_though_the_client_fails_to_disconnect(monkeypatch):
    async def failing(self: FakeMatterClient):
        raise ConnectionResetError("matterjs-server reset the connection")

    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    monkeypatch.setattr(FakeMatterClient, "disconnect", failing)
    controller = _matter(build_test_platform())
    await controller.start()
    session = controller._matter_client_session
    with pytest.raises(ConnectionResetError):
        await controller.stop()
    assert session.closed


async def test_a_stopped_matter_says_it_isnt_started(controller):
    ctrl, _ = controller
    await _wait(lambda: bool(ctrl.discoveries))
    discovery = next(iter(ctrl.discoveries.values()))
    await ctrl.stop()
    with pytest.raises(MatterConnectionError):
        await ctrl.pair_device(discovery, ProvidedCredentials(type=CredentialsType.code, value="20202021"))


async def test_a_reconnect_that_breaks_reports_matter_crashed(controller):
    ctrl, _ = controller
    await _commission(ctrl)

    class Locked(RecordingControllerOutput):
        async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
            raise RuntimeError("database is locked")  # the Hub's

    output = ctrl.output = Locked()
    cast(FakeMatterClient, ctrl._matter_client).lose_server()
    await _wait(lambda: "crashed" in output.errors)
    assert not output.errors["crashed"].is_still_running  # the runtime restarts Matter


async def test_a_failure_other_than_an_outage_on_reconnect_reports_matter_crashed(controller, monkeypatch):
    ctrl, output = controller
    lost = cast(FakeMatterClient, ctrl._matter_client)

    def unreadable(self: FakeMatterClient) -> list:
        raise RuntimeError("unreadable node dump")

    monkeypatch.setattr(FakeMatterClient, "get_nodes", unreadable)
    lost.lose_server()
    lost.server.restore()
    await _wait(lambda: "crashed" in output.errors)
    assert "server-unreachable" not in output.errors  # not an outage: no reconnecting forever


async def test_a_pairing_landing_while_matter_reports_its_losses_leaves_it_reconnecting(controller, monkeypatch):
    ctrl, output = controller
    first = await _commission(ctrl)
    second = load_on_off_light_node()
    second.node_data.node_id = FAKE_NODE_ID + 1  # on the fabric without a device: added as it is
    on_fabric = FakeMatterClient.get_nodes
    monkeypatch.setattr(FakeMatterClient, "get_nodes", lambda self: [*on_fabric(self), second])
    second_id = ctrl._mapper.device_uuid(second.node_id, second.device_info.productName)
    discovery = output.received_discoveries[0].model_copy(
        update={"id": second_id, "expected_credentials_options": [CredentialsType.none]}
    )
    ctrl.discoveries[second_id] = discovery  # as offered at a start

    is_reporting = asyncio.Event()

    class Slow(RecordingControllerOutput):
        async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID):
            is_reporting.set()
            await asyncio.sleep(0.1)  # the Hub's write and send

    output = ctrl.output = Slow()
    lost = cast(FakeMatterClient, ctrl._matter_client)
    lost.lose_server()
    await asyncio.wait_for(is_reporting.wait(), 1)
    await _pair(ctrl, discovery, None)  # quick: nothing to commission
    lost.server.restore()
    await _wait(lambda: set(output.connected_devices) == {first, discovery.id} and ctrl._matter_client is not lost)
    assert output.error_log == []


async def _pairing_in_flight(ctrl: MatterController) -> asyncio.Task:
    """A pairing whose commissioning waits for matterjs-server (a Thread one takes a minute)."""
    await _wait(lambda: bool(ctrl.discoveries))
    discovery = next(iter(ctrl.discoveries.values()))
    cast(FakeMatterClient, ctrl._matter_client).server.is_answering = False
    credentials = ProvidedCredentials(type=CredentialsType.code, value="20202021")
    pairing = asyncio.create_task(_pair(ctrl, discovery, credentials))
    await asyncio.sleep(0.05)
    return pairing


async def test_a_pairing_cut_short_by_a_lost_matterjs_server_says_why(controller):
    ctrl, _ = controller
    pairing = await _pairing_in_flight(ctrl)
    cast(FakeMatterClient, ctrl._matter_client).lose_server()  # the client cancels the calls waiting for an answer
    with pytest.raises(MatterConnectionError, match="ended during the pairing"):  # the app says why
        await pairing


async def test_a_pairing_cancelled_itself_stays_cancelled(controller):
    ctrl, _ = controller
    pairing = await _pairing_in_flight(ctrl)
    pairing.cancel()  # the Hub's request ended
    with pytest.raises(asyncio.CancelledError):
        await pairing


@pytest.mark.parametrize(
    ("call", "during"),
    [("send_command", "the command"), ("identify", "the identification"), ("unpair", "the unpairing")],
)
async def test_a_call_cut_short_by_a_lost_matterjs_server_says_why(controller, call: str, during: str):
    ctrl, _ = controller
    device_id = await _commission(ctrl)
    async with ctrl.platform.device_repository(ctrl) as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)
    toggle = next(p for p in state.parameters if p.integration_data.type is MatterParameterTypeEnum.command)
    command = DeviceCommand(device_id=device_id, parameter_id=toggle.id, value=None)
    calls = {
        "send_command": lambda: ctrl.send_command(command, device, toggle),
        "identify": lambda: ctrl.identify(device),
        "unpair": lambda: ctrl.unpair(device),
    }
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.is_answering = False
    waiting = asyncio.create_task(calls[call]())
    await asyncio.sleep(0.05)
    server.lose()  # the client cancels the calls waiting for an answer
    with pytest.raises(MatterConnectionError, match=f"ended during {during}$"):
        await waiting


async def test_a_matter_server_gone_during_the_hand_over_on_reconnect_is_reconnected(monkeypatch):
    server = FakeMatterServer()
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=server))
    monkeypatch.setattr(MatterController, "_RECONNECT_DELAY", 0.01)
    controller = _matter(build_test_platform(thread_dataset="0e08abcd"))
    await controller.start()
    first = controller._matter_client
    server.is_answering = False
    server.lose()
    server.restore()
    await _wait(lambda: controller._matter_client is not first)  # its hand-over waits for an answer
    second = controller._matter_client
    server.is_answering = True
    server.lose()  # the client cancels the hand-over
    server.restore()
    await _wait(lambda: controller._matter_client is not second)
    assert cast(FakeMatterClient, controller._matter_client).networks == {"thread": "0e08abcd"}
    await controller.stop()


async def test_mdns_discovery_goes_on_after_matter_server_went_away_mid_call(controller):
    ctrl, _ = controller
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.is_answering = False
    discovering = asyncio.create_task(ctrl._matter_discovery_loop(interval=0.01))
    await asyncio.sleep(0.05)
    server.lose()  # the client cancels the listing waiting for an answer
    await asyncio.sleep(0.05)
    assert not discovering.done()
    discovering.cancel()


async def test_mdns_discovery_logs_each_outage_once_as_a_warning(controller, caplog):
    ctrl, _ = controller
    server = cast(FakeMatterClient, ctrl._matter_client).server
    server.lose()
    discovering = asyncio.create_task(ctrl._matter_discovery_loop(interval=0.01))
    await asyncio.sleep(0.1)  # some listings fail
    server.restore()
    await asyncio.sleep(0.1)  # reconnected: listed again
    server.lose()
    await asyncio.sleep(0.1)
    discovering.cancel()
    assert [r.levelno for r in caplog.records if "discovery" in r.getMessage()] == [logging.WARNING] * 2


@pytest.fixture
def uninterviewed(monkeypatch) -> MatterNode:
    """A node on the fabric matterjs-server hasn't interviewed yet: no attributes, so no device info."""
    node = MatterNode(replace(load_on_off_light_node().node_data, node_id=9, attributes={}))
    on_fabric = FakeMatterClient.get_nodes
    monkeypatch.setattr(FakeMatterClient, "get_nodes", lambda self: [*on_fabric(self), node])
    return node


async def test_a_node_matterjs_server_hasnt_interviewed_is_left_out_at_start(monkeypatch, uninterviewed):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)
    controller = _matter(build_test_platform())
    await controller.start()
    await _wait(lambda: bool(controller.discoveries))
    assert [d.device_name for d in controller.discoveries.values()] == ["TEST_PRODUCT"]  # the commissionable one
    await controller.stop()


async def test_a_node_matterjs_server_hasnt_interviewed_doesnt_stop_a_pairing(controller, uninterviewed):
    ctrl, output = controller
    await _commission(ctrl)
    assert output.connected_devices


async def test_a_node_a_failed_pairing_commissioned_is_offered_as_it_is_at_once(controller, monkeypatch):
    ctrl, output = controller
    await _wait(lambda: bool(ctrl.discoveries))
    (commissionable,) = ctrl.discoveries.values()

    def broken(*args: Any):
        raise MatterUnexpectedError("mapping failed")  # anything after the commissioning

    monkeypatch.setattr(MatterController, "_get_main_parameter", broken)
    with pytest.raises(MatterUnexpectedError):
        await _pair(ctrl, commissionable, ProvidedCredentials(type=CredentialsType.code, value="20202021"))
    node = cast(FakeMatterClient, ctrl._matter_client).get_node(FAKE_NODE_ID)  # on the fabric now
    (offered,) = ctrl.discoveries.values()
    assert offered.id == ctrl._mapper.device_uuid(node.node_id, node.device_info.productName)
    assert offered.expected_credentials_options == [CredentialsType.none]  # added without a code
    assert output.lost_discoveries == [commissionable.id]  # no longer commissionable


async def test_hand_overs_in_place_leave_no_ended_tasks_behind(controller):
    ctrl, _ = controller
    running = len(ctrl._background_tasks)
    for dataset in ("0e08aaaa", "0e08bbbb", "0e08cccc"):
        before, ctrl.platform = ctrl.platform, replace(ctrl.platform, thread_dataset=dataset)
        assert await ctrl.platform_facts_did_change(before)
        await asyncio.sleep(0.02)
    assert len(ctrl._background_tasks) <= running + 1
