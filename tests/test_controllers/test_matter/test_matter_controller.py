# end-to-end test for Matter

import asyncio
import random
import pytest

from aiohttp import ClientSession
from starlette.websockets import WebSocketDisconnect
from uuid import UUID
from matter_server.client import MatterClient
from chip.clusters.Objects import OnOff

from majordom_hub.config import matter_server_url


@pytest.mark.asyncio
async def test_discover_unpaired(async_client, crud, get_user_bearer):
    await asyncio.sleep(5)
    user = await crud.create_user()

    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert r.json() != {}


@pytest.mark.asyncio
async def test_pair_device(async_client, crud, get_user_bearer):
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
    r2 = await async_client.post('/v1/api/device', json=data, headers=get_user_bearer(user.id))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_discover_paired(async_client, crud, get_user_bearer):
    user = await crud.create_user()

    r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
    assert r.status_code == 200
    assert r.json() == {}


@pytest.mark.asyncio
async def test_control_attribute(async_client_ws_connect, crud, create_matter_device):
    user = await crud.create_user()
    room = await crud.create_room()
    value = random.randint(0, 100)

    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('1b129906-a038-59ba-9d00-9a314aab4086')  # 13/8/17

    session = ClientSession()
    client = MatterClient(matter_server_url, session)
    await client.connect()
    event = asyncio.Event()
    asyncio.create_task(client.start_listening(init_ready=event))
    await event.wait()
    node_id = client.get_nodes()[0].node_id

    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)
    await client.disconnect()
    await session.close()
    
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
async def test_control_command(async_client_ws_connect, crud, create_matter_device):
    user = await crud.create_user()
    room = await crud.create_room()

    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('9ff0f12c-620e-57ab-8a80-ef41cad97bb8') #  On command 

    session = ClientSession()
    client = MatterClient(matter_server_url, session)
    await client.connect()
    event = asyncio.Event()
    asyncio.create_task(client.start_listening(init_ready=event))
    await event.wait()
    node_id = client.get_nodes()[0].node_id
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)

    await client.disconnect()
    await session.close()
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
    print(message)
    assert message and message.get('type') == 'majordom_did_receive_event', message


@pytest.mark.asyncio
async def test_events(async_client_ws_connect, crud, create_matter_device):
    user = await crud.create_user()
    room = await crud.create_room()
    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('4ff82bf1-1bd3-50d3-a0cd-9cd01c64d21a')  # 13/6/0
    expected_message= {
        'type': 'majordom_did_receive_event',
        'data': {
            'device_id': str(device_id),
            'parameter_id': str(parameter_id),
            'value': False
        }
    }
    message = None
    session = ClientSession()
    client = MatterClient(matter_server_url, session)
    try:
        await client.connect()
        event = asyncio.Event()
        asyncio.create_task(client.start_listening(init_ready=event))
        await event.wait()
        node_id = client.get_nodes()[0].node_id
        await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)
        print("Command sended")
        async with async_client_ws_connect(user.id) as ws:
            await client.send_device_command(node_id, 13, OnOff.Commands.Off())
            while True:
                async with asyncio.timeout(10):
                    message = await ws.receive_json()
                    print(message)
                if message['type'] == 'majordom_did_connect_device':
                    continue
                else:
                    break
    except WebSocketDisconnect as e:
        assert e.code == 1000
    finally:
        await client.disconnect()
        await session.close()
    assert message


@pytest.mark.asyncio
async def test_unpair(async_client, crud, get_user_bearer, create_matter_device):
    user = await crud.create_user()
    room = await crud.create_room()
    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    
    session = ClientSession()
    client = MatterClient(matter_server_url, session)
    await client.connect()
    event = asyncio.Event()
    asyncio.create_task(client.start_listening(init_ready=event))
    await event.wait()
    node_id = client.get_nodes()[0].node_id
    await create_matter_device(id=device_id, node_id=node_id, room_id=room.id)

    await client.disconnect()
    await session.close()
    r = await async_client.delete(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
