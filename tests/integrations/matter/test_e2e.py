"""Matter integration e2e — the Hub's API/ws wiring against the in-process FakeMatterClient.

No matter-server, no MVD binaries, no docker: the fake (majordom_matter.testing) stands in for
the matter-server client, backed by a canned on-off-light node, so these run in the default CI.
They verify the Hub wires the Matter integration correctly — discovery listing, pairing via the
device API, parameter mapping, and command control over the websocket. Exhaustive per-device
coverage (all 30 device types, real commissioning) lives in integration-matter's dockerized MVD
suite; real Thread hardware lives in test_hardware.py.
"""

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.integrations.matter import helper


@pytest.mark.asyncio
async def test_discover_unpaired(async_client, crud, get_user_bearer):
    """The canned node is on the fabric but not in the DB — it should surface as a discovery."""
    user = await crud.create_user()
    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    assert discoveries != {}
    for discovery in discoveries.values():
        assert discovery["integration"] == "Matter"
        assert discovery["transport"] == "IP"


@pytest.mark.asyncio
async def test_pair_device_maps_parameters(async_client, crud, get_user_bearer):
    """Pairing via the device API commissions the node and maps its parameters."""
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]

    r = await async_client.post(
        "/v1/api/device",
        json={
            "name": "Test Device 123",
            "note": "test note",
            "icon": "test icon",
            "category": "test category",
            "discovery_id": discovery_id,
            "room_id": str(room.id),
            "credentials": {"type": "code", "value": "20202021"},
        },
        headers=get_user_bearer(user.id),
    )
    assert r.status_code == 200, r.json()
    device_id = r.json()["id"]

    r = await async_client.get(f"/v1/api/device/{device_id}", headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert len(r.json()["parameters"]) > 0, "the on-off-light should map at least one parameter"


@pytest.mark.asyncio
async def test_discover_paired_is_empty(async_client, crud, get_user_bearer):
    """Once the only node is paired, discovery is empty (nothing left to adopt)."""
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
    r = await async_client.post(
        "/v1/api/device",
        json={
            "name": "Paired",
            "note": "",
            "icon": "",
            "category": "",
            "discovery_id": discovery_id,
            "room_id": str(room.id),
            "credentials": {"type": "code", "value": "20202021"},
        },
        headers=get_user_bearer(user.id),
    )
    assert r.status_code == 200

    r = await async_client.get("/v1/api/device/discoveries", headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert r.json() == {}


@pytest.mark.asyncio
async def test_control_command_over_ws(async_client, async_client_ws_connect, crud, get_user_bearer):
    """A device_command sent over the websocket reaches the controller and yields an event."""
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
    r = await async_client.post(
        "/v1/api/device",
        json={
            "name": "Controllable",
            "note": "",
            "icon": "",
            "category": "",
            "discovery_id": discovery_id,
            "room_id": str(room.id),
            "credentials": {"type": "code", "value": "20202021"},
        },
        headers=get_user_bearer(user.id),
    )
    assert r.status_code == 200
    device_id = r.json()["id"]

    r = await async_client.get(f"/v1/api/device/{device_id}", headers=get_user_bearer(user.id))
    # A writable (control) attribute parameter — the OnOff cluster maps one.
    control = next(
        p for p in r.json()["parameters"] if p["role"] == "control" and p["integration_data"]["type"] == "attribute"
    )

    received = False
    try:
        async with async_client_ws_connect(user.id) as ws:
            await ws.send_json(
                {
                    "type": "device_command",
                    "data": {
                        "device_id": str(device_id),
                        "parameter_id": str(control["id"]),
                        "value": helper.generate_value(control),
                    },
                }
            )
            async with asyncio.timeout(5):
                while True:
                    message = await ws.receive_json()
                    if message["type"] == "majordom_did_connect_device":
                        continue
                    if message["type"] == "majordom_did_receive_event":
                        received = True
                    break
    except (TimeoutError, WebSocketDisconnect, UnboundLocalError):
        pass

    assert received, "device_command should produce a majordom_did_receive_event"
