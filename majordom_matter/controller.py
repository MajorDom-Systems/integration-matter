import asyncio
import contextlib
import inspect
import logging
from typing import override
from uuid import UUID

from aiohttp import ClientSession
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Objects import Identify
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryInfo, BLEDiscoveryService
from majordom_integration_sdk.schemas.base import NonEmptyStr
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsType, Discovery, ProvidedCredentials
from majordom_integration_sdk.schemas.event import DeviceParameterChange
from majordom_integration_sdk.schemas.parameter import ParameterRole
from matter_server.client import MatterClient
from matter_server.client.models.node import MatterNode
from matter_server.common.errors import UnknownError
from matter_server.common.models import CommissionableNodeData, EventType

from majordom_matter.config import matter_ble_via_server, matter_server_url

from .exceptions import (
    MatterConnectionError,
    MatterNotFoundParameter,
    MatterUnexpectedError,
    MatterUnsupportedParameter,
)
from .mapper import MatterMapper
from .matter_spec import MAIN_PARAMETER_BY_CLUSTER, DefaultParams
from .model import (
    MatterDevice,
    MatterDeviceIntegrationData,
    MatterDeviceState,
    MatterParameter,
    MatterParameterState,
    MatterParameterTypeEnum,
)


class MatterController(AbstractController):
    """Bridges the Hub to Matter devices through a Matter controller server (matter-server).

    matter-server owns the Matter fabric, the Thread/BLE radios, and commissioning; this
    controller adapts it to the Hub's AbstractController contract, talking to it over its
    WebSocket API. On-network devices are discovered via matter-server.

    BLE-only commissionable devices: by default the Hub discovers them itself via the shared SDK
    BLE scanner (needs a working Bluetooth/D-Bus stack in the Hub process). With
    `matter_ble_via_server` set, the Hub does NO local BLE — matter-server does the BLE scan+
    commission (via commission_with_code); the Hub only surfaces a generic "commission by code"
    discovery. See NOTES-ble-via-matter-server.md and readme.md.
    """

    _matter_client: MatterClient
    _matter_client_session: ClientSession
    _majordom_descoveries: dict[UUID, Discovery]
    _mapper: MatterMapper

    # Matter commissionable BLE service (spec 5.4.2.5.6) — devices in commissioning mode advertise
    # this service + its service-data payload. discover_commissionable_nodes only surfaces IP/mDNS
    # devices, so BLE-only devices (a factory-fresh bulb not yet on any network) are discovered here
    # via the shared BLE scanner instead.
    _MATTER_COMMISSIONABLE_SERVICE = UUID("0000fff6-0000-1000-8000-00805f9b34fb")

    def __init__(self, dependencies: AbstractController.Dependencies):
        super().__init__(dependencies)
        # Instance state (not class-level): the mapper is wired with the framework's UUID
        # generators, and the discoveries dict must not be shared across instances.
        self._mapper = MatterMapper(self.device_uuid, self.parameter_uuid)
        self._majordom_descoveries = dict()

    # -------------------------------------------------------------------------
    # AbstractController interface
    # -------------------------------------------------------------------------

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        return self._majordom_descoveries

    @property
    @override
    def device_type(self) -> type[MatterDevice]:
        return MatterDevice

    @property
    @override
    def parameter_type(self) -> type[MatterParameter]:
        return MatterParameter

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self):
        self._background_tasks: list[asyncio.Task] = []
        self._node_availability: dict[UUID, bool] = {}
        self._matter_client_session = ClientSession()
        self._matter_client = MatterClient(matter_server_url, self._matter_client_session)
        await self._matter_client.connect()

        init_ready = asyncio.Event()
        self._background_tasks.append(asyncio.create_task(self._matter_client.start_listening(init_ready=init_ready)))
        await init_ready.wait()

        self._background_tasks.append(asyncio.create_task(self._matter_discovery_loop()))

        # BLE discovery for commissionable devices not yet on any IP network (the mDNS-based
        # discovery loop above can't see them). No-op in virtual mode where the BLE service is off.
        self._ble_addresses: dict[UUID, set[str]] = {}  # discovery_id -> live BLE addresses
        if matter_ble_via_server:
            # ble-via-server mode: do NOT open a local BLE scanner (the Hub process may have no
            # usable Bluetooth/D-Bus, e.g. a de-rooted container). matter-server owns the radio and
            # does the BLE scan+commission inside commission_with_code. Its WS API can't enumerate
            # commissionable BLE devices, so we surface one generic "commission by code" discovery.
            self._ble_cancel = None
            await self._emit_ble_via_server_discovery()
        else:
            self._ble_cancel = self.dependencies.ble_discovery_service.register(
                self, {self._MATTER_COMMISSIONABLE_SERVICE}
            )

        device_nodes: list[int] = []
        async with self.dependencies.make_device_repository() as device_repository:
            for device in await device_repository.get_all(as_=MatterDevice):
                if node := self._matter_client.get_node(device.integration_data.node_id):
                    self._subscription(device.id, node)
                    device_nodes.append(device.integration_data.node_id)
                else:
                    device.available = False
                    device.last_error = f"Device {device.name} is no longer connected to the Matter network"
                    await device_repository.save(device, device.id)

            for node in self._matter_client.get_nodes():
                if node.node_id in device_nodes:
                    continue
                discovery_id = self._mapper.device_uuid(node.node_id, node.device_info.productName)
                discovery = Discovery(
                    id=discovery_id,
                    integration=NonEmptyStr(self.name),
                    expected_credentials_options=[CredentialsType.none],
                    expiration=None,
                    transport=NonEmptyStr("IP"),
                    device_name=NonEmptyStr(node.device_info.productName or "Unknown"),
                    device_manufacturer=node.device_info.vendorName or str(node.device_info.vendorID),
                    device_category=None,
                    device_icon=None,
                )
                self._majordom_descoveries[discovery_id] = discovery
                await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    async def stop(self):
        self._majordom_descoveries.clear()

        if cancel := getattr(self, "_ble_cancel", None):
            cancel()
            self._ble_cancel = None

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        # Cancel the discovery loop and listener started in start() — otherwise they keep
        # running against a matter client/event loop that stop() is about to tear down,
        # which surfaces as "Future attached to a different loop" once the next test/run
        # starts a fresh event loop. cancel() is safe to call across loops.
        #
        # Whether we then AWAIT them — and tear the matter client down — depends on the
        # loop. If stop() runs on a different loop than start() did (e.g. invoked via a sync
        # TestClient, whose portal runs on its own loop), awaiting any of these loop-bound
        # futures (the tasks, or the ws client's disconnect/close, which awaits the same
        # loop-A websocket) itself raises "attached to a different loop". There, cancel()
        # above is the meaningful cleanup and we skip the awaits; the normal same-loop
        # lifecycle does the full teardown.
        background_tasks = getattr(self, "_background_tasks", [])
        for task in background_tasks:
            task.cancel()
        on_owning_loop = running_loop is not None and (
            not background_tasks or background_tasks[0].get_loop() is running_loop
        )
        self._background_tasks = []

        if not on_owning_loop:
            return

        for task in background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if not self._matter_client_session or not self._matter_client:
            return
        await self._matter_client.disconnect()
        await self._matter_client_session.close()

    # -------------------------------------------------------------------------
    # Hub -> device operations
    # -------------------------------------------------------------------------

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None):
        self._require_matter_client()

        if not credentials or credentials.type not in discovery.expected_credentials_options:
            raise MatterUnexpectedError(
                f"Credentials type {credentials.type if credentials else None!r} is not one of the "
                f"types this discovery advertised: {discovery.expected_credentials_options}"
            )

        # Every commissioning path below needs the pairing code/QR payload; a missing value is an
        # unexpected caller error, not something to stringify into "None" or crash on int(None).
        if credentials.value is None:
            raise MatterUnexpectedError("Matter commissioning requires a pairing code, but none was provided")

        # Compare by value (==), not identity (is): `credentials.type` can arrive through a
        # schema layer that resolved CredentialsType via a different import path, yielding a
        # distinct enum class where `is` would never match. `==` on a str-enum compares the
        # underlying value and is robust to that (and semantically what we want anyway).
        if credentials.type == CredentialsType.qr:
            commission_node = await self._matter_client.commission_with_code(str(credentials.value))
        elif credentials.type == CredentialsType.code:
            if discovery.transport == "BLE":
                # commission_on_network only works for devices already reachable over IP
                # (matter-server's own docstring: "for advanced usecases only, use
                # commission_with_code for regular commissioning") — a BLE-discovered
                # device has no IP yet, so it must go through commission_with_code instead,
                # which chip-tool routes over BLE automatically based on the code.
                commission_node = await self._matter_client.commission_with_code(str(credentials.value))
            else:
                commission_node = await self._matter_client.commission_on_network(int(credentials.value))
        else:
            raise MatterUnexpectedError("This credentials type is not supported")

        self._majordom_descoveries.pop(discovery.id)
        node = self._matter_client.get_node(commission_node.node_id)
        device_id = self._mapper.device_uuid(node.node_id, node.device_info.productName)

        async with self.dependencies.make_device_repository() as device_repository:
            device = await device_repository.state(discovery.id, MatterDeviceState)
            assert device
            device.id = device_id
            # Manufacturer-provided, read-only description (BasicInformation product label).
            device.description = getattr(node.device_info, "productLabel", None) or None

            # Persist the commissioned node id — the Hub seeds a provisional device whose
            # integration_data already exists (so it's never falsy), and later reconnects look
            # the node up by this id, so set it unconditionally rather than only when absent.
            if device.integration_data:
                device.integration_data.node_id = node.node_id
            else:
                device.integration_data = MatterDeviceIntegrationData(node_id=node.node_id)

            for endpoint_id, endpoint in node.endpoints.items():
                for cluster_id, cluster in endpoint.clusters.items():
                    if hasattr(cluster, "Commands"):
                        for parameter in self._mapper.parse_commands(device_id, endpoint_id, cluster_id, cluster, node):
                            device.parameters.append(MatterParameterState(**parameter.__dict__, value=None))

                    if hasattr(cluster, "Attributes"):
                        for parameter in self._mapper.parse_attributes(
                            device_id, endpoint_id, cluster_id, cluster, endpoint, node
                        ):
                            attribute_id = parameter.integration_data.attribute_id
                            if attribute_id is None:
                                raise MatterUnexpectedError(
                                    f"Attribute parameter {parameter.name!r} has no attribute_id "
                                    f"(endpoint {endpoint_id}, cluster {cluster_id})"
                                )
                            value = node.get_attribute_value(endpoint_id, cluster_id, attribute_id)
                            value = self._mapper.apply_attribute_scale(cluster_id, attribute_id, value)
                            device.parameters.append(
                                MatterParameterState(
                                    **parameter.__dict__, value=self._mapper.normalize_value(value)
                                )
                            )

            main_parameter_id, default_value = self._get_main_parameter(device.id, node)
            device.main_parameter = main_parameter_id
            if main_parameter_id and default_value is not None:
                main_parameter = next((p for p in device.parameters if p.id == main_parameter_id), None)
                if main_parameter is None:
                    device.main_parameter = None
                elif main_parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                    main_parameter.default_value = self._mapper.normalize_value(default_value)
                elif isinstance(default_value, dict):
                    # A command main parameter is tapped with a fixed argument set (a dict).
                    main_parameter.integration_data.default_arguments = default_value
                else:
                    raise MatterUnexpectedError(
                        f"Command main parameter {main_parameter_id} expected dict default arguments, "
                        f"got {type(default_value).__name__}: {default_value!r}"
                    )

            await device_repository.save(device, discovery.id)

        await self.dependencies.output.controller_did_connect_device(self, device_id)
        self._subscription(device_id, node)
        return device_id

    async def unpair(self, device: MatterDevice):
        await self._require_matter_client().remove_node(device.node_id)

    async def identify(self, device: MatterDevice):
        command = Identify.Commands.Identify()
        node = self._require_node(device)
        for endpoint_id in node.endpoints:
            if node.has_cluster(3, endpoint_id):
                await self._matter_client.send_device_command(device.node_id, endpoint_id, command)

    async def fetch(self, device: MatterDevice):
        node = self._require_node(device)

        events = []
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if not hasattr(cluster, "Attributes"):
                    continue
                for _, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                    if not issubclass(attribute, ClusterAttributeDescriptor):
                        continue
                    attribute_id = getattr(attribute, "attribute_id", -1)
                    value = node.get_attribute_value(endpoint_id, cluster_id, attribute_id)
                    value = self._mapper.apply_attribute_scale(cluster_id, attribute_id, value)
                    parameter_id = self._mapper.attribute_parameter_uuid(
                        device.id, endpoint_id, cluster_id, attribute_id
                    )
                    events.append(
                        DeviceParameterChange(
                            device_id=device.id,
                            parameter_id=parameter_id,
                            value=value if isinstance(value, str | int | float | bool) else None,
                        )
                    )

        await self.dependencies.output.controller_did_receive_events(self, events)

    async def send_command(self, command: DeviceCommand, device: MatterDevice, parameter: MatterParameter):
        try:
            node = self._require_node(device)

            endpoint_id = parameter.integration_data.endpoint_id
            endpoint = node.endpoints.get(endpoint_id)
            if not endpoint:
                raise MatterUnexpectedError(f"Endpoint {endpoint_id} not found on node {node.node_id}")

            cluster_id = parameter.integration_data.cluster_id
            cluster = endpoint.clusters.get(cluster_id)
            if not cluster:
                raise MatterUnexpectedError(f"Cluster {cluster_id} not found on endpoint {endpoint_id}")

            if parameter.integration_data.type is MatterParameterTypeEnum.command:
                await self._execute_cluster_command(node, endpoint_id, cluster, parameter, command)
            elif parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                await self._write_cluster_attribute(node, endpoint_id, cluster_id, parameter, command)
        except UnknownError as e:
            error = str(e)

            error_map = {
                "0x8b": (MatterNotFoundParameter, "not found on device"),
                "0x81": (MatterUnsupportedParameter, "not supported by device"),
            }

            match = next(((exc, msg) for code, (exc, msg) in error_map.items() if code in error), None)
            if match is None:
                # Unknown/unmapped Matter error (e.g. Busy 0x9c) — don't swallow it
                logging.error(f"Command '{parameter.name}' failed with unmapped error: {error}")
                raise

            exception_class, message = match
            async with self.dependencies.make_device_repository() as device_repository:
                device_state = await device_repository.state(device.id, MatterDeviceState)
                if device_state is not None:
                    # state() returns None only if the device was unpaired/removed out from under us;
                    # nothing to prune then — just surface the original command error below.
                    device_state.parameters = [p for p in device_state.parameters if p.id != parameter.id]
                    device.integration_data.black_list.append(parameter.id)
                    await device_repository.save(device, device.id)
                else:
                    logging.warning(f"Device {device.id} is gone; skipped blacklisting parameter {parameter.id}")

            logging.error(f"Command '{parameter.name}' is {message} and will be removed")
            raise exception_class(f"Command '{parameter.name}' failed: {message}") from None

    # -------------------------------------------------------------------------
    # Device -> Hub: discovery (mDNS / on-network)
    # -------------------------------------------------------------------------

    async def _matter_discovery_loop(self, interval: int = 5):
        # On-network (mDNS) discovery is polled from matter-server rather than routed through the
        # Hub's shared Zeroconf service on purpose: matter-server already parses the Matter `_matterc`
        # TXT records into clean, structured CommissionableNodeData (discriminator, vendor/product,
        # commissioning mode, pairing hints). Re-deriving that on the raw Zeroconf service would just
        # duplicate matter-server's parser. BLE-only devices, which mDNS can't see, ARE discovered via
        # the Hub's shared BLE service instead (see the BLE discovery section below) — so the "our
        # discovery + SDK for pairing" split is used exactly where it adds value.
        while True:
            try:
                nodes: list[CommissionableNodeData] = await self._matter_client.discover_commissionable_nodes()
                for node in nodes:
                    await self._async_matter_did_discover(node)
            except Exception as e:
                logging.error(f"[{self.name}] discovery error: {e}")
            await asyncio.sleep(interval)

    async def _async_matter_did_discover(self, node: CommissionableNodeData):
        discovery_id = self._mapper.discovery_uuid(
            node.instance_name
            or f"{node.vendor_id}_{node.product_id}_{node.addresses[0] if node.addresses else 'unknown'}"
        )
        discovery = Discovery(
            id=discovery_id,
            integration=NonEmptyStr(self.name),
            expected_credentials_options=self._mapper.define_credentials_options(
                node.commissioning_mode, node.pairing_hint, node.pairing_instruction, bool(node.addresses)
            ),
            expiration=None,
            transport=NonEmptyStr("IP" if node.addresses else "BLE"),
            device_name=NonEmptyStr(node.device_name or node.instance_name or "Unknown"),
            device_manufacturer=f"Vendor {node.vendor_id}" if node.vendor_id else None,
            device_category=str(node.device_type),
            device_icon=None,
        )
        self._majordom_descoveries[discovery_id] = discovery
        await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    # -------------------------------------------------------------------------
    # Device -> Hub: BLE via matter-server (no local scan) — commission by code
    # -------------------------------------------------------------------------

    # Stable key for the single generic "commission over BLE by code" discovery used when
    # matter_ble_via_server is set (the Hub does no local BLE scan).
    _BLE_VIA_SERVER_DISCOVERY_KEY = "matter-ble-via-server"

    async def _emit_ble_via_server_discovery(self) -> None:
        """Surface one generic 'commission over BLE by code' discovery (ble-via-server mode).

        matter-server exposes no way to ENUMERATE commissionable BLE devices, so instead of listing
        real devices we advertise a single placeholder. Pairing it with a manual pairing code / QR
        routes through pair_device's BLE branch to `commission_with_code`, which makes matter-server
        scan for the device over BLE (by the code's discriminator) and commission it. On-network
        devices are still discovered individually via the mDNS loop.

        DRAFT/TODO: (1) pair_device does `device_repository.state(discovery.id)` + `assert device`,
        which assumes the Hub seeded provisional device-state for this discovery id when the user
        initiated pairing — verify the Hub does that for a synthetic discovery, or seed it here.
        (2) pair_device pops the discovery on success; re-emit this placeholder afterwards so more
        BLE devices can be added. (3) The hardware test's discover→pair flow expects a device-
        specific discovery; under this mode it must instead pair the placeholder with the bulb's code.
        """
        discovery_id = self._mapper.discovery_uuid(self._BLE_VIA_SERVER_DISCOVERY_KEY)
        discovery = Discovery(
            id=discovery_id,
            integration=NonEmptyStr(self.name),
            # Accept the device's manual pairing code (or QR); commission_with_code does the BLE scan.
            expected_credentials_options=[CredentialsType.code.with_mask("DDDD-DDD-DDDD"), CredentialsType.qr],
            expiration=None,
            transport=NonEmptyStr("BLE"),
            device_name=NonEmptyStr("Matter device (enter pairing code)"),
            device_manufacturer=None,
            device_category=None,
            device_icon=None,
        )
        self._majordom_descoveries[discovery_id] = discovery
        await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    # -------------------------------------------------------------------------
    # Device -> Hub: BLE discovery (BLEDiscoveryListener) — commissionable devices not yet on IP
    # (used only when matter_ble_via_server is NOT set — i.e. the Hub scans BLE locally)
    # -------------------------------------------------------------------------

    async def ble_did_discover_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
        await self._matter_did_discover_ble(info)

    async def ble_did_update_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
        await self._matter_did_discover_ble(info)

    async def ble_did_remove_device(self, ble: BLEDiscoveryService, info: BLEDiscoveryInfo):
        # Intentionally keep the discovery. Commissionable devices rotate their BLE address while the
        # window is open (~minutes), so the scanner's short not-seen eviction (~11s) fires constantly
        # even while the device is very much present — dropping the discovery here would race
        # commissioning. pair_device removes it on success; stop() clears the rest.
        return

    async def _matter_did_discover_ble(self, info: BLEDiscoveryInfo):
        parsed = self._parse_commissionable_ble(info)
        if not parsed:
            return
        discriminator, vendor_id, product_id = parsed
        discovery_id = self._ble_discovery_id(discriminator, vendor_id, product_id)
        self._ble_addresses.setdefault(discovery_id, set()).add(info.device.address)
        if discovery_id in self._majordom_descoveries:
            return  # already advertised; the device keeps beaconing while its window is open
        discovery = Discovery(
            id=discovery_id,
            integration=NonEmptyStr(self.name),
            # A commissionable device accepts its manual pairing code (or QR). pair_device's BLE
            # branch feeds the code to commission_with_code, which does its own BLE scan by discriminator.
            expected_credentials_options=[CredentialsType.code.with_mask("DDDD-DDD-DDDD"), CredentialsType.qr],
            expiration=None,
            transport=NonEmptyStr("BLE"),
            device_name=NonEmptyStr(info.advertisement.local_name or f"Matter {vendor_id:04x}:{product_id:04x}"),
            device_manufacturer=f"Vendor {vendor_id}",
            device_category=None,
            device_icon=None,
        )
        self._majordom_descoveries[discovery_id] = discovery
        await self.dependencies.output.controller_did_receive_discovery(self, discovery)

    @staticmethod
    def _parse_commissionable_ble(info: BLEDiscoveryInfo) -> tuple[int, int, int] | None:
        """Decode a Matter commissionable BLE advert's service-data payload → (discriminator, vid, pid).

        Payload (Matter spec 5.4.2.5.6): [0]=opcode(0x00 Commissionable), [1:3]=discriminator (u16 LE,
        low 12 bits), [3:5]=vendor id (u16 LE), [5:7]=product id (u16 LE), [7]=additional-data flag.
        """
        data = info.advertisement.service_data.get("0000fff6-0000-1000-8000-00805f9b34fb")
        if not data or len(data) < 7 or data[0] != 0x00:
            return None
        discriminator = (data[1] | (data[2] << 8)) & 0x0FFF
        vendor_id = data[3] | (data[4] << 8)
        product_id = data[5] | (data[6] << 8)
        return discriminator, vendor_id, product_id

    def _ble_discovery_id(self, discriminator: int, vendor_id: int, product_id: int) -> UUID:
        # Stable across the device's (possibly changing) BLE address and across mDNS re-discovery
        # after it joins Thread — keyed on the commissioning identity, not the transport address.
        return self._mapper.discovery_uuid(f"ble_{vendor_id:04x}_{product_id:04x}_{discriminator:03x}")

    # -------------------------------------------------------------------------
    # Device -> Hub: subscriptions & availability
    # -------------------------------------------------------------------------

    def _subscription(self, device_id: UUID, node: MatterNode):
        """
        Registers attribute-change callbacks for every attribute on the node.
        Uses _make_callback to capture (device_id, parameter_id) by value —
        without it all callbacks would share the last loop iteration's values.
        """
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster in endpoint.clusters.items():
                if not hasattr(cluster, "Attributes"):
                    continue
                for _, attribute in inspect.getmembers(cluster.Attributes, inspect.isclass):
                    if not issubclass(attribute, ClusterAttributeDescriptor):
                        continue
                    attribute_path = f"{endpoint_id}/{cluster_id}/{attribute.attribute_id}"
                    parameter_id = self._mapper.attribute_parameter_uuid(
                        device_id, endpoint_id, cluster_id, attribute.attribute_id
                    )
                    self._matter_client.subscribe_events(
                        self._make_callback(device_id, parameter_id, cluster_id, attribute.attribute_id),
                        EventType.ATTRIBUTE_UPDATED,
                        node.node_id,
                        attribute_path,
                    )

        # node.available flips when the device drops off/rejoins the Matter fabric while
        # the Hub keeps running (not just at Hub startup) — NODE_UPDATED fires on that and
        # other node-info changes, so track the last known value and only act on a real
        # transition instead of re-signalling on every unrelated update.
        self._node_availability[device_id] = node.available
        self._matter_client.subscribe_events(
            self._make_availability_callback(device_id),
            EventType.NODE_UPDATED,
            node.node_id,
        )

    def _make_callback(self, device_id: UUID, parameter_id: UUID, cluster_id: int, attribute_id: int):
        def callback(event_type, new_value):
            event = DeviceParameterChange(
                device_id=device_id,
                parameter_id=parameter_id,
                value=self._mapper.normalize_value(
                    self._mapper.apply_attribute_scale(cluster_id, attribute_id, new_value)
                ),
            )
            asyncio.create_task(self.dependencies.output.controller_did_receive_events(self, [event]))

        return callback

    def _make_availability_callback(self, device_id: UUID):
        def callback(event_type, updated_node: MatterNode):
            was_available = self._node_availability.get(device_id)
            is_available = updated_node.available
            if is_available == was_available:
                return
            self._node_availability[device_id] = is_available
            if is_available:
                asyncio.create_task(self.dependencies.output.controller_did_connect_device(self, device_id))
            else:
                asyncio.create_task(self.dependencies.output.controller_did_lose_device(self, device_id))

        return callback

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _require_matter_client(self) -> MatterClient:
        if not self._matter_client:
            raise MatterConnectionError("Matter client is not started")
        return self._matter_client

    def _require_node(self, device: MatterDevice) -> MatterNode:
        node = self._require_matter_client().get_node(device.node_id)
        if not node:
            raise MatterUnexpectedError(f"Node for device {device.node_id} not found")
        return node

    async def _execute_cluster_command(
        self, node, endpoint_id: int, cluster, parameter: MatterParameter, command: DeviceCommand
    ):
        command_id = parameter.integration_data.command_id
        time_requested_timeout = None
        if command_id is None or command_id < 0:
            raise MatterUnexpectedError(f"Invalid command_id: {command_id}")

        if not hasattr(cluster, "Commands"):
            return

        # A value-less send (e.g. tapping the main parameter) falls back to the arguments this
        # command was set up with as a main parameter — see integration_data.default_arguments.
        arguments = command.value if command.value is not None else parameter.integration_data.default_arguments

        for _, cmd_class in inspect.getmembers(cluster.Commands, inspect.isclass):
            if not issubclass(cmd_class, ClusterCommand):
                continue
            if getattr(cmd_class, "command_id", -1) != command_id:
                continue

            # Only send client-side commands
            if not getattr(cmd_class, "is_client", True):
                continue

            if cluster.id == 0x00000101:  # DoorLock
                time_requested_timeout = 1000
            if isinstance(arguments, dict):
                data = self._mapper.parse_data_for_command(cmd_class, arguments)
                await self._matter_client.send_device_command(
                    node.node_id, endpoint_id, cmd_class(**data), timed_request_timeout_ms=time_requested_timeout
                )
            else:
                await self._matter_client.send_device_command(
                    node.node_id, endpoint_id, cmd_class(), timed_request_timeout_ms=time_requested_timeout
                )
            return

    async def _write_cluster_attribute(
        self, node, endpoint_id: int, cluster_id: int, parameter: MatterParameter, command: DeviceCommand
    ):
        if parameter.role != ParameterRole.control:
            raise MatterUnexpectedError(f"Parameter '{parameter.name}' is not writable")

        attribute_path = f"{endpoint_id}/{cluster_id}/{parameter.integration_data.attribute_id}"
        if attribute_path not in node.node_data.attributes:
            return

        endpoint = node.endpoints[endpoint_id]
        cluster = endpoint.clusters[cluster_id]
        attribute_cls = next(
            (
                a
                for _, a in inspect.getmembers(cluster.Attributes, inspect.isclass)
                if issubclass(a, ClusterAttributeDescriptor)
                and getattr(a, "attribute_id", -1) == parameter.integration_data.attribute_id
            ),
            None,
        )

        value = self._mapper.parse_data_for_attribute(attribute_cls, command.value) if attribute_cls else command.value
        await self._matter_client.write_attribute(node.node_id, attribute_path, value)

    def _get_main_parameter(self, device_id: UUID, node: MatterNode) -> tuple[UUID | None, DefaultParams]:
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, spec in MAIN_PARAMETER_BY_CLUSTER.items():
                if cluster_id not in endpoint.clusters:
                    continue

                if cluster_id == 0x00000202:  # FanControl
                    supported_attribute_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFFB) or []
                    if supported_attribute_ids and spec.command_or_attribute_id not in supported_attribute_ids:
                        continue
                    return (
                        self._mapper.attribute_parameter_uuid(
                            device_id, endpoint_id, cluster_id, spec.command_or_attribute_id
                        ),
                        spec.default_params,
                    )

                accepted_command_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFF9) or []
                if accepted_command_ids and spec.command_or_attribute_id not in accepted_command_ids:
                    continue

                return (
                    self._mapper.command_parameter_uuid(
                        device_id, endpoint_id, cluster_id, spec.command_or_attribute_id
                    ),
                    spec.default_params,
                )
        return None, None
