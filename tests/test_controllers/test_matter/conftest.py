import pytest
import pytest_asyncio
import os
import subprocess
import signal

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd


BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bins")

def get_all_devices():
    if not os.path.exists(BIN_DIR):
        return []
    
    devices = []
    for entry in os.scandir(BIN_DIR):
        if entry.is_file() and os.access(entry.path, os.X_OK):
            devices.append(entry.name)
    return sorted(devices)

@pytest_asyncio.fixture(scope="function", params=get_all_devices())
async def start_all_mvd(request):
    device_type = request.param
    proc = subprocess.Popen(
        [
            os.path.join(BIN_DIR, device_type),
            "--discriminator", "3840",
            "--passcode", "20202021",
            "--capabilities", "4",       # <--- keep only ON-NETWORK discovery
            "--interface-id", "eth0"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    yield proc, device_type
    await unpair_mvd()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

@pytest_asyncio.fixture(scope="function")
async def start_mvd():
    proc = subprocess.Popen(
        [
            os.path.join(BIN_DIR, "on-off-light"),
            "--discriminator", "3840",
            "--passcode", "20202021",
            "--capabilities", "4",       # <--- keep only ON-NETWORK discovery
            "--interface-id", "eth0"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    yield proc
    await unpair_mvd()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

@pytest_asyncio.fixture(scope="function")
async def start_mvd_with_pairing():
    proc = subprocess.Popen(
        [
            os.path.join(BIN_DIR, "on-off-light"),
            "--discriminator", "3840",
            "--passcode", "20202021",""
            "--capabilities", "4",       # <--- keep only ON-NETWORK discovery
            "--interface-id", "eth0"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
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