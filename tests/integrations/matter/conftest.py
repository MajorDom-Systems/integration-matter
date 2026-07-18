from unittest.mock import AsyncMock, patch

import pytest_asyncio

from majordom_hub.config import VIRTUAL_DISABLED_SERVICES, Settings
from majordom_hub.coordinator import Coordinator
from majordom_matter.testing import FakeMatterClient


@pytest_asyncio.fixture
async def coordinator(cloud_service_mock, credentials_repo_mock):
    """Root coordinator with MatterController re-enabled, its matter-server client faked.

    Matter is in VIRTUAL_DISABLED_SERVICES (off in virtual/demo mode and by default in tests),
    so the matter e2e turns it back on here — mirroring the zigbee/homekit conftests. It also
    swaps the real matter-server `MatterClient` for the in-process `FakeMatterClient` (from
    majordom_matter.testing), so these tests exercise the Hub's matter wiring end to end —
    discovery, pairing, mapping, commands, events through the API — with NO matter-server, NO
    MVD binaries, and NO docker. The heavy real-device coverage lives entirely in
    integration-matter's dockerized MVD suite; real Thread hardware lives in test_hardware.py.
    """
    with (
        patch("majordom_hub.coordinator.ServerService.start", new_callable=AsyncMock),
        patch("majordom_matter.controller.MatterClient", FakeMatterClient),
    ):
        c = Coordinator(settings=Settings(disable_services=VIRTUAL_DISABLED_SERVICES - {"MatterController"}))
        await c.start(wait_forever=False)
        yield c
        await c.stop()
