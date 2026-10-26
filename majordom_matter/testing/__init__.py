"""In-process test doubles for the Matter integration — no matterjs-server, no MVD binaries.

`FakeMatterClient` stands in for `matter_server.client.MatterClient`, backed by a single canned
node captured from a real MVD on-off-light (`fixtures/on_off_light_node.json`). It rebuilds a
**real** `MatterNode` exactly as the live client does — `MatterNode(dataclass_from_dict(
MatterNodeData, ...))` — so the controller and mapper run against a faithful node; only the
transport (the websocket to matterjs-server) is faked.

Use it to exercise the controller's wiring — discovery, pairing/mapping, commands, events — in
plain CI without docker. The heavy, real-device coverage stays in the dockerized MVD suite.

    from majordom_matter.testing import FakeMatterClient
    monkeypatch.setattr("majordom_matter.controller.MatterClient", FakeMatterClient)

To have the controller reconnect to the same fabric, share one `FakeMatterServer` between its clients:
`partial(FakeMatterClient, server=FakeMatterServer())`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from matter_server.client.exceptions import CannotConnect, InvalidState
from matter_server.client.models.node import MatterNode
from matter_server.common.errors import NodeNotExists
from matter_server.common.helpers.util import dataclass_from_dict
from matter_server.common.models import CommissionableNodeData, EventType, MatterNodeData

_FIXTURE = Path(__file__).parent / "fixtures" / "on_off_light_node.json"


def load_on_off_light_node() -> MatterNode:
    """A real `MatterNode` for a captured MVD on-off-light — the same construction the live
    client applies to node data received over the websocket."""
    data = json.loads(_FIXTURE.read_text())
    return MatterNode(dataclass_from_dict(MatterNodeData, data))


class _Subscription:
    __slots__ = ("callback", "event_filter", "node_filter", "attr_path_filter")

    def __init__(self, callback, event_filter, node_filter, attr_path_filter):
        self.callback = callback
        self.event_filter = event_filter
        self.node_filter = node_filter
        self.attr_path_filter = attr_path_filter


class FakeMatterServer:
    """matterjs-server's side of the fake: the fabric (the canned node) and whether the server is up."""

    def __init__(self, node: MatterNode | None = None):
        self.node = node if node is not None else load_on_off_light_node()
        self.commissioned = False
        self.up = True
        self.is_answering = True  # False: a call waits for its answer until the server goes away
        self.gone = asyncio.Event()  # set when this run of the server goes away

    def lose(self):
        """Go away, like its container restarting: listening clients end, new ones can't connect."""
        self.up = False
        self.gone.set()

    def restore(self):
        self.up = True
        self.gone = asyncio.Event()


class FakeMatterClient:
    """Drop-in for `matter_server.client.MatterClient`, backed by one canned node.

    Constructed with the same `(url, session)` signature the controller uses. The node is not
    "on the fabric" until commissioned; before that it is advertised via
    `discover_commissionable_nodes()`, so the full discover → pair → map flow runs. Outgoing
    commands/attribute writes are recorded (`sent_commands`, `written_attributes`,
    `removed_nodes`) and `fire_attribute_update()` lets a test simulate a device-side change.
    """

    def __init__(
        self,
        url: str | None = None,
        session: Any = None,
        *,
        node: MatterNode | None = None,
        server: FakeMatterServer | None = None,
    ):
        self._url = url
        self._session = session
        self.server = server if server is not None else FakeMatterServer(node)
        self._node = self.server.node
        self._subscriptions: list[_Subscription] = []
        self.sent_commands: list[tuple[int, int, Any]] = []
        self.written_attributes: list[tuple[int, str, Any]] = []
        self.removed_nodes: list[int] = []
        self.networks: dict[str, Any] = {}  # credentials handed over for commissioning
        self._server_gone = self.server.gone

    # --- lifecycle ---
    async def connect(self):
        if not self.server.up:
            raise CannotConnect(ConnectionRefusedError("matterjs-server is down"))
        self._server_gone = self.server.gone

    async def start_listening(self, init_ready: asyncio.Event | None = None):
        if init_ready is not None:
            init_ready.set()
        # The real client runs its websocket loop until matterjs-server goes away (`lose_server()`);
        # block until then or until the controller's stop() cancels this task.
        await self._server_gone.wait()

    async def disconnect(self):
        return None

    async def set_wifi_credentials(self, ssid: str, credentials: str):
        await self._answer()
        self.networks["wifi"] = (ssid, credentials)

    async def set_thread_operational_dataset(self, dataset: str):
        await self._answer()
        self.networks["thread"] = dataset

    async def _answer(self):
        if not self.server.up or self._server_gone.is_set():
            raise InvalidState("Not connected")  # as the real client does once its matterjs-server went away
        if not self.server.is_answering:
            await self._server_gone.wait()
            raise asyncio.CancelledError  # the real client's disconnect cancels the calls waiting for an answer

    # --- nodes ---
    def get_nodes(self) -> list[MatterNode]:
        return [self._node] if self.server.commissioned else []

    def get_node(self, node_id: int) -> MatterNode:
        if self.server.commissioned and node_id == self._node.node_id:
            return self._node
        raise NodeNotExists(f"Node {node_id} does not exist or is not yet interviewed")  # as the real client

    async def remove_node(self, node_id: int):
        if not (self.server.commissioned and node_id == self._node.node_id):
            raise NodeNotExists(f"Node {node_id} does not exist")  # as matterjs-server answers
        self.removed_nodes.append(node_id)
        if node_id == self._node.node_id:
            self.server.commissioned = False

    # --- discovery ---
    async def discover_commissionable_nodes(self) -> list[CommissionableNodeData]:
        if self.server.commissioned:
            return []
        info = self._node.device_info
        return [
            CommissionableNodeData(
                instance_name="FAKEONOFF0001",
                vendor_id=getattr(info, "vendorID", None),
                product_id=getattr(info, "productID", None),
                commissioning_mode=1,  # advertising for commissioning
                device_name=getattr(info, "productName", None) or "Fake On/Off Light",
                addresses=["fd00::1"],  # on-network → commissioned with network_only=True
            )
        ]

    # --- commissioning ---
    async def commission_with_code(self, code: str, network_only: bool = False) -> MatterNodeData:
        self.server.commissioned = True
        return self._node.node_data

    async def commission_on_network(self, setup_pin_code: int, *args: Any, **kwargs: Any) -> MatterNodeData:
        self.server.commissioned = True
        return self._node.node_data

    # --- commands / attributes ---
    async def send_device_command(self, node_id: int, endpoint_id: int, command: Any, **kwargs: Any):
        self.sent_commands.append((node_id, endpoint_id, command))
        return None

    async def write_attribute(self, node_id: int, attribute_path: str, value: Any) -> Any:
        self.written_attributes.append((node_id, attribute_path, value))
        # Reflect the write into the node so reads and subscribers see it, like a real device.
        self._node.update_attribute(attribute_path, value)
        self._emit(EventType.ATTRIBUTE_UPDATED, node_id, attribute_path, value)
        return value

    # --- subscriptions ---
    def subscribe_events(self, callback, event_filter=None, node_filter=None, attr_path_filter=None):
        sub = _Subscription(callback, event_filter, node_filter, attr_path_filter)
        self._subscriptions.append(sub)

        def unsubscribe():
            if sub in self._subscriptions:
                self._subscriptions.remove(sub)

        return unsubscribe

    # --- test helpers ---
    def lose_server(self):
        """Simulate matterjs-server going away (its container restarting): listening ends, as the real client's does."""
        self.server.lose()

    def fire_attribute_update(self, attribute_path: str, value: Any):
        """Simulate the device pushing a new attribute value to its subscribers."""
        self._node.update_attribute(attribute_path, value)
        self._emit(EventType.ATTRIBUTE_UPDATED, self._node.node_id, attribute_path, value)

    def _emit(self, event_type: EventType, node_id: int, attribute_path: str, value: Any):
        for sub in list(self._subscriptions):
            if sub.event_filter is not None and sub.event_filter != event_type:
                continue
            if sub.node_filter is not None and sub.node_filter != node_id:
                continue
            if sub.attr_path_filter is not None and sub.attr_path_filter != attribute_path:
                continue
            sub.callback(event_type, value)


__all__ = ["FakeMatterClient", "FakeMatterServer", "load_on_off_light_node"]
