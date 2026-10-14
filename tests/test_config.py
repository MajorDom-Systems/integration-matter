"""matter-server's port is the runtime's to choose: the compose and the endpoint follow it."""

from types import SimpleNamespace

from majordom_matter.ble_backend import ble_url
from majordom_matter.config import MATTER_SERVER

PORTS = SimpleNamespace(api=45812)


def test_prefers_its_usual_port():
    assert MATTER_SERVER.ports == {"api": 5580}


def test_listens_on_the_allocated_port():
    assert MATTER_SERVER.compose["environment"]["PORT"].format(ports=PORTS) == "45812"


def test_provides_its_url_on_the_allocated_port():
    assert MATTER_SERVER.provides[0].url.format(ports=PORTS) == "ws://127.0.0.1:45812/ws"


def test_ble_url_follows_any_port():
    assert ble_url("ws://127.0.0.1:45812/ws") == "ws://127.0.0.1:45812/ble"
