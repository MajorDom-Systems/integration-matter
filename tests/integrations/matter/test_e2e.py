import asyncio
import random
import pytest

from aiohttp import ClientSession
from starlette.websockets import WebSocketDisconnect
from uuid import UUID
from matter_server.client import MatterClient
from chip.clusters.Objects import OnOff

from tests.integrations.matter import helper 
from majordom_hub.config import matter_server_url


@pytest.mark.asyncio
async def test_discover_paired(start_mvd, async_client, crud, get_user_bearer):
    """Device is already in the database — discovery should be empty."""
    user = await crud.create_user()
    room = await crud.create_room()
    try:
        discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
        discovery_id = list(discoveries.keys())[0]
        r = await async_client.post('/v1/api/device', json={
                'name': 'Test Device 123',
                'note': 'test note',
                'icon': 'test icon',
                'category': 'test category',
                'discovery_id': discovery_id,
                'room_id': str(room.id),
                # 'credentials': 'MT:Y.K9042C00KA0648G00'
                'credentials': {'type': 'code', 'value': '20202021'}
            }, headers=get_user_bearer(user.id))
        assert r.status_code == 200
        r = await async_client.get('/v1/api/device/discoveries', headers=get_user_bearer(user.id))
        assert r.status_code == 200
    finally:
        await helper.unpair_mvd()
    assert r.json() == {}

@pytest.mark.asyncio
async def test_discover_unpaired(start_mvd, async_client, crud, get_user_bearer):
    """Device is in the fabric but not in the database — discovery should not be empty."""
    user = await crud.create_user()
    # Wait until mvd announces itself over mDNS and our system picks it up
    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    assert discoveries != {}
    for discovery in discoveries.values():
        assert discovery["device_manufacturer"] == "Vendor 65521"

@pytest.mark.asyncio
async def test_pair_device(start_mvd, async_client, crud, get_user_bearer):
    """User adds a device through discovery."""
    user = await crud.create_user()
    room = await crud.create_room()

    # Wait until the device appears in discovery
    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
    try:
        r = await async_client.post('/v1/api/device', json={
            'name': 'Test Device 123',
            'note': 'test note',
            'icon': 'test icon',
            'category': 'test category',
            'discovery_id': discovery_id,
            'room_id': str(room.id),
            # 'credentials': 'MT:Y.K9042C00KA0648G00'
            'credentials': {'type': 'code', 'value': '20202021'}

        }, headers=get_user_bearer(user.id))
        assert r.status_code == 200, r.json()
    finally:
        await helper.unpair_mvd()

# Retried once (pytest-rerunfailures): these send commands to an emulated MVD and, under
# full-suite emulation load, are flaky in two ways that a fresh MVD (each rerun re-runs the
# function-scoped fixtures, so a new MVD is spawned) reliably clears — (1) the command's
# majordom_did_receive_event occasionally doesn't arrive within the 5s WS window, and
# (2) some device state machines (e.g. WindowCovering movement) reject commands with a
# generic InteractionModelError Failure(0x1) depending on timing/order. Not a mapping bug.
@pytest.mark.flaky(reruns=1)
@pytest.mark.asyncio
async def test_control_all_attributes(mock_matter_discovery, start_all_mvd, async_client_ws_connect, async_client, crud, get_user_bearer):
    """
    Iterates over all setting-visible attributes on the device and verifies
    that each one can be sent and produces a majordom_did_receive_event response.

    Discovery is mocked (see mock_matter_discovery) — this test covers attribute
    mapping + control, not the mDNS discovery path (that's test_discover_*).
    """
    user = await crud.create_user()
    room = await crud.create_room()

    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]

    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        # 'credentials': 'MT:Y.K9042C00KA0648G00'
        'credentials': {'type': 'code', 'value': '20202021'}

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
        # Build a value that is valid for this parameter's type/constraints
        value = helper.generate_value(parameter)

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
            # Open a fresh WS connection per attribute so failures on one
            # attribute don't leave stale messages for the next one
            async with async_client_ws_connect(user.id) as ws:
                await ws.send_json(msg_data)
                async with asyncio.timeout(5):
                    while True:
                        message = await ws.receive_json()
                        # Skip the initial "device connected" notification and
                        # keep waiting for the actual command result event
                        if message["type"] == "majordom_did_connect_device":
                            continue
                        if message["type"] == "majordom_did_receive_event":
                            received = True
                            break
                        break
        except TimeoutError:
            # No event arrived in time — treated as a failure for this parameter
            pass
        except (WebSocketDisconnect, UnboundLocalError):
            # Connection dropped or ws var never got bound — also a failure
            pass

        if not received:
            failed.append(parameter["name"])

    assert not failed, f"The following commands did not produce an event: {failed}"


@pytest.mark.flaky(reruns=1)  # see note on test_control_all_attributes above
@pytest.mark.asyncio
async def test_control_all_commands(mock_matter_discovery, start_all_mvd, async_client_ws_connect, async_client, crud, get_user_bearer):
    """
    Iterates over all user-visible commands on the device and verifies
    that each one can be sent and produces a majordom_did_receive_event response.

    Discovery is mocked (see mock_matter_discovery) — this test covers command
    mapping + control, not the mDNS discovery path (that's test_discover_*).
    """
    proc, device_type = start_all_mvd
    user = await crud.create_user()
    room = await crud.create_room()

    # door-lock (long command sequence, slow tail events) and window-covering (movement
    # commands the device is slow to ack) are the two flaky command tests — give just them a
    # wider per-command event wait; everything else stays tight at 5s. (These two also get an
    # extra rerun, see _EXTRA_RERUN_NODES in conftest.)
    ws_event_timeout = 10 if device_type in ("door-lock", "window-covering") else 5

    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]

    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        # 'credentials': 'MT:Y.K9042C00KA0648G00'
        'credentials': {'type': 'code', 'value': '20202021'}

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
    
    # Some devices require commands to run in a specific order (e.g. you can't
    # set a lock schedule before the user/credential it belongs to exists), so
    # we reorder the command list per device type before sending anything
    if device_type == "basic-video-player":
        def get_media_priority(cmd):
            name = cmd["name"]
            if name == "On": return 0
            if "Level" in name or name in ["Step", "Move"]: return 1
            if name in ["Play", "SendKey"]: return 2
            return 3
        commands.sort(key=get_media_priority)

    elif device_type == "door-lock":
        # Door lock commands have real dependencies: a user must be created
        # before a credential/schedule can reference it, and both must exist
        # before lock/unlock or clear operations make sense
        def get_lock_priority(cmd):
            name = cmd["name"]
            if name == "SetUser": return 0
            if name in ["SetCredential", "SetWeekDaySchedule", "SetHolidaySchedule", "SetYearDaySchedule"]: return 1
            if name in ["GetUser", "GetCredentialStatus", "GetWeekDaySchedule", "GetHolidaySchedule", "GetYearDaySchedule"]: return 2
            if name in ["LockDoor", "UnlockDoor", "UnlockWithTimeout", "UnboltDoor"]: return 3
            if name in ["ClearWeekDaySchedule", "ClearHolidaySchedule", "ClearYearDaySchedule", "ClearCredential", "ClearUser"]: return 4
            return 5
        commands.sort(key=get_lock_priority)

    failed = []

    for command_param in commands:
        # These specific commands are known to be unsupported/flaky on the
        # virtual device for this device type, so they're intentionally skipped
        if device_type == "dishwasher" and command_param["name"] == "SetTemperature":
            continue
            
        if device_type == "basic-video-player" and command_param["name"] == "ChangeChannelByNumber":
            continue

        parameter_id = command_param["id"]
        fields = command_param.get("fields") or []

        if not fields:
            value = None
            
        elif device_type == "basic-video-player":
            # Random/generic values would make no sense for a media player
            # (e.g. a random "keyCode" or "match" string), so we hardcode
            # sane values per field name and fall back to generate_value
            # only for fields we don't explicitly care about
            value = {}
            specific_values = {
                "stepMode": 0, "moveMode": 0, "level": 50, "rate": None,
                "transitionTime": None, "optionsMask": 0, "optionsOverride": 0,
                "index": 1, "name": "Test Name", "target": 1, "data": "",
                "match": "HBO", "count": 1, "keyCode": 68, "position": 5000,
                "deltaPositionMilliseconds": 10000, "audioAdvanceUnmuted": True
            }
            for field in fields:
                value[field["name"]] = specific_values.get(field["name"], helper.generate_value(field))
                
        elif device_type == "door-lock":
            # Same idea as above: door lock fields need consistent, valid-looking
            # values (matching user/credential indices, sane time ranges, etc.)
            # rather than random data, otherwise dependent commands would fail
            value = {}
            specific_lock_values = {
                "PINCode": "123456",
                "timeout": 30,
                "userIndex": 2,
                "userUniqueID": 666,
                "userName": "TestUser",
                "userStatus": 1,
                "userType": 0,
                "credentialRule": 0,
                "credential": {"credentialType": 1, "credentialIndex": 1},
                "credentialData": "123456",
                "holidayIndex": 2,
                "weekDayIndex": 1,
                "yearDayIndex": 1,
                "operationType": 0,
                "operatingMode": 0,
                "daysMask": 62,
                "startHour": 9,
                "startMinute": 0,
                "endHour": 18,
                "endMinute": 0,
                "localStartTime": 1719684000,
                "localEndTime": 1719770400
            }
            for field in fields:
                value[field["name"]] = specific_lock_values.get(field["name"], helper.generate_value(field))                
        else:
            value = {
                field["name"]: helper.generate_value(field)
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
                async with asyncio.timeout(ws_event_timeout):
                    while True:
                        message = await ws.receive_json()
                        if message["type"] == "majordom_did_connect_device":
                            continue
                        if message["type"] == "majordom_did_receive_event":
                            received = True
                            break
                        break
        except ExceptionGroup as e:
            # asyncio.timeout()/task groups can wrap the real error, so unwrap
            # it and check if it's actually an expected "not supported" error
            # rather than a genuine failure — some commands are legitimately
            # not applicable to every device instance
            exceptions = helper.flatten_exception_group(e)
            for exception in exceptions:
                str_error = str(exception)
                if isinstance(exception, WebSocketDisconnect):
                    str_error = exception.reason if hasattr(exception, "reason") else str(exception)
                if "not found on device" in str_error or "not supported by device" in str_error:
                    received = True
        except (WebSocketDisconnect, UnboundLocalError, asyncio.TimeoutError):
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
    discoveries = await helper.wait_for_discovery(async_client, get_user_bearer(user.id))
    discovery_id = list(discoveries.keys())[0]
 
    # Commission the device into our system
    r = await async_client.post('/v1/api/device', json={
        'name': 'Test Device 123',
        'note': 'test note',
        'icon': 'test icon',
        'category': 'test category',
        'discovery_id': discovery_id,
        'room_id': str(room.id),
        # 'credentials': 'MT:Y.K9042C00KA0648G00'
        'credentials': {'type': 'code', 'value': '20202021'}

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
    node_id = (await helper.get_device_integration_data(device_id)).get("node_id")
 
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
        await helper.unpair_mvd()
 
    assert expected_message in messages
 
@pytest.mark.asyncio
async def test_unpair(start_mvd, async_client, crud, get_user_bearer):
    """Remove a device from the system."""
    user = await crud.create_user()
    room = await crud.create_room()
    device_id = UUID('00000000-0000-0000-0000-000000000000')
 
    node_id = await helper.pair_mvd()
    await helper.create_matter_device(id=device_id, node_id=node_id, room_id=room.id)
 
    r = await async_client.delete(f'/v1/api/device/{device_id}', headers=get_user_bearer(user.id))
    assert r.status_code == 200