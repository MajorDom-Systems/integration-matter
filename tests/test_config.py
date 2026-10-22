"""matterjs-server's port is the runtime's to choose: the compose and the endpoint follow it."""

from types import SimpleNamespace

from majordom_integration_sdk import Endpoint

from majordom_matter.config import MATTERJS_SERVER
from majordom_matter.controller import MatterController

PORTS = SimpleNamespace(api=45812)


def test_prefers_its_usual_port():
    assert MATTERJS_SERVER.ports == {"api": 5580}


def test_listens_on_the_allocated_port():
    assert MATTERJS_SERVER.compose["environment"]["PORT"].format(ports=PORTS) == "45812"


def test_provides_its_url_on_the_allocated_port():
    assert MATTERJS_SERVER.provides[0].url.format(ports=PORTS) == "ws://127.0.0.1:45812/ws"


def test_provides_its_ble_proxy_on_the_allocated_port():
    assert MATTERJS_SERVER.provides[1].url.format(ports=PORTS) == "ws://127.0.0.1:45812/ble"


def test_the_controller_binds_the_ble_proxy_not_the_hosts_bluetooth():
    ble = next(need for need in MatterController.needs if need.family == "ble")
    assert ble.accepts(MATTERJS_SERVER.provides[1])
    assert not ble.accepts(Endpoint("ble", "bluez", "hci0"))


def test_keeps_credentials_out_of_its_logs():
    assert MATTERJS_SERVER.compose["environment"]["LOG_LEVEL"] == "warning"
