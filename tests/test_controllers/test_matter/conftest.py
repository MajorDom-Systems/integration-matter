import pytest
import pytest_asyncio
import os
import subprocess
import signal

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd

@pytest_asyncio.fixture(scope="function")
async def start_mvd():  # mvd - matter virtual device
    proc = subprocess.Popen(
        [
            "mvd",
            "--device-type", "light",
            "--discriminator", "3840",
            "--passcode", "20202021",
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
            "mvd",
            "--device-type", "light",
            "--discriminator", "3840",
            "--passcode", "20202021",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    await pair_mvd()
    print("Pairing")
    yield proc
    await unpair_mvd()
    print("Unpairng")
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


@pytest_asyncio.fixture(scope="function")
async def pair_unpair_mvd(code: str="20202021"):
    try:
        node_id = await pair_mvd(code)
        yield node_id
        await unpair_mvd()
    except Exception as e:
        print(str(e))