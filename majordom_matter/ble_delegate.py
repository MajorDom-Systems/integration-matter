"""Lends the hub's Bluetooth to matterjs-server, so commissioning reaches devices through every radio
`BLEService` hears with — local adapters and ESPHome nodes alike.

matterjs-server (``BLE_PROXY=true``) takes a Bluetooth client on ``/ble`` and drives it with the
BLE proxy protocol v1: https://github.com/matter-js/matterjs-server/blob/main/docs/ble-proxy-protocol.md
"""

import asyncio
import base64
import contextlib
import json
import logging
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType
from majordom_integration_sdk.discovery import BLEConnection, BLEDiscoveryInfo, BLEMatcher, BLEService

logger = logging.getLogger(__name__)

_FRAME = struct.Struct(">BH")  # opcode, connection handle; the payload follows
_WRITE, _NOTIFICATION = 0x01, 0x02
_RECONNECT_SEC, _RECONNECT_MAX_SEC = 1, 10  # the first retry, then doubling up to the most


class _Failure(Exception):
    def __init__(self, error: str, message: str = ""):
        super().__init__(message or error)
        self.error = error


@dataclass
class _Peripheral:
    connection: BLEConnection
    lease: contextlib.AsyncExitStack  # holds the Bluetooth source leased for this connection
    characteristics: dict[str, int] = field(default_factory=dict)  # uuid -> handle
    write_handle: int | None = None  # where binary frames go: the characteristic last written


def ble_url(matter_url: str) -> str:
    return matter_url.removesuffix("/ws") + "/ble"


def _uuid(text: str) -> UUID:
    """Short (``fff6``), 32-bit or full UUID text as a UUID."""
    return UUID(text.rjust(8, "0") + "-0000-1000-8000-00805f9b34fb" if len(text) <= 8 else text)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@contextlib.contextmanager
def _gatt(error: str) -> Iterator[None]:
    """Answers a failing GATT operation as ``error``, however its source failed (bleak, aioesphomeapi, a lost link)."""
    try:
        yield
    except Exception as e:
        raise _Failure(error, str(e)) from e


class MatterServerBLEDelegate:
    """Connects to matterjs-server's ``/ble`` endpoint and performs its BLE operations through the Hub's BLEService.

    It reconnects whenever matterjs-server goes away. Run one per matterjs-server.
    """

    def __init__(self, url: str, ble: BLEService, session: ClientSession):
        self._url, self._ble, self._session = url, ble, session
        self._ws: ClientWebSocketResponse | None = None
        self._peripherals: dict[int, _Peripheral] = {}
        self._last_handle = 0
        self._stop_scanning: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task] = set()

    async def run(self):
        """Serves matterjs-server until cancelled, reconnecting whenever it goes away."""
        delay = _RECONNECT_SEC
        while True:
            try:
                async with self._session.ws_connect(self._url) as ws:
                    self._ws = ws
                    if await self._serve(ws):
                        delay = _RECONNECT_SEC
                reason = "closed"
            except Exception as e:  # whatever broke, matterjs-server must get the hub's Bluetooth back
                reason = f"failed: {e!r}"
            finally:
                await self._release()
            log = logger.warning if delay == _RECONNECT_SEC else logger.debug  # once per outage, not every retry
            log(f"matterjs-server's /ble endpoint {self._url} {reason}, reconnecting in {delay} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_SEC)

    async def _serve(self, ws: ClientWebSocketResponse) -> bool:
        """Serves one connection until it closes; False when matterjs-server refuses it."""
        await ws.send_json({"type": "hello", "version": 1})
        if error := (await ws.receive_json(timeout=10)).get("error"):
            logger.warning(f"matterjs-server refused the hub's Bluetooth: {error}")
            return False
        logger.info(f"Lending Bluetooth to matterjs-server at {self._url}")
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                self._spawn(self._answer(ws, json.loads(message.data)))
            elif message.type == WSMsgType.BINARY:
                await self._forward(message.data)  # in order: these are segments of one stream
        return True

    async def _answer(self, ws: ClientWebSocketResponse, command: dict):
        try:
            handler = getattr(self, f"_{command['command']}", None)
            if not handler:
                raise _Failure("internal_error", f"Unknown command {command['command']}")
            result = await handler(**command.get("args", {}))
            reply = {"id": command["id"], "success": True, "result": result or {}}
        except Exception as e:
            error = e.error if isinstance(e, _Failure) else "internal_error"
            reply = {"id": command["id"], "success": False, "error": error, "message": str(e)}
        await ws.send_json(reply)

    async def _forward(self, frame: bytes):
        opcode, handle = _FRAME.unpack_from(frame)
        peripheral = self._peripherals.get(handle)
        if opcode == _WRITE and peripheral and peripheral.write_handle is not None:
            try:
                await peripheral.connection.write(peripheral.write_handle, frame[_FRAME.size :], response=True)
            except Exception as e:  # a BTP segment is no request to fail, and BTP can't skip one: end the connection
                logger.warning(f"Bluetooth write on connection {handle} failed, disconnecting: {e!r}")
                self._lost(handle)
                self._spawn(self._close(peripheral))

    def _peripheral(self, connection_handle: int) -> _Peripheral:
        if peripheral := self._peripherals.get(connection_handle):
            return peripheral
        raise _Failure("not_connected")

    def _characteristic(self, connection_handle: int, uuid: str) -> tuple[_Peripheral, int]:
        peripheral = self._peripheral(connection_handle)
        if (handle := peripheral.characteristics.get(uuid.lower())) is None:
            raise _Failure("characteristic_not_found", uuid)
        return peripheral, handle

    # Commands

    async def _start_scan(self, service_uuids: list[str] | None = None, allow_duplicates: bool = False):
        await self._stop_scan()
        self._stop_scanning = self._ble.register(
            self, [BLEMatcher(_uuid(u)) for u in service_uuids or ()] or [BLEMatcher()]
        )

    async def _stop_scan(self):
        if self._stop_scanning:
            self._stop_scanning()
            self._stop_scanning = None

    async def _connect(self, address: str, timeout: int = 30_000):
        self._last_handle = handle = self._last_handle % 0xFFFF + 1
        async with contextlib.AsyncExitStack() as stack:  # closed here on failure, kept for the connection otherwise
            try:
                lease = await stack.enter_async_context(
                    self._ble.exclusive(address, owner="matter", timeout=timeout / 1000)
                )
                connection = await asyncio.wait_for(lease.connect(lambda: self._lost(handle)), timeout / 1000)
            except TimeoutError as e:
                raise _Failure("timeout") from e
            except ConnectionError as e:  # incl. BLEBusyError: every source that hears the device is leased
                raise _Failure("connection_failed", str(e)) from e
            self._peripherals[handle] = _Peripheral(connection, stack.pop_all())
        return {"connection_handle": handle, "mtu": connection.mtu}

    async def _disconnect(self, connection_handle: int):
        peripheral = self._peripheral(connection_handle)
        del self._peripherals[connection_handle]
        await self._close(peripheral)

    async def _discover_services(self, connection_handle: int):
        peripheral = self._peripheral(connection_handle)
        with _gatt("discovery_failed"):
            services = await peripheral.connection.services()
        return {"services": [{"uuid": s.uuid} for s in services]}

    async def _discover_characteristics(self, connection_handle: int, service_uuid: str):
        peripheral = self._peripheral(connection_handle)
        with _gatt("discovery_failed"):
            services = await peripheral.connection.services()
        service = next((s for s in services if _uuid(s.uuid) == _uuid(service_uuid)), None)
        if not service:
            raise _Failure("service_not_found", service_uuid)
        peripheral.characteristics |= {c.uuid.lower(): c.handle for c in service.characteristics}
        return {
            "characteristics": [{"uuid": c.uuid, "properties": list(c.properties)} for c in service.characteristics]
        }

    async def _read_characteristic(self, connection_handle: int, characteristic_uuid: str):
        peripheral, handle = self._characteristic(connection_handle, characteristic_uuid)
        with _gatt("read_failed"):
            return {"value": _b64(await peripheral.connection.read(handle))}

    async def _write_characteristic(
        self, connection_handle: int, characteristic_uuid: str, value: str, response: bool = False
    ):
        peripheral, handle = self._characteristic(connection_handle, characteristic_uuid)
        peripheral.write_handle = handle
        with _gatt("write_failed"):
            await peripheral.connection.write(handle, base64.b64decode(value), response)

    async def _subscribe_characteristic(self, connection_handle: int, characteristic_uuid: str):
        peripheral, handle = self._characteristic(connection_handle, characteristic_uuid)
        with _gatt("subscribe_failed"):
            await peripheral.connection.start_notify(handle, lambda data: self._notify(connection_handle, data))

    async def _write_and_subscribe(
        self,
        connection_handle: int,
        write_uuid: str,
        write_value: str,
        subscribe_uuid: str,
        write_response: bool = True,
    ):
        # the write must complete before notifications are on: the device answers the write by notifying
        await self._write_characteristic(connection_handle, write_uuid, write_value, write_response)
        await self._subscribe_characteristic(connection_handle, subscribe_uuid)

    async def _unsubscribe_characteristic(self, connection_handle: int, characteristic_uuid: str):
        peripheral, handle = self._characteristic(connection_handle, characteristic_uuid)
        await peripheral.connection.stop_notify(handle)

    async def _request_mtu(self, connection_handle: int, mtu: int):
        return {"mtu": self._peripheral(connection_handle).connection.mtu}

    # Events

    async def ble_did_discover_device(self, ble: BLEService, info: BLEDiscoveryInfo):
        adv = info.advertisement
        await self._send_event("device_discovered", {
            "address": info.device.address,
            "connectable": True,  # bleak doesn't say; matterjs-server ignores devices that aren't
            "name": adv.local_name,
            "rssi": adv.rssi,
            "service_uuids": adv.service_uuids,
            "service_data": {uuid: _b64(data) for uuid, data in adv.service_data.items()},
            "manufacturer_data": {str(company): _b64(data) for company, data in adv.manufacturer_data.items()},
        })  # fmt: skip

    ble_did_update_device = ble_did_discover_device

    async def ble_did_remove_device(self, ble: BLEService, info: BLEDiscoveryInfo): ...

    def _notify(self, connection_handle: int, data: bytes):
        if self._ws and not self._ws.closed:
            self._spawn(self._ws.send_bytes(_FRAME.pack(_NOTIFICATION, connection_handle) + data))

    def _lost(self, connection_handle: int):
        if peripheral := self._peripherals.pop(connection_handle, None):
            self._spawn(peripheral.lease.aclose())
            self._spawn(self._send_event("disconnected", {"connection_handle": connection_handle}))

    async def _send_event(self, event: str, data: dict):
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"event": event, "data": data})

    async def _release(self):
        for task in self._tasks:
            task.cancel()
        await self._stop_scan()
        peripherals, self._peripherals = list(self._peripherals.values()), {}
        await asyncio.gather(*(self._close(p) for p in peripherals), return_exceptions=True)

    @staticmethod
    async def _close(peripheral: _Peripheral):
        """Ends the connection and gives its Bluetooth source back, whatever the disconnect does."""
        try:
            await peripheral.connection.disconnect()
        finally:
            await peripheral.lease.aclose()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and (e := task.exception()):
            logger.debug(f"Bluetooth delegate task failed: {e!r}")
