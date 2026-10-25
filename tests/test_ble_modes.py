"""Bluetooth for commissioning: matterjs-server's own adapter when one is bound to it, else the hub's router."""

import asyncio
import json
import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from majordom_integration_sdk import Endpoint
from majordom_integration_sdk.runtime import Runtime
from majordom_integration_sdk.schemas.device import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.testing import (
    FakeBLERouter,
    FakeBLESource,
    RecordingControllerOutput,
    build_test_platform,
)

from majordom_matter import MatterController
from majordom_matter.model import MatterDeviceIntegrationData, MatterDeviceState
from majordom_matter.testing import FakeMatterClient, FakeMatterServer

SERVER = Endpoint("matter", "matter-server", "ws://matter-server")
COMMISSIONABLE = "0000fff6-0000-1000-8000-00805f9b34fb"
HCI0 = Endpoint("ble", "bluez", "hci0", params={"index": "0"})


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr("majordom_matter.controller.MatterClient", partial(FakeMatterClient, server=FakeMatterServer()))
    monkeypatch.setattr("majordom_matter.controller.MatterServerBLEDelegate.run", lambda self: asyncio.sleep(3600))


async def started(router: FakeBLERouter | None, server: Endpoint = SERVER) -> MatterController:
    platform = build_test_platform(Path(tempfile.mkdtemp()), integration="Matter")
    peripherals = {"matter": server} | ({"ble": router.endpoint} if router else {})
    controller = MatterController(platform, peripherals)
    await controller.start()
    return controller


def by_ble(controller: MatterController) -> list[str]:
    return [d.device_name for d in controller.discoveries.values() if d.transport == "BLE"]


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
    output = cast(RecordingControllerOutput, controller.platform.output)

    assert by_ble(controller) == ["Matter device (enter its pairing code)"]
    radio.advertise("AA", local_name="bulb", service_data={COMMISSIONABLE: bytes([0, 0, 0x0F, 0xF1, 0xFF, 0, 0x80, 0])})
    await asyncio.sleep(0.05)
    assert by_ble(controller) == ["Matter device (enter its pairing code)"]  # the router isn't used

    (by_code,) = (d for d in controller.discoveries.values() if d.transport == "BLE")
    async with controller.platform.make_device_repository() as repo:  # what the Hub seeds when pairing starts
        await repo.save(
            MatterDeviceState(
                id=by_code.id,
                name="Bulb",
                room_id=UUID(int=1),
                transport="BLE",
                integration="Matter",
                manufacturer=None,
                parameters=[],
                integration_data=MatterDeviceIntegrationData(node_id=0),
            )  # fmt: skip
        )
    await controller.pair_device(by_code, ProvidedCredentials(type=CredentialsType.code, value="3497-011-2332"))

    assert output.connected_devices
    assert by_code.id in controller.discoveries  # offered again for the next device
    assert [d.id for d in output.received_discoveries].count(by_code.id) == 2
    await controller.stop()
    await router.stop()


async def matterjs_server(tmp_path: Path, monkeypatch, reserved: str | None) -> tuple[dict[str, Any], Endpoint]:
    """matterjs-server's compose as the runtime renders it on a hub with hci0, and the endpoint the controller gets."""
    monkeypatch.setattr(
        "majordom_integration_sdk.runtime.runtime._host_endpoints", lambda: [Endpoint("ip", shared=True), HCI0]
    )
    runtime = Runtime(tmp_path, {"matter": MatterController}, docker=False, scan=False, builtins=[FakeBLERouter()])
    await runtime.reserve("matterjs-server", "ble", reserved)
    peripherals = runtime.peripherals("matter")
    assert peripherals is not None
    return json.loads((tmp_path / "compose.json").read_text())["services"]["matterjs-server"], peripherals.endpoint(
        "matter"
    )


async def test_through_the_router_matterjs_server_proxies_bluetooth_and_stays_off_the_system_bus(tmp_path, monkeypatch):
    service, matter = await matterjs_server(tmp_path, monkeypatch, reserved=None)
    environment = service["environment"]
    assert (environment["BLE_PROXY"], environment["BLUETOOTH_ADAPTER"], environment["NOBLE_BINDINGS"]) == (
        "true",
        "",
        "",
    )
    assert service["volumes"] == [f"{tmp_path}/services/matterjs-server:/data"]
    assert matter.params["adapter"] == ""


async def test_with_an_adapter_reserved_matterjs_server_drives_it_through_bluez(tmp_path, monkeypatch):
    service, matter = await matterjs_server(tmp_path, monkeypatch, reserved="hci0")
    environment = service["environment"]
    assert (environment["BLE_PROXY"], environment["BLUETOOTH_ADAPTER"], environment["NOBLE_BINDINGS"]) == (
        "",
        "0",
        "dbus",
    )
    assert service["volumes"] == [f"{tmp_path}/services/matterjs-server:/data", "/run/dbus:/run/dbus:ro"]
    assert matter.params["adapter"] == "hci0"
