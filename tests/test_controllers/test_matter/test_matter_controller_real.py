"""
Real-hardware Matter test — mirrors tests/test_controllers/test_zigbee/test_zigbee_controller_real.py.

DRAFT: this file is not runnable as-is. It needs a real Matter device paired once
in the faraday cage so the placeholders below can be filled in with actual ids —
see the "TODO(hardware)" markers. Everything else (fixture shapes, test flow) follows
the Zigbee reference file, adapted for Matter's separate discovery/commissioning step
and its matter-server dependency.

Unlike the MVD-based tests in this directory (test_matter_controller.py, using the
function-scoped `start_mvd` fixtures from conftest.py), these tests run once per
session against one real, already-wired device — hence the local fixture overrides
below instead of reusing conftest.py's function-scoped ones.
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

pytestmark = [pytest.mark.real_iot_device, pytest.mark.asyncio(loop_scope="session")]

cloud_key = Paths.data.keys.cloud.read_text()

# TODO(hardware): pair a real Matter device once in the cage (e.g. via the CLI —
# see the `pair`/`devices`/`device` commands added to services/cli.py), then fill
# these in from what it reports. _DEVICE_ID only needs to be known after pairing;
# discovery id isn't fixed here because Matter's discovery id (mDNS instance_name-
# derived) is generally not stable/predictable ahead of time the way Zigbee's
# IEEE-derived one is — test_discovery_and_pairing below discovers it at runtime.
_DEVICE_ID = "TODO-fill-in-after-first-real-pairing"
_PARAM_MAIN_ID = "TODO-fill-in-after-first-real-pairing"  # the device's main_parameter (e.g. OnOff toggle)

# lab-pi5 IoT cage slot wired to the Matter DUT — see test_zigbee/conftest.py's
# port map comment for the other slots on the same cage.
_LAB_MATTER_DEVICE_IDX = 1  # TODO(hardware): confirm/adjust once the DUT is wired


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
        c = Coordinator(settings=Settings(disable_services=VIRTUAL_DISABLED_SERVICES - {"MatterController"}))
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
    # Same cage as Zigbee's, different slot (see matter_device_idx) — see
    # test_zigbee/conftest.py's port-map comment for the shared serial port default.
    port: str = request.config.getoption("--iot-cage-port") or "/dev/ttyUSB0"
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
async def power_on_and_settle(iot_cage: ThreadedIotRpc, matter_device_idx: int):
    """Power on the device and give it time to boot and start advertising over mDNS/BLE."""
    await iot_cage.power(matter_device_idx, False)
    await asyncio.sleep(1)
    await iot_cage.power(matter_device_idx, True)
    await asyncio.sleep(10)  # TODO(hardware): tune to the real DUT's actual boot time


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_discovery_and_pairing(power_on_and_settle, async_client, async_client_ws_connect, crud, get_user_bearer):
    """
    Matter specifics vs Zigbee: there IS a separate discovery step (mDNS/BLE advertisement)
    before pairing, and pairing needs credentials (QR/pairing code), unlike Zigbee's
    join-on-power-on flow. This only checks discovery → pair → connect end to end —
    not coverage of attributes/commands, that's what test_matter_controller.py is for.
    """
    user = await crud.create_user()

    async with async_client_ws_connect(user.id, timeout=30) as ws:
        r = await async_client.get("v1/api/device/discoveries", headers=get_user_bearer(user.id))
        assert r.status_code == 200, r.json()
        if not r.json():
            while True:
                message = await ws.receive_json()
                if message["type"] == "majordom_did_discover_discovery":
                    break

    r = await async_client.get("v1/api/device/discoveries", headers=get_user_bearer(user.id))
    assert r.status_code == 200 and r.json(), r.json()
    discovery_id = next(iter(r.json()))

    room = await crud.create_room()
    data = {
        "name": "Test Device",
        "note": "test note",
        "icon": "test icon",
        "category": "test category",
        "room_id": room.id.hex,
        "discovery_id": discovery_id,
        "credentials": "20202021",  # TODO(hardware): the DUT's actual pairing code
    }

    async with async_client_ws_connect(user.id, timeout=60) as ws:
        r = await async_client.post("/v1/api/device", json=data, headers=get_user_bearer(user.id))
        assert r.status_code == 200 and r.json() and r.json()["name"] == data["name"], r.json()
        paired_device_id = r.json()["id"]
        while True:
            message = await ws.receive_json()
            if message["type"] == "majordom_did_connect_device":
                break
    assert message["data"] == paired_device_id
    # TODO(hardware): once known, replace this assert with `assert paired_device_id == _DEVICE_ID`
    # (and use _DEVICE_ID directly, like the Zigbee reference does) so re-runs verify the
    # device's id stayed stable rather than trusting whatever this run happened to pair.


async def test_discovery_paired(async_client, crud, get_user_bearer):
    # assumes device is already paired and reachable after test_discovery_and_pairing
    user = await crud.create_user()
    r = await async_client.get("v1/api/device/discoveries", headers=get_user_bearer(user.id))
    assert r.status_code == 200 and r.json() == {}, r.json()


async def test_control_main_parameter(crud, async_client_ws_connect, iot_cage: ThreadedIotRpc | None, matter_device_idx: int):
    # assumes device is already paired and reachable after test_discovery_and_pairing
    """Toggle the device's main parameter; if the cage is present, verify the sensor slot changed."""
    if iot_cage is not None:
        await iot_cage.monitor(True)
        iot_cage.clear_events(matter_device_idx)

    user = await crud.create_user()
    command = {
        "type": "device_command",
        "data": {
            "device_id": _DEVICE_ID,
            "parameter_id": _PARAM_MAIN_ID,
            "value": None,
        },
    }
    message = None
    async with async_client_ws_connect(user.id, timeout=10) as ws:
        await ws.send_json(command)
        while True:
            message = await ws.receive_json()
            if message["type"] == "majordom_did_receive_event":
                break
    assert message and message.get("type") == "majordom_did_receive_event", message

    if iot_cage is not None:
        await asyncio.sleep(0.5)  # let sensor event propagate
        events = iot_cage.get_events(matter_device_idx)
        assert events, f"Expected a sensor event on cage slot {matter_device_idx} after toggle command"
        await iot_cage.monitor(False)
    else:
        warnings.warn("iot_cage is None, skipping sensor event verification")


async def test_unpair(async_client, crud, get_user_bearer, iot_cage: ThreadedIotRpc | None, matter_device_idx: int):
    user = await crud.create_user()
    r = await async_client.delete(f"/v1/api/device/{_DEVICE_ID}", headers=get_user_bearer(user.id))
    assert r.status_code == 200, r.json()
