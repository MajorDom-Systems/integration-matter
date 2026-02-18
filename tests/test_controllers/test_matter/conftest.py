import asyncio
import pytest
import pytest_asyncio
import os
import time
import subprocess
import signal

from uuid import UUID
from aiohttp import ClientSession
from matter_server.client import MatterClient

from tests.test_controllers.test_matter.parameters import parameters

from majordom_hub.config import matter_server_url
from majordom_hub.services.controller.matter.model import MatterDevice, MatterDeviceState, MatterDeviceIntegrationData, MatterParameterTypeEnum, MatterParameterState
from majordom_hub.repository.device_repository import DeviceRepository
from majordom_hub.utils.database import create_async_session

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd

@pytest.fixture(scope="function")
def start_mvd():  # mvd - matter virtual device
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