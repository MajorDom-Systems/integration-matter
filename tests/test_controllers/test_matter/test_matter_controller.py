import asyncio
import random
import pytest

from aiohttp import ClientSession
from starlette.websockets import WebSocketDisconnect
from uuid import UUID
from matter_server.client import MatterClient
from chip.clusters.Objects import OnOff

from tests.test_controllers.test_matter.helper import pair_mvd, unpair_mvd, create_matter_device, get_device_integration_data

# from majordom_hub.config import matter_server_url


@pytest.mark.asyncio
async def test_discover_paired(start_mvd_with_pairing, async_client, crud, get_user_bearer):
    user = await crud.create_user()
    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert r.json() == {}

@pytest.mark.asyncio
async def test_discover_unpaired(start_mvd, async_client, crud, get_user_bearer):
    await asyncio.sleep(10)
    user = await crud.create_user()

    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert r.json() != {}

@pytest.mark.asyncio
async def test_pair_device(start_mvd, async_client, crud, get_user_bearer):
    await asyncio.sleep(10)
    user = await crud.create_user()
    room = await crud.create_room()
    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    discovery_id = list(r.json().keys())[0]

    data = {
        'name': 'Test Device 123',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        'credentials': 'MT:Y.K9042C00KA0648G00'
    }
    r = await async_client.post('/v1/api/device', json=data, headers=get_user_bearer(user.id))
    assert r.status_code == 200
    await unpair_mvd()

@pytest.mark.asyncio
async def test_control_attribute(start_mvd, pair_unpair_mvd, async_client_ws_connect, crud):
    user = await crud.create_user()
    room = await crud.create_room()
    value = random.randint(1, 254)

    device_id = UUID('00000000-0000-0000-0000-000000000000')
    parameter_id = UUID('00000000-0000-0000-0000-000000000001')

    node_id = pair_unpair_mvd
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)

    msg_data = {
        'type': 'device_command',
        'data': {
            'device_id': str(device_id),
            'parameter_id': str(parameter_id),
            'value': value
        }
    }

    message = None
    try:
        async with async_client_ws_connect(user.id) as ws:
            while True:
                await ws.send_json(msg_data)
                async with asyncio.timeout(1):
                    message = await ws.receive_json()
                if message['type'] == 'majordom_did_connect_device':
                    continue
                else:
                    break
    except WebSocketDisconnect as e:
        assert e.code == 1000
    assert message and message.get('type') == 'majordom_did_receive_event', message
    


@pytest.mark.asyncio
async def test_control_command(start_mvd, pair_unpair_mvd, async_client_ws_connect, crud):
    user = await crud.create_user()
    room = await crud.create_room()

    device_id = UUID('00000000-0000-0000-0000-000000000000')
    parameter_id = UUID('00000000-0000-0000-0000-000000000000')

    node_id = pair_unpair_mvd
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)

    msg_data = {
        'type': 'device_command',
        'data': {
            'device_id': str(device_id),
            'parameter_id': str(parameter_id),
            'value': None
        }
    }

    message = None
    try:
        async with async_client_ws_connect(user.id) as ws:
            while True:
                await ws.send_json(msg_data)
                async with asyncio.timeout(1):
                    message = await ws.receive_json()
                if message['type'] == 'majordom_did_connect_device':
                    continue
                else:
                    break
    except UnboundLocalError:
        pytest.skip("Bug with httpx_ws")
    except WebSocketDisconnect as e:
        assert e.code == 1000
    assert message and message.get('type') == 'majordom_did_receive_event', message

@pytest.mark.asyncio
async def test_events(start_mvd, async_client, async_client_ws_connect, crud, get_user_bearer):
    user = await crud.create_user()
    room = await crud.create_room()

    await asyncio.sleep(10)
    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    discovery_id = list(r.json().keys())[0]
    data = {
        'name': 'Test Device 123',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        'credentials': 'MT:Y.K9042C00KA0648G00'
    }
    r = await async_client.post('/v1/api/device', json=data, headers=get_user_bearer(user.id))
    assert r.status_code == 200
    device_id = r.json()["id"]
    r = await async_client.get(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    parameter_id = next(
        parameter["id"] for parameter in r.json()["parameters"]
        if parameter["integration_data"]["cluster_id"] == 6
        and parameter["integration_data"]["attribute_id"] == 0
        and parameter["integration_data"]["type"] == "attribute"
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

    session = ClientSession()
    app = MatterClient("ws://localhost:5580/ws", session)
    await app.connect()

    event = asyncio.Event()
    asyncio.create_task(app.start_listening(init_ready=event))
    await event.wait()

    message = None

    messages = []

    try:
        async with async_client_ws_connect(user.id) as ws:
            try:
                await app.send_device_command(node_id, 13, OnOff.Commands.On())
                async with asyncio.timeout(5):
                    while True:
                        message = await ws.receive_json()
                        messages.append(message)
                            
            except TimeoutError:
                print("Timeout error")
    finally:
        await app.disconnect()
        await session.close()
        await unpair_mvd()
    assert expected_message in messages

@pytest.mark.asyncio
async def test_unpair(start_mvd, async_client, crud, get_user_bearer):
    user = await crud.create_user()
    room = await crud.create_room()
    device_id = UUID('00000000-0000-0000-0000-000000000000')
    
    node_id = await pair_mvd()
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)

    r = await async_client.delete(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
