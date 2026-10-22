"""matterjs-server: the Matter controller service this integration drives over its WebSocket API."""

from majordom_integration_sdk import Endpoint, Need, Service

MATTERJS_SERVER = Service(
    "matterjs-server",
    # Thread is optional: without a border router Matter still works over Wi-Fi and Ethernet.
    needs=[Need("ip"), Need("thread", ["otbr-rest"], required=False)],
    provides=[Endpoint("matter", "ws", "ws://127.0.0.1:{ports.api}/ws")],
    ports={"api": 5580},
    compose={
        # :dev pinned by digest (2026-10-08); bump on purpose
        "image": "ghcr.io/matter-js/matterjs-server@sha256:bcaacf6b2b2df9ce2b929ad7469d5380a4d627f05f790e201d127e8b65af5b0e",  # noqa: E501
        "network_mode": "host",
        "userns_mode": "host",
        "user": "{host.uid}:{host.gid}",  # its data folder belongs to the hub
        "environment": {
            "STORAGE_PATH": "/data",
            "PORT": "{ports.api}",
            "BLE_PROXY": "true",
            # matter.js logs every command's fields at info, the Thread network key included
            "LOG_LEVEL": "warning",
        },  # Bluetooth comes from the hub, see ble_delegate
        "volumes": ["{data}:/data"],
    },
)
