import asyncio
import pytest
import pytest_asyncio
import os
import subprocess
import signal
import tempfile
from unittest.mock import AsyncMock, patch

from matter_server.common.models import CommissionableNodeData

from majordom_hub.config import VIRTUAL_DISABLED_SERVICES, Settings
from majordom_hub.coordinator import Coordinator
from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd


@pytest_asyncio.fixture
async def coordinator(cloud_service_mock, credentials_repo_mock):
    """Override the root coordinator to re-enable MatterController.

    Matter is in VIRTUAL_DISABLED_SERVICES (off in virtual/demo mode and by default in
    tests), so the matter tests turn it back on here — mirroring how the zigbee/homekit
    conftests re-enable their own controllers. Reuses the root cloud/credentials mocks.
    """
    with patch("majordom_hub.coordinator.ServerService.start", new_callable=AsyncMock):
        c = Coordinator(settings=Settings(disable_services=VIRTUAL_DISABLED_SERVICES - {"MatterController"}))
        await c.start(wait_forever=False)
        yield c
        await c.stop()


# Node ids that need more than the function-level @pytest.mark.flaky(reruns=1). door-lock
# and window-covering are the flakiest command tests: door-lock is the longest sequence
# (~18 commands, whose tail commands hit event-delivery timeouts), and window-covering's
# movement commands are intermittently rejected by the device state machine
# (InteractionModelError Failure(0x1)). A single rerun sometimes isn't enough for either, so
# give just these two an extra retry (see also the wider per-command WS wait for them in
# test_control_all_commands). The rest stay at reruns=1.
_EXTRA_RERUN_NODES = {
    "test_control_all_commands[door-lock]": 2,
    "test_control_all_commands[window-covering]": 2,
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        for suffix, reruns in _EXTRA_RERUN_NODES.items():
            if item.nodeid.endswith(suffix):
                # append=False → this becomes the "closest" flaky marker, overriding the
                # function-level reruns=1 (verified: pytest-rerunfailures reads the closest).
                item.add_marker(pytest.mark.flaky(reruns=reruns), append=False)


BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bins")

# How long to give the MVD binary to either crash or start listening before we
# consider it alive. Keeps a crash-on-startup failing fast with a clear message
# instead of only surfacing ~4s later as an opaque "no devices discovered" from
# wait_for_discovery.
_MVD_LIVENESS_CHECK_S = 0.3

def get_all_devices():
    if not os.path.exists(BIN_DIR):
        return []

    devices = []
    for entry in os.scandir(BIN_DIR):
        if entry.is_file() and os.access(entry.path, os.X_OK):
            devices.append(entry.name)
    return sorted(devices)

async def _start_mvd(device_type: str) -> subprocess.Popen:
    # stderr goes to a temp file rather than DEVNULL/PIPE: a file never blocks
    # the writer regardless of whether/when we read it back (unlike PIPE, which
    # can deadlock the child once its output fills the OS pipe buffer), while
    # still letting us surface a real error if the process dies on startup.
    stderr_file = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [
            os.path.join(BIN_DIR, device_type),
            "--discriminator", "3840",
            "--passcode", "20202021",
            "--capabilities", "4",       # <--- keep only ON-NETWORK discovery
            "--interface-id", "eth0"
        ],
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
        preexec_fn=os.setsid
    )
    await asyncio.sleep(_MVD_LIVENESS_CHECK_S)
    if proc.poll() is not None:
        stderr_file.seek(0)
        stderr = stderr_file.read().decode(errors="replace")
        pytest.fail(f"MVD binary '{device_type}' exited immediately (code {proc.returncode}): {stderr}")
    return proc

@pytest_asyncio.fixture(scope="function", params=get_all_devices())
async def start_all_mvd(request):
    device_type = request.param
    proc = await _start_mvd(device_type)
    yield proc, device_type
    await unpair_mvd()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

@pytest_asyncio.fixture(scope="function")
async def start_mvd():
    proc = await _start_mvd("on-off-light")
    yield proc
    await unpair_mvd()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

@pytest_asyncio.fixture(scope="function")
async def start_mvd_with_pairing():
    proc = await _start_mvd("on-off-light")
    await pair_mvd()
    yield proc
    await unpair_mvd()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


@pytest_asyncio.fixture(scope="function")
async def pair_unpair_mvd(code: str = "20202021"):
    try:
        node_id = await pair_mvd(code)
        yield node_id
        await unpair_mvd()
    except Exception as e:
        print(str(e))


# Fields for a synthetic commissionable node, standing in for whatever the real mDNS browse
# would have returned. Its only job is to make ONE discovery appear so a test can grab a
# discovery_id and POST it — every MVD advertises the same discriminator/passcode, and the
# discovery payload isn't what the coverage tests assert on, so a fixed stub is fine.
# Kept as plain kwargs (not a constructed CommissionableNodeData) so nothing matter-server-
# specific runs at collection time — the dataclass is only built inside the fixture below,
# so a field mismatch on some matter_server version can't break collection of the whole
# directory (which would take the hardware tests down with it).
_MOCK_COMMISSIONABLE_NODE_KWARGS = dict(
    instance_name="MOCKMVD00000001",
    vendor_id=0xFFF1,
    product_id=0x8000,
    commissioning_mode=1,          # 1 = open for commissioning → credentials type "code"
    device_type=0,
    device_name="Mock MVD",
    addresses=["fe80::1"],         # non-empty → transport "IP" (not "BLE")
    pairing_hint=0,
    pairing_instruction="",
)


@pytest.fixture(scope="function")
def mock_matter_discovery():
    """Replace the real mDNS browse with an instant synthetic discovery.

    The parametrized coverage tests (test_control_all_*) exist to verify attribute/command
    *mapping*, not discovery. The device schema they assert on comes from commission_on_network
    (which stays REAL against the running MVD) — the mDNS browse only supplies a discovery_id
    to POST. That browse is the slow, flaky part under emulation / across the docker bridge
    (measured 3.7–7.6s and prone to timing out), so here we patch matter-server's
    `discover_commissionable_nodes` to return the stub above immediately and deterministically.
    Pairing and command control still run for real.

    Set up before the coordinator (put it first in the test signature) so the patch is live
    when MatterController.start() kicks off its discovery loop.
    """
    node = CommissionableNodeData(**_MOCK_COMMISSIONABLE_NODE_KWARGS)
    with patch(
        "matter_server.client.client.MatterClient.discover_commissionable_nodes",
        new_callable=AsyncMock,
        return_value=[node],
    ):
        yield