import pytest

from uuid import UUID


from tests.test_controllers.test_matter.parameters import parameters

from majordom_hub.services.controller.matter.model import MatterDevice, MatterDeviceState, MatterDeviceIntegrationData, MatterParameterTypeEnum, MatterParameterState
from majordom_hub.repository.device_repository import DeviceRepository
from majordom_hub.utils.database import create_async_session

@pytest.fixture
def create_matter_device():
    async def create(id: UUID, node_id: int, room_id: UUID):
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
    return create
