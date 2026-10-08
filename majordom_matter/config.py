"""matter-server: the Matter controller service this integration drives over its WebSocket API."""

from majordom_integration_sdk import Endpoint, Need, Service

MATTER_SERVER = Service(
    "matter-server",
    # Thread is optional: without a border router Matter still works over Wi-Fi and Ethernet.
    needs=[Need("ip"), Need("ble", required=False), Need("thread", ["otbr-rest"], required=False)],
    provides=[Endpoint("matter", "ws", "ws://127.0.0.1:5580/ws")],
    compose={
        "image": "ghcr.io/matter-js/matterjs-server:dev",
        "network_mode": "host",
        "userns_mode": "host",
        "user": "{host.uid}:{host.gid}",  # its data folder belongs to the hub
        "environment": {"STORAGE_PATH": "/data", "NOBLE_BINDINGS": "dbus", "BLUETOOTH_ADAPTER": "0"},
        "volumes": ["{data}:/data", "/run/dbus:/run/dbus:ro"],
    },
)
