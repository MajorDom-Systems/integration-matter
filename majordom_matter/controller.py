import asyncio
import contextlib
import inspect
import logging
from typing import override
from uuid import UUID

from aiohttp import ClientSession
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor, ClusterCommand
from chip.clusters.Objects import ClosureControl, Identify, OperationalState
from majordom_integration_sdk import Need
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryInfo, BLEDiscoveryService
from majordom_integration_sdk.schemas.base import NonEmptyStr
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import CredentialsType, Discovery, ProvidedCredentials
from majordom_integration_sdk.schemas.event import DeviceParameterChange
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole, ParameterVisibility
from matter_server.client.models.node import MatterNode
from matter_server.common.errors import UnknownError
from matter_server.common.models import CommissionableNodeData, EventType, MatterNodeData

from majordom_matter.ble_backend import MatterServerBLEBackend
from majordom_matter.client import MatterClient
from majordom_matter.config import MATTER_SERVER

from .exceptions import (
    MatterConnectionError,
    MatterNotFoundParameter,
    MatterUnexpectedError,
    MatterUnsupportedParameter,
)
from .mapper import MatterMapper
from .matter_spec import (
    CLOSURE_MAIN_STATE,
    MAIN_PARAMETER_BY_CLUSTER,
    STATE_TOGGLE_ATTRIBUTES,
    STATE_TOGGLE_COMMANDS,
    AttributeKey,
    DefaultParams,
    MainParameterSpec,
)
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

    matter-server owns the Matter fabric and commissioning, over the hub's Bluetooth
    (`MatterServerBLEBackend`) and a Thread border router; this controller adapts it to the
    Hub's AbstractController contract, talking to it over its WebSocket API. On-network devices
    are discovered via matter-server; BLE-only commissionable devices are discovered via the
    Hub's shared BLE service. See readme.md.
    """

    needs = [Need("matter", ["ws"])]
    services = [MATTER_SERVER]

    _matter_client: MatterClient
    _matter_client_session: ClientSession
    _majordom_descoveries: dict[UUID, Discovery]
    _mapper: MatterMapper

    # Matter commissionable BLE service (spec 5.4.2.5.6) — devices in commissioning mode advertise
    # this service + its service-data payload. discover_commissionable_nodes only surfaces IP/mDNS
    # devices, so BLE-only devices (a factory-fresh bulb not yet on any network) are discovered here
    # via the shared BLE scanner instead.
    _MATTER_COMMISSIONABLE_SERVICE = UUID("0000fff6-0000-1000-8000-00805f9b34fb")

    _RECONNECT_DELAY = 1.0  # doubles per failed attempt, up to _RECONNECT_DELAY_MAX
    _RECONNECT_DELAY_MAX = 30.0

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
        listener = await self._connect()
        self._background_tasks.append(asyncio.create_task(self._matter_discovery_loop()))

        # matter-server commissions over Bluetooth through the hub's radios, not its own
        ble_url = self.dependencies.links["matter"].url.removesuffix("/ws") + "/ble"
        ble_backend = MatterServerBLEBackend(
            ble_url, self.dependencies.ble_discovery_service, self._matter_client_session
        )
        self._background_tasks.append(asyncio.create_task(ble_backend.run()))

        # BLE discovery for commissionable devices not yet on any IP network (the mDNS-based
        # discovery loop above can't see them). No-op in virtual mode where the BLE service is off.
        self._ble_addresses: dict[UUID, set[str]] = {}  # discovery_id -> live BLE addresses
        self._ble_cancel = self.dependencies.ble_discovery_service.register(self, {self._MATTER_COMMISSIONABLE_SERVICE})

        await self._sync_with_server()
        self._background_tasks.append(asyncio.create_task(self._stay_connected(listener)))

    async def _sync_with_server(self):
        """Hands matter-server the networks and follows the devices on its fabric; on every (re)connect."""
        # Networks new devices get commissioned onto.
        if wifi := self.dependencies.context.wifi:
            await self._matter_client.set_wifi_credentials(wifi.ssid, wifi.password)
        if dataset := self.dependencies.context.thread_dataset:
            await self._matter_client.set_thread_operational_dataset(dataset)

        device_nodes: list[int] = []
        nodes = {node.node_id: node for node in self._matter_client.get_nodes()}  # get_node raises for a missing one
        async with self.dependencies.make_device_repository() as device_repository:
            for device in await device_repository.get_all(as_=MatterDevice):
                if node := nodes.get(device.integration_data.node_id):
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

    async def _connect(self) -> asyncio.Task:
        """Connects a fresh client to matter-server; the returned listener runs until matter-server goes away."""
        self._matter_client = MatterClient(self.dependencies.links["matter"].url, self._matter_client_session)
        await self._matter_client.connect()

        init_ready = asyncio.Event()
        listener = asyncio.create_task(self._matter_client.start_listening(init_ready=init_ready))
        ready = asyncio.create_task(init_ready.wait())
        try:
            await asyncio.wait({ready, listener}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            if not init_ready.is_set():
                listener.cancel()
        if not init_ready.is_set():
            raise MatterConnectionError("matter-server went away before it was ready")
        return listener

    async def _stay_connected(self, listener: asyncio.Task):
        """Reconnects whenever matter-server goes away (e.g. its container is recreated) until stop() cancels it,
        so the integration heals by itself instead of being restarted by the hub."""
        try:
            while True:
                try:
                    await listener
                except Exception as e:
                    logging.warning(f"[{self.name}] matter-server listener failed: {e!r}")
                message = "Lost the connection to the Matter server, reconnecting."
                await self.dependencies.output.controller_did_encounter_error(self, message, still_running=True)
                for device_id, available in self._node_availability.items():
                    if available:
                        await self.dependencies.output.controller_did_lose_device(self, device_id)
                self._node_availability = dict.fromkeys(self._node_availability, False)

                delay = self._RECONNECT_DELAY
                while True:
                    await asyncio.sleep(delay)
                    try:
                        listener = await self._connect()
                        await self._sync_with_server()
                        break
                    except Exception as e:
                        listener.cancel()
                        logging.info(f"[{self.name}] matter-server is not back yet: {e!r}")
                        delay = min(delay * 2, self._RECONNECT_DELAY_MAX)
                logging.info(f"[{self.name}] reconnected to matter-server")
                for device_id, available in self._node_availability.items():
                    if available:
                        await self.dependencies.output.controller_did_connect_device(self, device_id)
        finally:
            listener.cancel()

    # -------------------------------------------------------------------------
    # Hub -> device operations
    # -------------------------------------------------------------------------

    async def _commission(self, value: str, network_only: bool) -> MatterNodeData:
        """Route by what the user typed, whichever credentials type the UI collected it under.

        `MT:` is a QR payload; 11 or 21 digits are a manual pairing code; 8 digits are the raw
        setup PIN, which only commission_on_network takes and which carries no discriminator
        for finding a BLE-only device.
        """
        if value.strip().upper().startswith("MT:"):
            return await self._matter_client.commission_with_code(value.strip(), network_only=network_only)
        digits = "".join(char for char in value if char.isdigit())
        match len(digits):
            case 8:
                if not network_only:
                    raise ValueError(
                        "A raw 8-digit setup PIN only works for devices already on the network; "
                        "enter the 11-digit manual pairing code or scan the QR code for a Bluetooth device"
                    )
                return await self._matter_client.commission_on_network(int(digits))
            case 11 | 21:
                return await self._matter_client.commission_with_code(digits, network_only=network_only)
            case _:
                raise ValueError(
                    f"Unrecognised Matter pairing code ({len(digits)} digits): expected an 11- or 21-digit "
                    "manual pairing code, an 8-digit setup PIN, or an MT: QR payload"
                )

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

        # matter-server may run without a Bluetooth adapter; then a non-network_only commission fails
        # with "Bluetooth commissioning is not available", so only BLE discoveries may use BLE.
        network_only = discovery.transport != "BLE"

        # Compare by value (==), not identity (is): `credentials.type` can arrive through a
        # schema layer that resolved CredentialsType via a different import path, yielding a
        # distinct enum class where `is` would never match. `==` on a str-enum compares the
        # underlying value and is robust to that (and semantically what we want anyway).
        if credentials.type not in (CredentialsType.qr, CredentialsType.code):
            raise MatterUnexpectedError("This credentials type is not supported")
        commission_node = await self._commission(str(credentials.value), network_only=network_only)

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
                            device.parameters.append(
                                MatterParameterState(
                                    **parameter.__dict__,
                                    value=self._mapper.attribute_value(cluster_id, attribute_id, value),
                                )
                            )

            main_parameter, default_value = self._get_main_parameter(device.id, node, device.parameters)
            device.main_parameter = main_parameter.id if main_parameter is not None else None
            if main_parameter is not None and default_value is not None:
                if main_parameter.integration_data.type is MatterParameterTypeEnum.attribute:
                    main_parameter.default_value = self._mapper.normalize_value(default_value)
                elif isinstance(default_value, dict):
                    # A command main parameter is tapped with a fixed argument set (a dict).
                    main_parameter.integration_data.default_arguments = default_value
                else:
                    raise MatterUnexpectedError(
                        f"Command main parameter {main_parameter.id} expected dict default arguments, "
                        f"got {type(default_value).__name__}: {default_value!r}"
                    )
            if main_parameter is not None and main_parameter.data_type is ParameterDataType.struct:
                # A struct is a one-tap main parameter only with a default_value: the id-keyed
                # arguments a tap sends.
                arguments = main_parameter.integration_data.default_arguments or {}
                main_parameter.default_value = {
                    str(f.id): arguments[f.name] for f in main_parameter.fields or () if f.name in arguments
                }

            await device_repository.save(device, discovery.id)

        await self.dependencies.output.controller_did_connect_device(self, device_id)
        self._subscription(device_id, node)
        return device_id

    async def unpair(self, device: MatterDevice):
        client = self._require_matter_client()
        # a device that never finished commissioning (or that matter-server lost) has no node to remove
        if any(node.node_id == device.node_id for node in client.get_nodes()):
            await client.remove_node(device.node_id)

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
                    parameter_id = self._mapper.attribute_parameter_uuid(
                        device.id, endpoint_id, cluster_id, attribute_id
                    )
                    events.append(
                        DeviceParameterChange(
                            device_id=device.id,
                            parameter_id=parameter_id,
                            value=self._mapper.attribute_value(cluster_id, attribute_id, value),
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
                attribute_id = parameter.integration_data.attribute_id
                if attribute_id is not None and AttributeKey(cluster_id, attribute_id) in STATE_TOGGLE_ATTRIBUTES:
                    cluster_command = self._state_toggle_command(
                        node, endpoint_id, cluster, attribute_id, command.value
                    )
                    await self._matter_client.send_device_command(
                        node.node_id,
                        endpoint_id,
                        cluster_command,
                        timed_request_timeout_ms=1000 if cluster_command.must_use_timed_invoke else None,
                    )
                else:
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
    # Device -> Hub: BLE discovery (BLEDiscoveryListener) — commissionable devices not yet on IP
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
                value=self._mapper.attribute_value(
                    cluster_id, attribute_id, self._mapper.parse_raw_attribute(cluster_id, attribute_id, new_value)
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

            time_requested_timeout = 1000 if cmd_class.must_use_timed_invoke else None
            if isinstance(arguments, dict):
                arguments = self._mapper.arguments_by_name(
                    command.device_id, endpoint_id, cluster.id, command_id, cmd_class, arguments
                )
                data = self._mapper.parse_data_for_command(cmd_class, arguments)
                await self._matter_client.send_device_command(
                    node.node_id, endpoint_id, cmd_class(**data), timed_request_timeout_ms=time_requested_timeout
                )
            else:
                await self._matter_client.send_device_command(
                    node.node_id, endpoint_id, cmd_class(), timed_request_timeout_ms=time_requested_timeout
                )
            return

    @staticmethod
    def _state_toggle_command(node: MatterNode, endpoint_id: int, cluster, attribute_id: int, value) -> ClusterCommand:
        """The command that sets a read-only state attribute to `value` (see STATE_TOGGLE_ATTRIBUTES)."""
        current = node.get_attribute_value(endpoint_id, cluster.id, attribute_id)
        key = AttributeKey(cluster.id, attribute_id)
        if key == CLOSURE_MAIN_STATE:
            if current in (
                ClosureControl.Enums.MainStateEnum.kMoving,
                ClosureControl.Enums.MainStateEnum.kWaitingForMotion,
            ):
                return ClosureControl.Commands.Stop()
            overall = node.get_attribute_value(
                endpoint_id, cluster.id, ClosureControl.Attributes.OverallCurrentState.attribute_id
            )
            closed = getattr(overall, "position", None) == ClosureControl.Enums.CurrentPositionEnum.kFullyClosed
            target = ClosureControl.Enums.TargetPositionEnum
            return ClosureControl.Commands.MoveTo(
                position=target(target.kMoveToFullyOpen if closed else target.kMoveToFullyClosed)
            )
        if key == AttributeKey(
            OperationalState.id, OperationalState.Attributes.OperationalState.attribute_id
        ) and current not in (
            OperationalState.Enums.OperationalStateEnum.kRunning,
            OperationalState.Enums.OperationalStateEnum.kPaused,
        ):
            raise MatterUnexpectedError(
                "OperationalState only pauses or resumes a running cycle; it never starts one from idle"
            )
        command_id = STATE_TOGGLE_COMMANDS[key].get(value)
        if command_id is None:
            raise MatterUnexpectedError(f"{cluster.__class__.__name__} state {value!r} cannot be set from MajorDom")
        return next(
            command_class
            for _, command_class in inspect.getmembers(cluster.Commands, inspect.isclass)
            if issubclass(command_class, ClusterCommand) and command_class.command_id == command_id
        )()

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

    def _get_main_parameter(
        self, device_id: UUID, node: MatterNode, parameters: list[MatterParameterState]
    ) -> tuple[MatterParameterState | None, DefaultParams]:
        # Only `user` parameters can be main (see can_be_main_parameter, which also needs the
        # default_value set from these defaults) — skip hidden ones so they don't shadow a later match.
        candidates = {p.id: p for p in parameters if p.visibility is ParameterVisibility.user}
        for endpoint_id, endpoint in node.endpoints.items():
            for cluster_id, cluster_spec in MAIN_PARAMETER_BY_CLUSTER.items():
                if cluster_id not in endpoint.clusters:
                    continue
                # An empty list means the device didn't report it: assume supported.
                supported_attribute_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFFB) or []
                accepted_command_ids: list[int] = node.get_attribute_value(endpoint_id, cluster_id, 0xFFF9) or []
                spec: MainParameterSpec | None = cluster_spec
                while spec is not None:
                    spec_id = spec.command_or_attribute_id
                    if spec.is_attribute:
                        # A state toggle also needs every command it taps through.
                        needed_command_ids = STATE_TOGGLE_COMMANDS.get(AttributeKey(cluster_id, spec_id), {}).values()
                        supported = (not supported_attribute_ids or spec_id in supported_attribute_ids) and (
                            not accepted_command_ids or set(needed_command_ids) <= set(accepted_command_ids)
                        )
                        parameter_id = self._mapper.attribute_parameter_uuid(
                            device_id, endpoint_id, cluster_id, spec_id
                        )
                    else:
                        supported = not accepted_command_ids or spec_id in accepted_command_ids
                        parameter_id = self._mapper.command_parameter_uuid(device_id, endpoint_id, cluster_id, spec_id)
                    if supported and parameter_id in candidates:
                        return candidates[parameter_id], spec.default_params
                    spec = spec.fallback
        return None, None
