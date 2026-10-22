"""Main-parameter toggles driven by a state attribute, against the in-process FakeMatterClient.

The canned on-off light's OnOff cluster is swapped for the cluster under test on the same
endpoint, so that cluster's main parameter wins. A tap is simulated the way the Hub does it:
`next_main_parameter_value(stored value, main_cycle)`, then `send_command` with that value.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from chip.clusters.Objects import (
    ClosureControl,
    DoorLock,
    FanControl,
    MediaPlayback,
    OperationalState,
    RvcOperationalState,
    ValveConfigurationAndControl,
    WindowCovering,
)
from majordom_integration_sdk import Endpoint
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.schemas.parameter import ParameterRole, ParameterVisibility, next_main_parameter_value
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_dependencies
from matter_server.client.models.node import MatterNode
from matter_server.common.helpers.util import dataclass_from_dict
from matter_server.common.models import MatterNodeData

from majordom_matter import MatterController
from majordom_matter.exceptions import MatterUnexpectedError
from majordom_matter.model import MatterDevice, MatterDeviceIntegrationData, MatterDeviceState, MatterParameterTypeEnum
from majordom_matter.testing import _FIXTURE, FakeMatterClient

ENDPOINT = 13


def _node_with(cluster_id: int, attributes: dict[int, Any], accepted_commands: list[int]) -> MatterNode:
    """The canned node with its OnOff cluster replaced by `cluster_id` carrying `attributes`."""
    data = json.loads(_FIXTURE.read_text())
    paths = data["attributes"]
    for path in [p for p in paths if p.startswith(f"{ENDPOINT}/6/")]:
        del paths[path]
    prefix = f"{ENDPOINT}/{cluster_id}"
    paths |= {f"{prefix}/{attribute_id}": value for attribute_id, value in attributes.items()}
    paths |= {
        f"{prefix}/65529": accepted_commands,
        f"{prefix}/65531": [*attributes, 65529, 65531, 65532, 65533],
        f"{prefix}/65532": 0,
        f"{prefix}/65533": 1,
    }
    return MatterNode(dataclass_from_dict(MatterNodeData, data))


async def _pair(
    monkeypatch, node: MatterNode
) -> tuple[MatterController, FakeMatterClient, MatterDevice, MatterDeviceState]:
    fake = FakeMatterClient(node=node)
    monkeypatch.setattr("majordom_matter.controller.MatterClient", lambda url, session: fake)
    deps = build_test_dependencies(documents_folder=Path(tempfile.mkdtemp()), integration="Matter").copy(
        links={"matter": Endpoint("matter", "matter-server-ws", "ws://matter-server/ws")}
    )
    controller = MatterController(deps)
    await controller.start()
    async with asyncio.timeout(5):
        while not controller.discoveries:
            await asyncio.sleep(0.02)
    discovery = next(iter(controller.discoveries.values()))
    async with deps.make_device_repository() as repo:
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
    device_id = cast(RecordingControllerOutput, deps.output).connected_devices[-1]
    async with deps.make_device_repository() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id, MatterDeviceState)
    assert device is not None and state is not None
    return controller, fake, device, state


def _main(state: MatterDeviceState):
    assert state.main_parameter is not None, "the device should have a main parameter"
    return state.parameters_dict[state.main_parameter]


async def _tap(controller: MatterController, fake: FakeMatterClient, device: MatterDevice, parameter) -> Any:
    """Send what the Hub sends for a main-parameter tap; return the command that reached the device."""
    value = next_main_parameter_value(parameter.value, parameter.main_cycle)
    sent = fake.sent_commands
    before = len(sent)
    await controller.send_command(
        DeviceCommand(device_id=device.id, parameter_id=parameter.id, value=value), device, parameter
    )
    assert len(sent) == before + 1, "a tap should send exactly one command"
    return sent[-1][2]


TOGGLES = [
    # cluster, state attribute, accepted commands, cycle, current state, command the tap sends
    (DoorLock, DoorLock.Attributes.LockState, [0, 1], {1, 2}, 1, DoorLock.Commands.UnlockDoor()),
    (DoorLock, DoorLock.Attributes.LockState, [0, 1], {1, 2}, 2, DoorLock.Commands.LockDoor()),
    (DoorLock, DoorLock.Attributes.LockState, [0, 1], {1, 2}, 0, DoorLock.Commands.LockDoor()),
    (DoorLock, DoorLock.Attributes.LockState, [0, 1], {1, 2}, 3, DoorLock.Commands.LockDoor()),
    (
        ValveConfigurationAndControl,
        ValveConfigurationAndControl.Attributes.CurrentState,
        [0, 1],
        {0, 1},
        0,
        ValveConfigurationAndControl.Commands.Open(),
    ),
    (
        ValveConfigurationAndControl,
        ValveConfigurationAndControl.Attributes.CurrentState,
        [0, 1],
        {0, 1},
        1,
        ValveConfigurationAndControl.Commands.Close(),
    ),
    (
        ValveConfigurationAndControl,
        ValveConfigurationAndControl.Attributes.CurrentState,
        [0, 1],
        {0, 1},
        2,
        ValveConfigurationAndControl.Commands.Close(),
    ),
    (
        WindowCovering,
        WindowCovering.Attributes.CurrentPositionLiftPercentage,
        [0, 1, 2],
        {0, 100},
        0,
        WindowCovering.Commands.DownOrClose(),
    ),
    (
        WindowCovering,
        WindowCovering.Attributes.CurrentPositionLiftPercentage,
        [0, 1, 2],
        {0, 100},
        100,
        WindowCovering.Commands.UpOrOpen(),
    ),
    (
        WindowCovering,
        WindowCovering.Attributes.CurrentPositionLiftPercentage,
        [0, 1, 2],
        {0, 100},
        40,
        WindowCovering.Commands.UpOrOpen(),
    ),
    (MediaPlayback, MediaPlayback.Attributes.CurrentState, [0, 1, 2], {0, 1}, 0, MediaPlayback.Commands.Pause()),
    (MediaPlayback, MediaPlayback.Attributes.CurrentState, [0, 1, 2], {0, 1}, 1, MediaPlayback.Commands.Play()),
    (MediaPlayback, MediaPlayback.Attributes.CurrentState, [0, 1, 2], {0, 1}, 2, MediaPlayback.Commands.Play()),
    (
        RvcOperationalState,
        RvcOperationalState.Attributes.OperationalState,
        [0, 3],
        {1, 2},
        1,
        RvcOperationalState.Commands.Pause(),
    ),
    (
        RvcOperationalState,
        RvcOperationalState.Attributes.OperationalState,
        [0, 3],
        {1, 2},
        2,
        RvcOperationalState.Commands.Resume(),
    ),
    (
        RvcOperationalState,
        RvcOperationalState.Attributes.OperationalState,
        [0, 3],
        {1, 2},
        0x42,
        RvcOperationalState.Commands.Resume(),
    ),
    (
        OperationalState,
        OperationalState.Attributes.OperationalState,
        [0, 1, 2, 3],
        {1, 2},
        1,
        OperationalState.Commands.Pause(),
    ),
    (
        OperationalState,
        OperationalState.Attributes.OperationalState,
        [0, 1, 2, 3],
        {1, 2},
        2,
        OperationalState.Commands.Resume(),
    ),
]


@pytest.mark.parametrize(("cluster", "attribute", "accepted", "cycle", "current", "expected"), TOGGLES)
async def test_state_attribute_main_parameter_toggles_through_its_command(
    monkeypatch, cluster, attribute, accepted: list[int], cycle: set[int], current: int, expected
):
    controller, fake, device, state = await _pair(
        monkeypatch, _node_with(cluster.id, {attribute.attribute_id: current}, accepted)
    )
    try:
        main = _main(state)
        assert main.integration_data.type is MatterParameterTypeEnum.attribute
        assert (main.integration_data.cluster_id, main.integration_data.attribute_id) == (
            cluster.id,
            attribute.attribute_id,
        )
        # The Hub only forwards commands for control parameters.
        assert main.role is ParameterRole.control and main.visibility is ParameterVisibility.user
        assert main.default_value == cycle and state.can_set_main_parameter(main.id)

        assert await _tap(controller, fake, device, main) == expected
    finally:
        await controller.stop()


async def test_door_lock_toggle_uses_timed_invoke(monkeypatch):
    node = _node_with(DoorLock.id, {DoorLock.Attributes.LockState.attribute_id: 1}, [0, 1])
    controller, fake, device, state = await _pair(monkeypatch, node)
    timeouts: list[int | None] = []

    async def send_device_command(node_id, endpoint_id, command, timed_request_timeout_ms=None, **kwargs):
        timeouts.append(timed_request_timeout_ms)

    monkeypatch.setattr(controller._matter_client, "send_device_command", send_device_command)
    try:
        main = _main(state)
        await controller.send_command(DeviceCommand(device_id=device.id, parameter_id=main.id, value=2), device, main)
        assert timeouts == [1000]
    finally:
        await controller.stop()


async def test_fan_mode_main_parameter_toggles_off_and_on(monkeypatch):
    node = _node_with(FanControl.id, {FanControl.Attributes.FanMode.attribute_id: 0}, [])
    controller, fake, device, state = await _pair(monkeypatch, node)
    try:
        main = _main(state)
        assert main.integration_data.attribute_id == FanControl.Attributes.FanMode.attribute_id
        assert main.default_value == {0, 4}
        value = next_main_parameter_value(main.value, main.main_cycle)
        await controller.send_command(
            DeviceCommand(device_id=device.id, parameter_id=main.id, value=value), device, main
        )
        assert fake.written_attributes[-1][1:] == (f"{ENDPOINT}/{FanControl.id}/0", 4)
    finally:
        await controller.stop()


async def test_idle_operational_state_is_never_started(monkeypatch):
    node = _node_with(OperationalState.id, {OperationalState.Attributes.OperationalState.attribute_id: 0}, [0, 1, 2, 3])
    controller, fake, device, state = await _pair(monkeypatch, node)
    try:
        main = _main(state)
        assert main.integration_data.type is MatterParameterTypeEnum.attribute
        with pytest.raises(MatterUnexpectedError, match="never starts"):
            await _tap(controller, fake, device, main)
        assert fake.sent_commands == []
    finally:
        await controller.stop()


async def test_a_state_the_toggle_cannot_set_is_rejected(monkeypatch):
    node = _node_with(DoorLock.id, {DoorLock.Attributes.LockState.attribute_id: 1}, [0, 1])
    controller, fake, device, state = await _pair(monkeypatch, node)
    try:
        main = _main(state)
        with pytest.raises(MatterUnexpectedError, match="cannot be set"):
            await controller.send_command(
                DeviceCommand(device_id=device.id, parameter_id=main.id, value=3), device, main
            )
        assert fake.sent_commands == []
    finally:
        await controller.stop()


@pytest.mark.parametrize(
    ("cluster", "attributes", "accepted", "expected_command_id"),
    [
        # A cover without position awareness keeps the one-way UpOrOpen.
        (WindowCovering, {0: 0}, [0, 1, 2], WindowCovering.Commands.UpOrOpen.command_id),
        # A player without Pause, a vacuum without Pause keep their one-way command.
        (MediaPlayback, {0: 0}, [0, 2], MediaPlayback.Commands.Play.command_id),
        (RvcOperationalState, {4: 1}, [3], RvcOperationalState.Commands.Resume.command_id),
        # An appliance that can't pause gets no main parameter: Start is never a one-tap.
        (OperationalState, {4: 0}, [1, 2], None),
    ],
)
async def test_unsupported_toggle_falls_back(
    monkeypatch, cluster, attributes: dict[int, Any], accepted: list[int], expected_command_id: int | None
):
    controller, _fake, _device, state = await _pair(monkeypatch, _node_with(cluster.id, attributes, accepted))
    try:
        if expected_command_id is None:
            assert state.main_parameter is None
            return
        main = _main(state)
        assert main.integration_data.type is MatterParameterTypeEnum.command
        assert (main.integration_data.cluster_id, main.integration_data.command_id) == (
            cluster.id,
            expected_command_id,
        )
    finally:
        await controller.stop()


CLOSURE_POSITION = ClosureControl.Enums.TargetPositionEnum


@pytest.mark.parametrize(
    ("main_state", "overall", "expected"),
    [
        (
            0,
            {"0": 0, "1": False},
            ClosureControl.Commands.MoveTo(position=CLOSURE_POSITION(CLOSURE_POSITION.kMoveToFullyOpen)),
        ),
        (
            0,
            {"0": 1, "1": False},
            ClosureControl.Commands.MoveTo(position=CLOSURE_POSITION(CLOSURE_POSITION.kMoveToFullyClosed)),
        ),
        (
            0,
            {"0": 2, "1": False},
            ClosureControl.Commands.MoveTo(position=CLOSURE_POSITION(CLOSURE_POSITION.kMoveToFullyClosed)),
        ),
        (0, None, ClosureControl.Commands.MoveTo(position=CLOSURE_POSITION(CLOSURE_POSITION.kMoveToFullyClosed))),
        (1, {"0": 2, "1": False}, ClosureControl.Commands.Stop()),
        (2, {"0": 0, "1": False}, ClosureControl.Commands.Stop()),
    ],
)
async def test_closure_main_parameter_toggles_by_current_state(
    monkeypatch, main_state: int, overall: dict | None, expected
):
    attributes = {
        ClosureControl.Attributes.MainState.attribute_id: main_state,
        ClosureControl.Attributes.OverallCurrentState.attribute_id: overall,
    }
    controller, fake, device, state = await _pair(monkeypatch, _node_with(ClosureControl.id, attributes, [0, 1]))
    try:
        main = _main(state)
        assert main.integration_data.attribute_id == ClosureControl.Attributes.MainState.attribute_id
        assert main.role is ParameterRole.control and main.visibility is ParameterVisibility.user
        # Structs can't cycle: a one-value button, and the controller picks the command by state.
        assert main.main_cycle == [ClosureControl.Enums.MainStateEnum.kMoving]
        assert await _tap(controller, fake, device, main) == expected
    finally:
        await controller.stop()
