"""Unit tests driving MatterController against a live matter-server + real MVD devices.

Skipped unless MATTER_INTEGRATION_TESTS=1 (set in docker/); they need a running
matter-server and the x86-64 MVD binaries, so they don't run on a bare dev machine.
"""

import asyncio
import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MATTER_INTEGRATION_TESTS") != "1",
    reason="needs matter-server + x86-64 MVD binaries — run via docker/ (plan §3.5)",
)

from majordom_integration_sdk.schemas.device import CredentialsType, ProvidedCredentials


async def _wait_for(predicate, timeout: float | None = None):
    timeout = timeout or float(os.environ.get("MATTER_DISCOVERY_TIMEOUT_S", "12"))
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.1)


async def test_discovers_a_matter_device(matter):
    controller, output, _repo, _mvd = matter
    await _wait_for(lambda: bool(output.received_discoveries))
    discovery = output.received_discoveries[-1]
    assert discovery.integration == "matter"
    assert discovery.transport == "IP"


async def _pair_draft(controller, repository, discovery):
    """The Hub's side: the draft in, the filled one saved, the device followed."""
    from majordom_matter.testing import draft_for

    draft = await controller.pair_device(
        discovery, draft_for(discovery), ProvidedCredentials(type=CredentialsType.code, value="20202021")
    )
    async with repository.session() as repo:
        await repo.save(draft)
    await controller.fetch(draft)
    return draft


async def test_pairs_a_discovered_device(matter):
    from majordom_matter.model import MatterDevice

    controller, output, repository, _mvd = matter
    await _wait_for(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))

    draft = await _pair_draft(controller, repository, discovery)

    # the draft comes back under the real physical id (derived from the commissioned node)
    device_id = draft.physical_id
    assert device_id != discovery.id
    async with repository.session() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id)
    assert device is not None and device.integration_data.node_id
    assert state is not None and len(state.parameters) > 0


async def test_unpairs_a_device(matter):
    from majordom_matter.model import MatterDevice

    controller, output, repository, _mvd = matter
    await _wait_for(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))
    draft = await _pair_draft(controller, repository, discovery)

    async with repository.session() as repo:
        device = await repo.get(draft.physical_id, as_=MatterDevice)
    assert device is not None
    await controller.unpair(device)  # removes the node from the fabric without error


async def _pair(controller, output, repository):
    from majordom_matter.model import MatterDevice

    await _wait_for(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))
    draft = await _pair_draft(controller, repository, discovery)
    async with repository.session() as repo:
        return await repo.get(draft.physical_id, as_=MatterDevice)


async def test_fetch_emits_events(matter):
    controller, output, repository, _mvd = matter
    device = await _pair(controller, output, repository)
    before = len(output.events)
    await controller.fetch(device)
    assert len(output.events) > before, "fetch should re-read the device and emit parameter-change events"


async def test_identify_runs_against_the_device(matter):
    controller, output, repository, _mvd = matter
    device = await _pair(controller, output, repository)
    # The on-off-light carries the Identify cluster; identify() should complete without error.
    await controller.identify(device)


def _all_device_types() -> list[str]:
    """Every MVD binary fetch_mvd.sh dropped into tests/mvd — the full upstream device set."""
    d = pathlib.Path(__file__).parent / "mvd"
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


@pytest.mark.parametrize("mvd", _all_device_types(), indirect=True)
async def test_commissions_every_device_type(matter):
    """Commission every MVD device the canary fetched — known types and any brand-new one Google
    ships — so the integration's flexible mapping is exercised across the full set automatically.
    A new binary added by fetch_mvd.sh is covered here with no code change; the canary goes red
    only on a genuine commissioning/mapping failure, which is the signal to go look. (See §3.5.)"""
    from majordom_matter.model import MatterDevice

    controller, output, repository, _mvd = matter
    await _wait_for(lambda: bool(controller.discoveries))
    discovery = next(iter(controller.discoveries.values()))
    draft = await _pair_draft(controller, repository, discovery)

    device_id = draft.physical_id
    async with repository.session() as repo:
        device = await repo.get(device_id, as_=MatterDevice)
        state = await repo.state(device_id)
    assert device is not None and device.integration_data.node_id, "device should commission"
    assert state is not None and len(state.parameters) > 0, "mapping should yield parameters"
    await controller.unpair(device)
