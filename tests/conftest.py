"""Unit tests for the Matter controller.

Drive `MatterController` directly against a live python-matter-server and real MVD `chef`
virtual devices — no Hub. The MVD binaries are Linux x86-64, so this runs in the test
container (see docker/); on Apple Silicon via Rosetta. The Hub keeps the e2e and
real-hardware coverage.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import ClientSession
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.repository import DeviceRepositoryMemory
from majordom_integration_sdk.testing import (
    FakeBLEDiscoveryService,
    FakeSSDPDiscoveryService,
    FakeZeroconfDiscoveryService,
    RecordingControllerOutput,
)
from matter_server.client import MatterClient

from majordom_matter import MatterController
from majordom_matter.config import matter_server_url

BIN_DIR = Path(__file__).parent / "mvd"
_MVD_LIVENESS_CHECK_S = 0.3


async def _start_mvd(device_type: str = "on-off-light") -> subprocess.Popen:
    stderr_file = tempfile.TemporaryFile()  # noqa: SIM115 (handed to Popen; outlives this fn)
    proc = subprocess.Popen(
        [
            str(BIN_DIR / device_type),
            "--discriminator",
            "3840",
            "--passcode",
            "20202021",
            "--capabilities",
            "4",
            "--interface-id",
            "eth0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
        preexec_fn=os.setsid,
    )
    await asyncio.sleep(_MVD_LIVENESS_CHECK_S)
    if proc.poll() is not None:
        stderr_file.seek(0)
        pytest.fail(
            f"MVD '{device_type}' exited immediately (code {proc.returncode}): "
            f"{stderr_file.read().decode(errors='replace')}"
        )
    return proc


def _kill_mvd(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


async def _stop_mvd(proc: subprocess.Popen) -> None:
    await asyncio.get_running_loop().run_in_executor(None, _kill_mvd, proc)


async def _unpair_all() -> None:
    """Remove every node from matter-server so each test starts from a clean fabric."""
    session = ClientSession()
    client = MatterClient(matter_server_url, session)
    try:
        await client.connect()
        event = asyncio.Event()
        task = asyncio.create_task(client.start_listening(init_ready=event))
        await event.wait()
        for node in list(client.get_nodes()):
            with contextlib.suppress(Exception):
                await client.remove_node(node.node_id)
        task.cancel()
    finally:
        await client.disconnect()
        await session.close()


@pytest_asyncio.fixture
async def mvd(request):
    """A running virtual device on the Matter network.

    Defaults to on-off-light; indirectly parametrize (`@pytest.mark.parametrize("mvd", [...],
    indirect=True)`) to sweep other device types — see test_all_devices.py.
    """
    device_type = getattr(request, "param", "on-off-light")
    await _unpair_all()
    proc = await _start_mvd(device_type)
    yield proc
    await _unpair_all()
    await _stop_mvd(proc)


@pytest_asyncio.fixture
async def matter(mvd):
    """Started MatterController (connected to matter-server) + recording output + repository."""
    repository = DeviceRepositoryMemory(integration="Matter")
    output = RecordingControllerOutput()
    deps = AbstractController.Dependencies(
        output=output,
        make_device_repository=repository.session,
        documents_folder=Path(tempfile.mkdtemp()),
        zeroconf_discovery_service=FakeZeroconfDiscoveryService(),
        ssdp_discovery_service=FakeSSDPDiscoveryService(),
        ble_discovery_service=FakeBLEDiscoveryService(),
    )
    controller = MatterController(deps)
    await controller.start()
    yield controller, output, repository, mvd
    await controller.stop()
