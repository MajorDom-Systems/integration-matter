import asyncio

from uuid import UUID
from aiohttp import ClientSession
from matter_server.client import MatterClient

from tests.test_controllers.test_matter.parameters import parameters

# from majordom_hub.config import matter_server_url
from majordom_hub.services.controller.matter.model import MatterDevice, MatterDeviceState, MatterDeviceIntegrationData, MatterParameterState
from majordom_hub.repository.device_repository import DeviceRepository
from majordom_hub.utils.database import create_async_session


async def create_matter_device(id: UUID, node_id: int, room_id: UUID):
    async with  create_async_session() as session:
        device_repo = DeviceRepository(session)
        await device_repo.save(
            MatterDevice(
                id=id,
                integration_data=MatterDeviceIntegrationData(node_id=node_id),
                room_id=room_id,
                name="Test",
                transport="IP",
                integration="Matter",
                manufacturer="Vendor 0xfff1"
            ),
            id
        )
        device = await device_repo.state(id, MatterDeviceState)
        assert device
        for parameter in parameters:
            device.parameters.append(
                MatterParameterState.parse_obj(parameter)
            )
        await device_repo.save(device, id)

async def get_device_integration_data(device_id: UUID) -> dict:
    async with  create_async_session() as session:
        device_repo = DeviceRepository(session)
        device = await device_repo.get(device_id, MatterDevice)
        assert device

        return device.integration_data.dict()

async def pair_mvd(code: str="20202021"):
    session = ClientSession()
    app = MatterClient("ws://localhost:5580/ws", session)
    try:
        await app.connect()
        event = asyncio.Event()
        asyncio.create_task(app.start_listening(init_ready=event))
        await event.wait()
        await app.commission_on_network(20202021)
        node_id = app.get_nodes()[0].node_id
        return int(node_id)
    except Exception as e:
        print(e)
    finally:
        await app.disconnect()
        await session.close()

async def unpair_mvd(node_id: int | None = None):
    session = ClientSession()
    app = MatterClient("ws://localhost:5580/ws", session)

    try:
        await app.connect()
        event = asyncio.Event()
        asyncio.create_task(app.start_listening(init_ready=event))
        await event.wait()
        if node_id:
            await app.remove_node(node_id)
        else:
            for node in app.get_nodes():
                await app.remove_node(node.node_id)
    except Exception as e:
        print(str(e))
    finally:
        await app.disconnect()
        await session.close()