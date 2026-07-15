import asyncio
import pytest
import random
import subprocess
import time

from uuid import UUID
from aiohttp import ClientSession
from matter_server.client import MatterClient

from majordom_hub.config import matter_server_url
from majordom_hub.services.controller.matter.model import MatterDevice, MatterDeviceState, MatterDeviceIntegrationData, MatterParameterState
from majordom_hub.repository.device_repository import DeviceRepository
from majordom_hub.utils.database import create_async_session


async def create_matter_device(id: UUID, node_id: int, room_id: UUID):
    async with  create_async_session() as session:
        device_repo = DeviceRepository(session)
        await device_repo.save(
            MatterDevice(
                id=id,
                integration_data=MatterDeviceIntegrationData(node_id=node_id),
                room_id=room_id,
                name="Test",
                transport="IP",
                integration="Matter",
                manufacturer="Vendor 0xfff1"
            ),
            id
        )
        device = await device_repo.state(id, MatterDeviceState)
        assert device
        await device_repo.save(device, id)

async def get_device_integration_data(device_id: UUID) -> dict:
    async with  create_async_session() as session:
        device_repo = DeviceRepository(session)
        device = await device_repo.get(device_id, MatterDevice)
        assert device

        return device.integration_data.dict()

async def pair_mvd(code: str="20202021"):
    session = ClientSession()
    app = MatterClient(matter_server_url, session)
    try:
        await app.connect()
        event = asyncio.Event()
        asyncio.create_task(app.start_listening(init_ready=event))
        await event.wait()
        await app.commission_on_network(20202021)
        node_id = app.get_nodes()[0].node_id
        return int(node_id)
    except Exception as e:
        print(e)
    finally:
        await app.disconnect()
        await session.close()

async def unpair_mvd(node_id: int | None = None):
    session = ClientSession()
    app = MatterClient(matter_server_url, session)

    try:
        await app.connect()
        event = asyncio.Event()
        asyncio.create_task(app.start_listening(init_ready=event))
        await event.wait()
        if node_id:
            await app.remove_node(node_id)
        else:
            for node in app.get_nodes():
                await app.remove_node(node.node_id)
    except Exception as e:
        print(str(e))
    finally:
        await app.disconnect()
        await session.close()

async def wait_for_discovery(async_client, headers, timeout: float = 4.0, interval: float = 0.5) -> dict:
    """Poll discovery endpoint until at least one device appears or timeout is reached."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await async_client.get('/v1/api/device/discoveries', headers=headers)
        if r.status_code == 200 and r.json():
            return r.json()
        await asyncio.sleep(interval)
    pytest.fail(f"No devices discovered within {timeout}s")

def generate_value(field: dict):
    data_type = field.get("data_type")
    valid_values = field.get("valid_values")
    min_value = field.get("min_value")
    max_value = field.get("max_value")

    if valid_values:
        return int(random.choice(list(valid_values.keys())))

    if data_type in ("integer", "enum"):
        lo = int(min_value) if min_value is not None else 1
        hi = int(max_value) if max_value is not None else 1
        return random.randint(lo, hi)

    if data_type == "decimal":
        lo = float(min_value) if min_value is not None else 1.0
        hi = float(max_value) if max_value is not None else 1.0
        return random.uniform(lo, hi)

    if data_type == "bool":
        return random.choice([True, False])

    if data_type == "string":
        return "test"

    if data_type == "none":
        return None

    # unknown data_type without valid_values/min/max — should not happen for control attributes;
    # fail loudly instead of silently sending None
    raise ValueError(f"Cannot generate test value for field {field.get('name')} with data_type={data_type}")

def flush_ble_cache() -> None:
    """Flush BlueZ's device cache and restart bluetoothd.

    Matter's BLE discovery listens for BlueZ's ``InterfacesAdded`` ("new device")
    D-Bus signal. A device advertising a *static* BLE address that is already in
    BlueZ's cache never re-fires that signal, so the controller never discovers it
    (see majordom_hub/services/controller/matter/readme.md). Clearing the cache makes
    the device look new again on its next advert.

    Best-effort: needs host ``bluetoothctl`` and permission to restart bluetooth, so it
    silently no-ops where those aren't available (e.g. inside an unprivileged container).
    The reliable place to run it is the host runner — see the matter-hardware workflow.

    IMPORTANT: do NOT run any BLE scan between calling this and commissioning; a scan
    re-caches the device and re-consumes the ``InterfacesAdded`` signal.
    """
    try:
        listed = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=10)
        for line in listed.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                subprocess.run(["bluetoothctl", "remove", parts[1]], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "restart", "bluetooth"], capture_output=True, timeout=20)
        time.sleep(2)  # let bluetoothd come back up before the device advertises
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def flatten_exception_group(exception) -> list[Exception]:
    exceptions = list()
    if exception.exceptions:
        for exc in exception.exceptions:
            if hasattr(exc, "exceptions"):
                exceptions.extend(flatten_exception_group(exc))
            else:
                exceptions.append(exc)
    return exceptions