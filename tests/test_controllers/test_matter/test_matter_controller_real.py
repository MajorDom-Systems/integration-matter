"""
Real-hardware Matter test — commissions a physical Thread bulb over BLE→Thread through the
hub and verifies it physically via the IoT-cage photoresistor. Mirrors
tests/test_controllers/test_zigbee/test_zigbee_controller_real.py, adapted for Matter's separate
discovery/commission step, its matter-server dependency, and its Thread border-router dependency.

Deselected by default (the `real_iot_device` marker). Runs on the self-hosted lab-pi5 runner via
the matter-hardware workflow. Preconditions on the runner (see the integration readme):
  * OTBR up as Thread leader on a freshly-pulled openthread/border-router image, REST on :8081.
  * matterjs-server up on :5580 (the hub's matter_server_url).
  * The IKEA KAJPLATS bulb wired to IoT-cage slot `_LAB_MATTER_DEVICE_IDX`, plus the cage Arduino.

The DUT is the IKEA KAJPLATS: it opens a ~5-min Matter BLE pairing window on any power-on (no
factory reset needed) and, unlike the Nanoleaf (firmware 4.1.3 wedges its Thread RX after one
Invoke), it takes repeated commands reliably — so it's the device CI gates on.
"""

import asyncio
import time
import warnings
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect

from majordom_hub import models
from majordom_hub.config import VIRTUAL_DISABLED_SERVICES, Settings
from majordom_hub.coordinator import Coordinator
from majordom_hub.providers.paths import Paths
from tests.hardware.iot_cage.threaded import ThreadedIotRpc
from tests.test_controllers.test_matter import helper

pytestmark = [pytest.mark.real_iot_device, pytest.mark.asyncio(loop_scope="session")]

cloud_key = Paths.data.keys.cloud.read_text()

# The IKEA KAJPLATS Matter pairing code (its 4-bit short discriminator 0xA matches the 0xA3B in
# its 0xFFF6 advert). A plain power-on opens its pairing window; no factory reset needed.
_IKEA_PAIRING_CODE = "2455-383-5850"

# IoT-cage slot wired to the Matter DUT. Slot 2 also carries the A0 photoresistor used for physical
# verification; the Nanoleaf (slot 3) shares that one sensor, so the fixtures power everything else
# off first to isolate the DUT's light on it.
_LAB_MATTER_DEVICE_IDX = 2

# Cage Arduino serial port. Default to the STABLE by-id path, not /dev/ttyUSBn — USB enumeration on
# the Pi reshuffles across reboots/replugs (the CH340 has been ttyUSB0 and ttyUSB2), and pointing at
# the wrong node silently talks to the SkyConnect or Z-Wave stick instead.
_LAB_IOT_CAGE_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"

# Seconds to let the DUT boot and start advertising over BLE after power-on.
_DUT_BOOT_S = 12.0
# Minimum photoresistor delta between the bulb's On and Off levels to call the physical check passed
# (observed On≈53 / Off≈5 on this rig; 20 is a comfortable margin against ambient drift).
_SENSOR_MIN_DELTA = 20

# Populated by test_discovery_and_pairing and consumed by the later ordered tests (one real device,
# paired once, reused across this session — hence module state instead of re-pairing per test).
_state: dict = {}


# ---------------------------------------------------------------------------
# Session-scoped fixtures — local to this module so the MVD-based tests in this
# same directory keep their existing function-scoped/per-test-cleared behavior.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_db():
    """No-op override: the real device stays paired across this whole test session."""


@pytest.fixture(scope="session")
def clear_majordom_db():
    db_url = "sqlite:///" + str(Paths.data.db)
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    for i in range(5):
        try:
            models.Base.metadata.drop_all(bind=engine)
            break
        except OperationalError as e:
            if "database is locked" in str(e):
                time.sleep(0.1)
            else:
                raise
    else:
        raise RuntimeError("Stalled database session")
    models.Base.metadata.create_all(bind=engine)


@pytest.fixture(scope="session")
def credentials_repo_mock_matter():
    with patch("majordom_hub.repository.credentials_repository.CredentialsRepository._write_file", new_callable=Mock):
        yield


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def cloud_service_mock_matter():
    with (
        patch("majordom_hub.coordinator.CloudService.start", new_callable=AsyncMock),
        patch("majordom_hub.coordinator.CloudService.fetch_all", new_callable=AsyncMock),
        patch("majordom_hub.coordinator.CloudService.send_message", new_callable=AsyncMock) as mock,
    ):
        yield mock


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def coordinator(cloud_service_mock_matter, credentials_repo_mock_matter, clear_majordom_db):
    with patch("majordom_hub.coordinator.ServerService.start", new_callable=AsyncMock):
        # Enable the shared BLE scanner too — the IKEA is BLE-only until it joins Thread, so the
        # controller discovers it via BLEDiscoveryService (not the mDNS discover loop).
        c = Coordinator(
            settings=Settings(disable_services=VIRTUAL_DISABLED_SERVICES - {"MatterController", "BLEDiscoveryService"})
        )
        await c.start(wait_forever=False)
        yield c
        await c.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def async_client(coordinator):
    async with AsyncClient(
        transport=ASGITransport(app=coordinator.server_service.app), base_url="http://testserver"
    ) as client:
        yield client


@pytest.fixture(scope="session")
def get_user_bearer():
    return lambda id: {
        "Authorization": "Bearer "
        + jwt.encode(
            {"role": "access", "user_id": id.hex if isinstance(id, UUID) else id, "is_admin": False, "exp": time.time() + 3600},
            cloud_key,
            algorithm="RS256",
        )
    }


@pytest.fixture(scope="session")
def async_client_ws_connect(coordinator, get_user_bearer):
    @asynccontextmanager
    async def _connect(user_id: UUID, timeout: float = 1.0, raise_timeout: bool = False):
        try:
            async with asyncio.timeout(timeout):
                async with AsyncClient(
                    transport=ASGIWebSocketTransport(app=coordinator.server_service.app), base_url="ws://testserver"
                ) as client:
                    async with aconnect_ws("/v1/ws/user", client, headers=get_user_bearer(user_id)) as ws:
                        yield ws
        except asyncio.TimeoutError:
            if raise_timeout:
                raise AssertionError("WebSocket connection timed out. Not satisfactory message received within timeout")
        except WebSocketDisconnect as e:
            assert e.code == 1000, f"Unexpected WebSocket disconnect code: {e.code}"

    return _connect


@pytest.fixture(scope="session")
def matter_device_idx(request: pytest.FixtureRequest) -> int:
    v = request.config.getoption("--matter-device-idx")
    return int(v) if v is not None else _LAB_MATTER_DEVICE_IDX


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def iot_cage(request: pytest.FixtureRequest) -> AsyncGenerator[ThreadedIotRpc, None]:
    port: str = request.config.getoption("--iot-cage-port") or _LAB_IOT_CAGE_PORT
    cage = ThreadedIotRpc(port=port, timeout=8.0)
    await cage.connect()
    try:
        yield cage
    finally:
        try:
            await cage.all_off()
        except Exception:
            pass
        await cage.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def thread_provisioned(coordinator):
    """Hand the Matter server the Thread operational dataset before any commissioning.

    The hub's MatterController only calls commission_with_code — it never forms or forwards the
    Thread network — so the server must already hold the dataset the device will be joined to. We
    read the live dataset from the running OTBR (REST) and push it via set_thread_dataset.
    """
    dataset = await helper.fetch_otbr_dataset()
    assert dataset and all(ch in "0123456789abcdefABCDEF" for ch in dataset), f"bad OTBR dataset: {dataset!r}"
    await helper.set_thread_dataset(dataset)
    return dataset


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def power_on_and_settle(thread_provisioned, iot_cage: ThreadedIotRpc, matter_device_idx: int):
    """Isolate the shared sensor to the DUT, flush BLE, power the DUT on, and let it advertise."""
    # Everything off first so the one shared photoresistor sees only the DUT's light.
    await iot_cage.all_off()
    await asyncio.sleep(1)
    # Flush BlueZ's cache so the controller's InterfacesAdded-based discovery re-fires for the IKEA's
    # STATIC BLE address when it re-advertises on power-on. Must be the last BLE-touching step before
    # discovery/commission — do not scan in between. (Best-effort/host-only; see helper.flush_ble_cache.)
    helper.flush_ble_cache()
    await iot_cage.power(matter_device_idx, True)
    await asyncio.sleep(_DUT_BOOT_S)  # boot + BLE advert; opens the IKEA's ~5-min pairing window


# ---------------------------------------------------------------------------
# Tests (ordered: pairing populates _state for the control/unpair tests)
# ---------------------------------------------------------------------------


async def test_discovery_and_pairing(power_on_and_settle, async_client, async_client_ws_connect, crud, get_user_bearer):
    """Discover the powered-on IKEA over BLE and commission it BLE→Thread through the hub."""
    user = await crud.create_user()

    # Only the DUT is powered, so it should be the sole discovery. Generous timeout: BLE discovery +
    # the hub's 5s discovery-loop cadence.
    # Poll until the IKEA's BLE commissionable discovery appears (VID 0x117C = 4476). Other adverts
    # (e.g. a stale mDNS entry from another bulb) may be present, so select the IKEA specifically.
    async def _find_ikea() -> str | None:
        r = await async_client.get("/v1/api/device/discoveries", headers=get_user_bearer(user.id))
        if r.status_code != 200:
            return None
        for did, d in r.json().items():
            if d.get("transport") == "BLE" and "4476" in (d.get("device_manufacturer") or ""):
                return did
        return None

    deadline = asyncio.get_event_loop().time() + 120
    discovery_id = None
    while asyncio.get_event_loop().time() < deadline:
        discovery_id = await _find_ikea()
        if discovery_id:
            break
        await asyncio.sleep(1)
    assert discovery_id, "IKEA BLE commissionable discovery did not appear within 120s"

    room = await crud.create_room()
    data = {
        "name": "IKEA KAJPLATS",
        "note": "hardware test",
        "icon": "bulb",
        "category": "light",
        "room_id": room.id.hex,
        "discovery_id": discovery_id,
        "credentials": {"type": "code", "value": _IKEA_PAIRING_CODE},
    }

    # Commissioning (BLE→PASE→armFailSafe→scanNetworks→AddThreadNetwork→connectNetwork→CASE) can take
    # a few minutes; wait for the connect event on the user WS.
    async with async_client_ws_connect(user.id, timeout=240) as ws:
        r = await async_client.post("/v1/api/device", json=data, headers=get_user_bearer(user.id))
        assert r.status_code == 200 and r.json().get("name") == data["name"], r.json()
        paired_device_id = r.json()["id"]
        while True:
            message = await ws.receive_json()
            if message["type"] == "majordom_did_connect_device":
                break
    assert message["data"] == paired_device_id

    # Resolve the OnOff attribute parameter (cluster 6, attribute 0) for the control/verify test.
    r = await async_client.get(f"/v1/api/device/{paired_device_id}", headers=get_user_bearer(user.id))
    assert r.status_code == 200, r.json()
    onoff_param_id = next(
        p["id"]
        for p in r.json()["parameters"]
        if p["integration_data"].get("type") == "attribute"
        and p["integration_data"].get("cluster_id") == 6
        and p["integration_data"].get("attribute_id") == 0
    )
    _state["device_id"] = paired_device_id
    _state["onoff_param_id"] = onoff_param_id
    _state["discovery_id"] = discovery_id


async def test_discovery_paired(async_client, crud, get_user_bearer):
    """After pairing, the device is a managed device and its discovery was consumed.

    Note: we don't assert the BLE advert disappears — a real commissionable bulb rotates its BLE
    address and keeps beaconing until its window closes, so the continuous scanner may still see it.
    What matters is that pairing produced a managed device.
    """
    device_id = _state.get("device_id")
    assert device_id, "pairing test must run first"
    user = await crud.create_user()
    r = await async_client.get(f"/v1/api/device/{device_id}", headers=get_user_bearer(user.id))
    assert r.status_code == 200 and r.json().get("id") == device_id, r.json()


async def test_control_onoff(
    async_client, async_client_ws_connect, crud, get_user_bearer, iot_cage: ThreadedIotRpc, matter_device_idx: int
):
    """Drive OnOff over Matter and confirm the bulb physically changes via the photoresistor.

    Uses the OnOff cluster's On/Off *commands* — the OnOff attribute (6/0) is read-only on real
    devices (only virtual MVDs accept writing it), so it must be toggled by command.
    """
    device_id = _state.get("device_id")
    assert device_id, "pairing test must run first"
    user = await crud.create_user()

    r = await async_client.get(f"/v1/api/device/{device_id}", headers=get_user_bearer(user.id))
    assert r.status_code == 200, r.json()
    params = r.json()["parameters"]

    def command_id(name: str):
        return next(
            (
                p["id"]
                for p in params
                if p["integration_data"].get("type") == "command"
                and p["integration_data"].get("cluster_id") == 6
                and p["name"] == name
            ),
            None,
        )

    off_id, on_id = command_id("Off"), command_id("On")
    assert off_id and on_id, f"On/Off commands not exposed; parameters: {[p['name'] for p in params]}"

    await iot_cage.monitor(True)

    async def invoke(param_id):
        async with async_client_ws_connect(user.id, timeout=20) as ws:
            await ws.send_json(
                {"type": "device_command", "data": {"device_id": str(device_id), "parameter_id": str(param_id), "value": None}}
            )
            async with asyncio.timeout(15):
                while True:
                    message = await ws.receive_json()
                    # The user WS also carries connect/discovery notifications — skip all but our result.
                    if message["type"] == "majordom_did_receive_event":
                        return

    # The bulb starts ON (just powered). Off → sensor should fall; On → sensor should rise back.
    iot_cage.clear_events(matter_device_idx)
    await invoke(off_id)
    await asyncio.sleep(2)
    off_events = [e.value for e in iot_cage.get_events(matter_device_idx)]

    iot_cage.clear_events(matter_device_idx)
    await invoke(on_id)
    await asyncio.sleep(2)
    on_events = [e.value for e in iot_cage.get_events(matter_device_idx)]

    await iot_cage.monitor(False)

    assert off_events, "no photoresistor reading after Off — is the DUT the only bulb powered?"
    assert on_events, "no photoresistor reading after On"
    off_level, on_level = min(off_events), max(on_events)
    assert on_level - off_level >= _SENSOR_MIN_DELTA, (
        f"photoresistor did not track OnOff (off={off_events}, on={on_events}); "
        f"delta {on_level - off_level} < {_SENSOR_MIN_DELTA}"
    )


async def test_unpair(async_client, crud, get_user_bearer):
    """Remove the device; this is also the session's cleanup of the real commissioning."""
    device_id = _state.get("device_id")
    assert device_id, "pairing test must run first"
    user = await crud.create_user()
    r = await async_client.delete(f"/v1/api/device/{device_id}", headers=get_user_bearer(user.id))
    assert r.status_code == 200, r.json()
    _state.pop("device_id", None)
