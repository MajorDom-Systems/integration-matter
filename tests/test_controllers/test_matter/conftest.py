import asyncio
import pytest
import pytest_asyncio
import os
import subprocess
import signal
import tempfile

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd


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