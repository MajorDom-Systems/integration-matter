"""Bluetooth for commissioning: the hub's router, or matterjs-server's own adapter when no router holds the adapters."""

import asyncio
import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from majordom_integration_sdk import AbstractController, Discovery, Endpoint, PeripheralChange, Peripherals
from majordom_integration_sdk.runtime import MemoryDisabledStore, PeripheralState, Runtime
from majordom_integration_sdk.schemas.device import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.testing import (
    FakeBLERouter,
    FakeBLESource,
    FakeCompose,
    FakePeripheral,
    RecordingControllerOutput,
    RecordingRuntimeOutput,
    build_test_platform,
)

import majordom_matter
from majordom_matter import MatterController
from majordom_matter.model import MatterDeviceIntegrationData, MatterDeviceState
from majordom_matter.testing import FakeMatterClient, FakeMatterServer

SERVER = Endpoint("matter", "matter-server", "ws://matter-server")
COMMISSIONABLE = "0000fff6-0000-1000-8000-00805f9b34fb"


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=FakeMatterServer()))
    monkeypatch.setattr("majordom_matter.controller.MatterServerBLEDelegate.run", lambda self: asyncio.sleep(3600))


async def started(router: FakeBLERouter | None, server: Endpoint = SERVER) -> MatterController:
    controller = MatterController(build_test_platform())
    controller.peripherals = Peripherals({"matter": server} | ({"ble": router.endpoint} if router else {}))
    controller.output = RecordingControllerOutput()
    await controller.start()
    return controller


def by_ble(controller: MatterController) -> list[str]:
    return [d.device_name for d in controller.discoveries.values() if d.transport == "BLE"]


async def seed_the_hubs_row(controller: MatterController, discovery: Discovery):
    """What the Hub saves under the discovery's id when pairing starts."""
    async with controller.platform.device_repository(controller) as repo:
        await repo.save(
            MatterDeviceState(
                id=discovery.id,
                name="Bulb",
                room_id=UUID(int=1),
                transport="BLE",
                integration="matter",
                manufacturer=None,
                parameters=[],
                integration_data=MatterDeviceIntegrationData(node_id=0),
            )  # fmt: skip
        )


async def test_through_the_router_it_lists_the_devices_waiting_over_bluetooth(fake_server):
    radio = FakeBLESource("hci0")
    router = FakeBLERouter([radio])
    await router.start()
    controller = await started(router)

    # opcode 0, discriminator 0xF00, vendor 0xFFF1, product 0x8000
    radio.advertise("AA", local_name="bulb", service_data={COMMISSIONABLE: bytes([0, 0, 0x0F, 0xF1, 0xFF, 0, 0x80, 0])})
    await asyncio.sleep(0.05)
    assert by_ble(controller) == ["bulb"]
    await controller.stop()
    await router.stop()


async def test_without_the_router_or_an_adapter_it_hears_no_bluetooth(fake_server):
    controller = await started(None, replace(SERVER, params={"adapter": ""}))  # our matterjs-server, no adapter bound
    assert by_ble(controller) == []
    await controller.stop()


async def test_with_an_adapter_of_its_own_it_takes_a_pairing_code_for_matterjs_server_to_find_the_device(fake_server):
    radio = FakeBLESource("hci0")
    router = FakeBLERouter([radio])
    await router.start()
    controller = await started(router, replace(SERVER, params={"adapter": "hci1"}))
    output = cast(RecordingControllerOutput, controller.output)

    assert by_ble(controller) == ["Matter device (enter its pairing code)"]
    radio.advertise("AA", local_name="bulb", service_data={COMMISSIONABLE: bytes([0, 0, 0x0F, 0xF1, 0xFF, 0, 0x80, 0])})
    await asyncio.sleep(0.05)
    assert by_ble(controller) == ["Matter device (enter its pairing code)"]  # the router isn't used

    (by_code,) = (d for d in controller.discoveries.values() if d.transport == "BLE")
    await seed_the_hubs_row(controller, by_code)
    await controller.pair_device(by_code, ProvidedCredentials(type=CredentialsType.code, value="3497-011-2332"))

    assert output.connected_devices
    assert by_code.id in controller.discoveries  # offered again for the next device
    assert [d.id for d in output.received_discoveries].count(by_code.id) == 2
    await controller.stop()
    await router.stop()


async def test_the_pairing_code_is_offered_again_once_the_device_it_paired_is_saved(fake_server):
    controller = await started(None, replace(SERVER, params={"adapter": "hci0"}))
    is_row_there: list[bool] = []  # at each offer: the Hub's row under its id, which pairing moves to the device's

    class Output(RecordingControllerOutput):
        async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery):
            if discovery.transport == "BLE":
                async with controller.platform.device_repository(controller) as repo:
                    is_row_there.append(await repo.get(discovery.id) is not None)

    controller.output = Output()
    (by_code,) = (d for d in controller.discoveries.values() if d.transport == "BLE")
    await seed_the_hubs_row(controller, by_code)
    await controller.pair_device(by_code, ProvidedCredentials(type=CredentialsType.code, value="3497-011-2332"))
    assert is_row_there == [False]  # a pairing started from it at once would save its row under a taken id
    await controller.stop()


async def test_with_an_adapter_of_its_own_it_keeps_running_as_the_router_it_doesnt_use_goes(fake_server):
    router = FakeBLERouter([FakeBLESource("hci1")])
    await router.start()
    controller = await started(router, replace(SERVER, params={"adapter": "hci0"}))
    controller.peripherals = Peripherals({"matter": controller.peripherals.endpoint("matter")})
    assert await controller.needed_peripheral_did_change(PeripheralChange("ble", router.endpoint, None))
    await controller.stop()
    await router.stop()


async def hub(
    tmp_path: Path, router: FakeBLERouter, *, is_router_disabled: bool = False
) -> tuple[Runtime, MatterController]:
    """A hub with hci0 running Matter, once its controller runs."""
    hci0 = Endpoint("ble", "bluez", "hci0", params={"index": "0"}, instance=FakeBLESource("hci0"))
    host = FakePeripheral("host", provides=[Endpoint("ip", is_shared=True), hci0])
    exported = majordom_matter.peripherals(build_test_platform())
    (controller,) = (p for p in exported if isinstance(p, MatterController))
    runtime = Runtime(
        tmp_path,
        [host, FakeCompose(), router, *exported],
        output=RecordingRuntimeOutput(),
        disabled=MemoryDisabledStore([router.key] if is_router_disabled else []),
    )
    await runtime.start()
    async with asyncio.timeout(2):
        while runtime.status(controller).state != PeripheralState.RUNNING:
            await asyncio.sleep(0.01)
    return runtime, controller


async def matterjs_server(tmp_path: Path, *, is_router_disabled: bool) -> tuple[dict[str, Any], Endpoint]:
    """matterjs-server's compose as the runtime renders it on a hub with hci0, and the endpoint the controller gets."""
    runtime, controller = await hub(tmp_path, FakeBLERouter(), is_router_disabled=is_router_disabled)
    compose = json.loads((tmp_path / "peripherals" / "compose" / "compose.json").read_text())
    await runtime.stop()
    return compose["services"]["matterjs-server"], controller.peripherals.endpoint("matter")


async def test_through_the_router_matterjs_server_proxies_bluetooth_and_stays_off_the_system_bus(tmp_path, fake_server):
    service, matter = await matterjs_server(tmp_path, is_router_disabled=False)
    environment = service["environment"]
    assert (environment["BLE_PROXY"], environment["BLUETOOTH_ADAPTER"], environment["NOBLE_BINDINGS"]) == (
        "true",
        "",
        "",
    )
    assert service["volumes"] == [f"{tmp_path}/peripherals/matterjs-server:/data"]
    assert matter.params["adapter"] == ""


async def test_without_the_router_matterjs_server_drives_an_adapter_through_bluez(tmp_path, fake_server):
    service, matter = await matterjs_server(tmp_path, is_router_disabled=True)
    environment = service["environment"]
    assert (environment["BLE_PROXY"], environment["BLUETOOTH_ADAPTER"], environment["NOBLE_BINDINGS"]) == (
        "",
        "0",
        "dbus",
    )
    assert service["volumes"] == [f"{tmp_path}/peripherals/matterjs-server:/data", "/run/dbus:/run/dbus:ro"]
    assert matter.params["adapter"] == "hci0"


class LateRouter(FakeBLERouter):
    """A router that comes up only once let: after Matter runs."""

    def __init__(self, sources: list[FakeBLESource]):
        super().__init__(sources)
        self.is_let = asyncio.Event()

    async def start(self):
        await self.is_let.wait()
        await super().start()


async def test_a_router_coming_up_after_matter_is_taken_without_a_restart(tmp_path, fake_server):
    radio = FakeBLESource("hci1")
    router = LateRouter([radio])
    runtime, controller = await hub(tmp_path, router)
    router.is_let.set()
    async with asyncio.timeout(2):
        while "ble" not in controller.peripherals.bound:
            await asyncio.sleep(0.01)

    radio.advertise("AA", local_name="bulb", service_data={COMMISSIONABLE: bytes([0, 0, 0x0F, 0xF1, 0xFF, 0, 0x80, 0])})
    await asyncio.sleep(0.05)
    assert by_ble(controller) == ["bulb"]
    statuses = cast(RecordingRuntimeOutput, runtime.output).statuses
    assert [status.state for key, status in statuses if key == "matter"].count(PeripheralState.STARTING) == 1
    await runtime.stop()
