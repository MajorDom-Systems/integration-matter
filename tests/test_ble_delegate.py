"""matter-server commissions over Bluetooth through the hub's radios (BLE proxy protocol v1)."""

import asyncio
import struct

import pytest
from aiohttp import ClientSession, web
from majordom_integration_sdk.capabilities import GattCharacteristic, GattService
from majordom_integration_sdk.testing import FakeBLERouter, FakeBLESource

from majordom_matter.ble_delegate import MatterServerBLEDelegate

MATTER = "0000fff6-0000-1000-8000-00805f9b34fb"
C1, C2 = "18ee2ef5-263d-4559-959f-4f9c429f9d11", "18ee2ef5-263d-4559-959f-4f9c429f9d12"
FRAME = struct.Struct(">BH")


class GattError(Exception):
    """What bleak's BleakError and aioesphomeapi's APIConnectionError have in common: not an OSError."""


class FakeConnection:
    mtu = 247

    def __init__(self):
        self.writes: list[tuple[int, bytes, bool]] = []
        self.notify = None
        self.disconnected = False
        self.error: Exception | None = None  # raised by writes

    async def services(self):
        characteristics = (GattCharacteristic(C1, 3, ("write",)), GattCharacteristic(C2, 5, ("indicate",)))
        return [GattService(MATTER, 1, characteristics)]

    async def read(self, handle):
        return b""

    async def write(self, handle, data, is_response_needed=True):
        if self.error:
            raise self.error
        self.writes.append((handle, data, is_response_needed))

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
def radios() -> list[FakeBLESource]:
    return [FakeBLESource("hci0", connection=FakeConnection()), FakeBLESource("hci1", connection=FakeConnection())]


@pytest.fixture
async def backend(matter_server, radios):
    url, backends = matter_server
    radio = radios[0]
    ble = FakeBLERouter(sources=radios)
    await ble.start()
    async with ClientSession() as session:
        task = asyncio.create_task(MatterServerBLEDelegate(url, ble, session).run())
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


async def test_failures_are_answered_with_protocol_errors(backend, radios):
    ws, radio = backend
    radio.connection = None
    radios[1].connection = None

    assert (await command(ws, 0, "connect", address="CC:DD"))["error"] == "connection_failed"
    assert (await command(ws, 1, "discover_services", connection_handle=9))["error"] == "not_connected"
    assert (await command(ws, 2, "reboot"))["error"] == "internal_error"


async def test_a_failed_write_is_answered_and_the_backend_keeps_serving(backend):
    ws, radio = backend
    connection = radio.connection
    assert (await command(ws, 0, "connect", address="AA:BB"))["success"]
    assert (await command(ws, 1, "discover_characteristics", connection_handle=1, service_uuid=MATTER))["success"]
    connection.error = GattError("link lost")

    reply = await command(ws, 2, "write_characteristic", connection_handle=1, characteristic_uuid=C1, value="aHM=")
    assert reply["error"] == "write_failed"

    # a BTP segment is no request: its failure ends the connection, which matter-server is told about
    await ws.send_bytes(FRAME.pack(0x01, 1) + b"segment")
    assert await ws.receive_json(timeout=5) == {"event": "disconnected", "data": {"connection_handle": 1}}
    assert connection.disconnected

    connection.error = None
    assert (await command(ws, 3, "connect", address="AA:BB"))["result"] == {"connection_handle": 2, "mtu": 247}


async def test_a_dropped_websocket_reconnects(backend, matter_server):
    ws, _ = backend
    _, backends = matter_server
    await ws.close()
    ws = await asyncio.wait_for(backends.get(), 5)
    assert await ws.receive_json(timeout=5) == {"type": "hello", "version": 1}


async def test_a_connection_leases_the_strongest_source_until_it_is_gone(backend, radios):
    ws, hci0 = backend
    hci1 = radios[1]
    hci0.advertise("AA:BB", rssi=-80)
    hci1.advertise("AA:BB", rssi=-40)
    await asyncio.sleep(0)

    assert (await command(ws, 0, "connect", address="AA:BB"))["success"]
    assert hci1.connected == ["AA:BB"] and hci0.connected == []
    assert hci1.on_advertisement is None and hci0.on_advertisement

    assert (await command(ws, 1, "disconnect", connection_handle=1))["success"]
    assert hci1.on_advertisement


async def test_a_busy_source_fails_the_connect_and_a_lost_link_releases_it(backend, radios):
    ws, hci0 = backend
    hci0.advertise("AA:BB")
    await asyncio.sleep(0)
    assert (await command(ws, 0, "connect", address="AA:BB", timeout=300))["success"]

    reply = await command(ws, 1, "connect", address="AA:BB", timeout=300)
    assert reply["error"] == "connection_failed"

    await command(ws, 2, "discover_characteristics", connection_handle=1, service_uuid=MATTER)
    await command(ws, 3, "write_characteristic", connection_handle=1, characteristic_uuid=C1, value="aHM=")
    hci0.connection.error = GattError("link lost")
    await ws.send_bytes(FRAME.pack(0x01, 1) + b"segment")
    assert (await ws.receive_json(timeout=5))["event"] == "disconnected"
    async with asyncio.timeout(5):
        while not hci0.on_advertisement:
            await asyncio.sleep(0.01)


async def test_a_dropped_websocket_releases_the_leased_source(backend, matter_server):
    ws, radio = backend
    radio.advertise("AA:BB")
    await asyncio.sleep(0)
    assert (await command(ws, 0, "connect", address="AA:BB"))["success"]
    assert radio.on_advertisement is None

    await ws.close()
    async with asyncio.timeout(5):
        while not radio.on_advertisement:
            await asyncio.sleep(0.01)
    assert radio.connection.disconnected
