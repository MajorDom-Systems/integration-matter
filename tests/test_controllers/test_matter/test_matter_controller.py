import asyncio
import random
import pytest

from aiohttp import ClientSession
from starlette.websockets import WebSocketDisconnect
from uuid import UUID
from matter_server.client import MatterClient
from chip.clusters.Objects import OnOff

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd, create_matter_device, get_device_integration_data, wait_for_discovery, generate_value
from majordom_hub.config import matter_server_url


@pytest.mark.asyncio
async def test_discover_paired(start_mvd, async_client, crud, get_user_bearer):
    """Device is already in the database — discovery should be empty."""
    user = await crud.create_user()
    room = await crud.create_room()
    try:
        discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
        discovery_id = list(discoveries.keys())[0]
        r = await async_client.post('/v1/api/device', json={
                'name': 'Test Device 123',
                'note': 'test note',
                'icon': 'test icon',
                'category': 'test category',
                'discovery_id': discovery_id,
                'room_id': str(room.id),
                'credentials': 'MT:Y.K9042C00KA0648G00'
            }, headers=get_user_bearer(user.id))
        assert r.status_code == 200
        r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
        assert r.status_code == 200
    finally:
        await unpair_mvd()
    assert r.json() == {}

@pytest.mark.asyncio
async def test_discover_unpaired(start_mvd, async_client, crud, get_user_bearer):
    """Device is in the fabric but not in the database — discovery should not be empty."""
    user = await crud.create_user()
    # Wait until mvd announces itself over mDNS and our system picks it up
    discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
    assert discoveries != {}
    for discovery in discoveries.values():
        assert discovery["device_manufacturer"] == "Vendor 65521"

@pytest.mark.asyncio
async def test_pair_device(start_mvd, async_client, crud, get_user_bearer):
    """User adds a device through discovery."""
    user = await crud.create_user()
    room = await crud.create_room()

    # Wait until the device appears in discovery
    discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
    try:
        r = await async_client.post('/v1/api/device', json={
            'name': 'Test Device 123',
            'note': 'test note',
            'icon': 'test icon',
            'category': 'test category',
            'discovery_id': discovery_id,
            'room_id': str(room.id),
            'credentials': 'MT:Y.K9042C00KA0648G00'
        }, headers=get_user_bearer(user.id))
        assert r.status_code == 200, r.json()
    finally:
        await unpair_mvd()

@pytest.mark.asyncio
async def test_control_all_attributes(start_mvd, async_client_ws_connect, async_client, crud, get_user_bearer):
    """
    Iterates over all setting-visible attributes on the device and verifies
    that each one can be sent and produces a majordom_did_receive_event response.
    """
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]

    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        'credentials': 'MT:Y.K9042C00KA0648G00'
    }, headers=get_user_bearer(user.id))
    assert r.status_code == 200
    device_id = r.json()["id"]

    # Fetch all parameters for this device
    r = await async_client.get(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    # Filter only setting-visible commands with role=control
    parameters = [
        p for p in r.json()["parameters"]
        if p["integration_data"]["type"] == "attribute"
        and p["visibility"] == "setting"
        and p["role"] == "control"
    ]
    failed = []
    
    for parameter in parameters:
        parameter_id = parameter["id"]
        value = generate_value(parameter)

        msg_data = {
            "type": "device_command",
            "data": {
                "device_id": str(device_id),
                "parameter_id": str(parameter_id),
                "value": value
            }
        }

        received = False
        try:
            async with async_client_ws_connect(user.id) as ws:
                await ws.send_json(msg_data)
                async with asyncio.timeout(5):
                    while True:
                        message = await ws.receive_json()
                        if message["type"] == "majordom_did_connect_device":
                            continue
                        if message["type"] == "majordom_did_receive_event":
                            received = True
                            break
                        break
        except TimeoutError:
            pass
        except (WebSocketDisconnect, UnboundLocalError):
            pass

        if not received:
            failed.append(parameter["name"])

    assert not failed, f"The following commands did not produce an event: {failed}"




@pytest.mark.asyncio
async def test_control_all_commands(start_mvd, async_client_ws_connect, async_client, crud, get_user_bearer):
    """
    Iterates over all user-visible commands on the device and verifies
    that each one can be sent and produces a majordom_did_receive_event response.
    """
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]

    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        'credentials': 'MT:Y.K9042C00KA0648G00'
    }, headers=get_user_bearer(user.id))
    assert r.status_code == 200
    device_id = r.json()["id"]

    # Fetch all parameters for this device
    r = await async_client.get(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    # Filter only user-visible commands with role=control
    commands = [
        p for p in r.json()["parameters"]
        if p["integration_data"]["type"] == "command"
        and p["visibility"] == "user"
        and p["role"] == "control"
    ]
    failed = []

    for command_param in commands:
        parameter_id = command_param["id"]
        fields = command_param.get("fields") or []

        # Build value: None for commands with no fields, dict for commands with fields
        if not fields:
            value = None
        else:
            value = {
                field["name"]: generate_value(field)
                for field in fields
            }

        msg_data = {
            "type": "device_command",
            "data": {
                "device_id": str(device_id),
                "parameter_id": str(parameter_id),
                "value": value
            }
        }

        received = False
        try:
            async with async_client_ws_connect(user.id) as ws:
                await ws.send_json(msg_data)
                async with asyncio.timeout(5):
                    while True:
                        message = await ws.receive_json()
                        if message["type"] == "majordom_did_connect_device":
                            continue
                        if message["type"] == "majordom_did_receive_event":
                            received = True
                            break
                        break
        except TimeoutError:
            pass
        except (WebSocketDisconnect, UnboundLocalError):
            pass

        if not received:
            failed.append(command_param["name"])

    assert not failed, f"The following commands did not produce an event: {failed}"




@pytest.mark.asyncio
async def test_events(start_mvd, async_client, async_client_ws_connect, crud, get_user_bearer):
    """
    Verifies that when a device state changes externally (OnOff.On sent directly
    via a separate Matter client), our system detects the attribute change
    and forwards it as an event to the WebSocket client.
    """
    user = await crud.create_user()
    room = await crud.create_room()

    # Wait until mvd announces itself and our system creates a discovery entry
    discoveries = await wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
 
    # Commission the device into our system
    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device 123',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        'credentials': 'MT:Y.K9042C00KA0648G00'
    }, headers=get_user_bearer(user.id))
    assert r.status_code == 200
    device_id = r.json()["id"]
 
    # Find the parameter that maps to OnOff attribute (cluster 6, attribute 0)
    r = await async_client.get(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    parameter_id = next(
        p["id"] for p in r.json()["parameters"]
        if p["integration_data"]["cluster_id"] == 6
        and p["integration_data"]["attribute_id"] == 0
        and p["integration_data"]["type"] == "attribute"
    )
    node_id = (await get_device_integration_data(device_id)).get("node_id")
 
    expected_message = {
        'type': 'majordom_did_receive_event',
        'data': {
            'device_id': str(device_id),
            'parameter_id': str(parameter_id),
            'value': True
        }
    }
 
    # Create a separate Matter client that bypasses our system entirely —
    # this simulates an external trigger (e.g. physical button or another controller)
    session = ClientSession()
    app = MatterClient(matter_server_url, session)
    await app.connect()
 
    event = asyncio.Event()
    asyncio.create_task(app.start_listening(init_ready=event))
    await event.wait()
 
    messages = []
    try:
        async with async_client_ws_connect(user.id) as ws:
            try:
                # Send OnOff.On directly to the device, bypassing our system
                await app.send_device_command(node_id, 13, OnOff.Commands.On())
 
                # Our system should catch the attribute subscription update
                # and push a majordom_did_receive_event to all connected WS clients
                async with asyncio.timeout(5):
                    while True:
                        message = await ws.receive_json()
                        messages.append(message)
                        if message == expected_message:
                            break
            except TimeoutError:
                print("Timeout: event was not received within 5 seconds")
    finally:
        await app.disconnect()
        await session.close()
        await unpair_mvd()
 
    assert expected_message in messages
 
@pytest.mark.asyncio
async def test_unpair(start_mvd, async_client, crud, get_user_bearer):
    """Remove a device from the system."""
    user = await crud.create_user()
    room = await crud.create_room()
    device_id = UUID('00000000-0000-0000-0000-000000000000')
 
    node_id = await pair_mvd()
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)
 
    r = await async_client.delete(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
 