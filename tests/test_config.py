"""matterjs-server's port is the runtime's to choose: the compose and the endpoint follow it."""

from types import SimpleNamespace

from majordom_integration_sdk import Endpoint

from majordom_matter.config import matterjs_server
from majordom_matter.controller import MatterController

PORTS = SimpleNamespace(api=45812)


def test_prefers_its_usual_port():
    assert matterjs_server().ports == {"api": 5580}


def test_listens_on_the_allocated_port():
    assert matterjs_server().compose["environment"]["PORT"].format(ports=PORTS) == "45812"


def test_provides_its_base_url_on_the_allocated_port():
    (provided,) = matterjs_server().provides
    assert (provided.family, provided.dialect) == ("matter", "matter-server")
    assert provided.url.format(ports=PORTS) == "ws://127.0.0.1:45812"  # the API at /ws, BLE at /ble


def test_listens_on_loopback_only():
    assert matterjs_server().compose["environment"]["LISTEN_ADDRESS"] == "127.0.0.1"


def test_the_controller_needs_the_router_not_the_hosts_bluetooth():
    ble = next(need for need in MatterController.needs if need.family == "ble")
    assert ble.accepts(Endpoint("ble", "router", "router", is_shared=True))
    assert not ble.accepts(Endpoint("ble", "bluez", "hci0"))
    assert not ble.is_required


def test_keeps_credentials_out_of_its_logs():
    assert matterjs_server().compose["environment"]["LOG_LEVEL"] == "warning"
