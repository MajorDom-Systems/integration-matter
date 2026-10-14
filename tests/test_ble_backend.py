"""matter-server commissions over Bluetooth through the hub's radios (BLE proxy protocol v1)."""

import asyncio
import struct

import pytest
from aiohttp import ClientSession, web
from majordom_integration_sdk.discovery import BLEService, GattCharacteristic, GattService
from majordom_integration_sdk.testing import FakeBLESource

from majordom_matter.ble_backend import MatterServerBLEBackend

MATTER = "0000fff6-0000-1000-8000-00805f9b34fb"
C1, C2 = "18ee2ef5-263d-4559-959f-4f9c429f9d11", "18ee2ef5-263d-4559-959f-4f9c429f9d12"
FRAME = struct.Struct(">BH")


class FakeConnection:
    mtu = 247

    def __init__(self):
        self.writes: list[tuple[int, bytes, bool]] = []
        self.notify = None
        self.disconnected = False

    async def services(self):
        characteristics = (GattCharacteristic(C1, 3, ("write",)), GattCharacteristic(C2, 5, ("indicate",)))
        return [GattService(MATTER, 1, characteristics)]

    async def read(self, handle):
        return b""

    async def write(self, handle, data, response=True):
        self.writes.append((handle, data, response))

    async def start_notify(self, handle, callback):
        self.notify = callback

    async def stop_notify(self, handle): ...

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
async def matter_server():
    """A stand-in for matter-server's /ble endpoint; yields its URL and the backends that connect."""
    backends: asyncio.Queue[web.WebSocketResponse] = asyncio.Queue()
    done = asyncio.Event()

    async def ble(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await backends.put(ws)
        await done.wait()
        return ws

    app = web.Application()
    app.router.add_get("/ble", ble)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}/ble", backends
    done.set()
    await runner.cleanup()


@pytest.fixture
async def backend(matter_server):
    url, backends = matter_server
    radio = FakeBLESource("hci0", connection=FakeConnection())
    ble = BLEService(sources=[radio])
    await ble.start()
    async with ClientSession() as session:
        task = asyncio.create_task(MatterServerBLEBackend(url, ble, session).run())
        ws = await asyncio.wait_for(backends.get(), 5)
        assert await ws.receive_json() == {"type": "hello", "version": 1}
        await ws.send_json({"type": "hello_response", "version": 1})
        yield ws, radio
        task.cancel()
    await ble.stop()


async def command(ws, id: int, name: str, **args) -> dict:
    await ws.send_json({"id": id, "command": name, "args": args})
    return await ws.receive_json(timeout=5)


async def test_commissioning_runs_through_the_hub_radio(backend):
    ws, radio = backend
    radio.advertise("AA:BB", service_data={MATTER: bytes(8)})
    await asyncio.sleep(0)

    await ws.send_json({"id": 0, "command": "start_scan", "args": {"service_uuids": ["fff6"]}})
    messages = [await ws.receive_json(timeout=5) for _ in range(2)]
    assert {"id": 0, "success": True, "result": {}} in messages
    discovered = next(m["data"] for m in messages if m.get("event") == "device_discovered")
    assert discovered["address"] == "AA:BB" and discovered["service_data"] == {MATTER: "AAAAAAAAAAA="}

    assert (await command(ws, 1, "connect", address="AA:BB"))["result"] == {"connection_handle": 1, "mtu": 247}
    assert (await command(ws, 2, "discover_services", connection_handle=1))["result"] == {
        "services": [{"uuid": MATTER}]
    }
    characteristics = await command(ws, 3, "discover_characteristics", connection_handle=1, service_uuid=MATTER)
    assert [c["uuid"] for c in characteristics["result"]["characteristics"]] == [C1, C2]

    reply = await command(
        ws, 4, "write_and_subscribe", connection_handle=1, write_uuid=C1, write_value="aHM=", subscribe_uuid=C2
    )
    assert reply["success"]
    connection = radio.connection
    assert connection.writes == [(3, b"hs", True)]

    connection.notify(b"handshake")
    assert await ws.receive_bytes(timeout=5) == FRAME.pack(0x02, 1) + b"handshake"

    await ws.send_bytes(FRAME.pack(0x01, 1) + b"segment")
    async with asyncio.timeout(5):
        while len(connection.writes) < 2:
            await asyncio.sleep(0.01)
    assert connection.writes[1] == (3, b"segment", True)

    assert (await command(ws, 5, "disconnect", connection_handle=1))["success"]
    assert connection.disconnected


async def test_failures_are_answered_with_protocol_errors(backend):
    ws, radio = backend
    radio.connection = None

    assert (await command(ws, 0, "connect", address="CC:DD"))["error"] == "connection_failed"
    assert (await command(ws, 1, "discover_services", connection_handle=9))["error"] == "not_connected"
    assert (await command(ws, 2, "reboot"))["error"] == "internal_error"
