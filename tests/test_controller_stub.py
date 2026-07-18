"""Controller wiring tests against the in-process FakeMatterClient — no matter-server, no MVD.

These run in the default (non-docker) CI. They exercise discovery, pairing + parameter mapping,
commands, and attribute events through the REAL controller and mapper against a faithful canned
on-off-light node. The heavy real-device coverage (all 30 device types, real commissioning) lives
in the dockerized MVD suite, test_controller.py.
"""

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID

import pytest_asyncio
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.testing import build_test_dependencies

from majordom_matter import MatterController
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
    await controller.pair_device(discovery, ProvidedCredentials(type=CredentialsType.code, value="20202021"))
    return controller.dependencies.output.connected_devices[-1]


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
