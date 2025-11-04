# end-to-end test for Matter

import asyncio
import random
import pytest

from aiohttp import ClientSession
from starlette.websockets import WebSocketDisconnect
from uuid import UUID
from matter_server.client import MatterClient

from majordom_hub.config import matter_server_url


@pytest.mark.asyncio
async def test_clear(clear_db):
    await clear_db()
    assert 1


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
async def test_control_attribute(async_client_ws_connect, crud):
    user = await crud.create_user()
    value = random.randint(0, 100)

    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('150fa391-8925-5058-882e-0f20aab9ea7d') #  13/8/0

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
async def test_control_command(async_client_ws_connect, crud):
    user = await crud.create_user()

    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('6f275326-6dae-5fc5-9f46-acc864ed08cf') #  Command to use the OnOff switch

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
    except WebSocketDisconnect as e:
        assert e.code == 1000
    assert message and message.get('type') == 'majordom_did_receive_event', message


@pytest.mark.asyncio
async def test_events(async_client_ws_connect, crud):
    user = await crud.create_user()
    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')
    parameter_id = UUID('150fa391-8925-5058-882e-0f20aab9ea7d')  # 13/8/0
    value = random.randint(0, 100)
    expected_message = {
        'type': 'majordom_did_receive_event',
        'data': {
            'device_id': str(device_id),
            'parameter_id': str(parameter_id),
            'value': value
        }
    }
    message = None
    # session = ClientSession()
    # client = MatterClient(matter_server_url, session)
    # await client.connect()
    # asyncio.create_task(client.start_listening())
    # await asyncio.sleep(1)
    # node_id = client.get_nodes()[0].node_id
    try:
        async with async_client_ws_connect(user.id) as ws:
            # await client.write_attribute(node_id, "13/8/0", 30)
            while True:
                async with asyncio.timeout(1):
                    message = await ws.receive_json()
                if message['type'] == 'majordom_did_connect_device':
                    continue
                elif message == expected_message:
                    break
    except WebSocketDisconnect as e:
        assert e.code == 1000
    assert message == expected_message, message
    # await client.disconnect()
    # await session.close()


@pytest.mark.asyncio
async def test_unpair(async_client, crud, get_user_bearer):
    user = await crud.create_user()
    device_id = UUID('2df7fec5-26ef-5119-800d-3934a5840916')

    r = await async_client.delete(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200
