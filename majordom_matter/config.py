"""matterjs-server: the Matter controller service this integration drives over its WebSocket API (``/ws``), lending it
the hub's Bluetooth over its BLE proxy endpoint (``/ble``)."""

from majordom_integration_sdk import Endpoint, Need, Service

MATTERJS_SERVER = Service(
    "matterjs-server",
    # Thread is optional: without a border router Matter still works over Wi-Fi and Ethernet. Bluetooth too: an adapter
    # of its own only when one is reserved for it. The router is an in-process object a container can't bind: it
    # reaches matterjs-server through the controller (ble_delegate, over /ble) instead.
    needs=[Need("ip"), Need("thread", ["otbr-rest"], is_required=False), Need("ble", ["bluez"], is_required=False)],
    # the base URL: /ws, /ble; "adapter" tells the controller whether an adapter is bound here (empty: none)
    provides=[
        Endpoint("matter", "matter-server", "ws://127.0.0.1:{ports.api}", params={"adapter": "{peripherals.ble.url}"})
    ],
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
            # Bluetooth comes from the hub (see ble_delegate) unless an adapter was reserved for it. Then noble drives
            # it through the host's BlueZ over the system bus: its default HCI socket backend needs root
            "BLE_PROXY": "{peripherals.ble.url:!true}",
            "BLUETOOTH_ADAPTER": "{peripherals.ble.params[index]}",
            "NOBLE_BINDINGS": "{peripherals.ble.url:?dbus}",
            # matter.js logs every command's fields at info, the Thread network key included
            "LOG_LEVEL": "warning",
        },
        "volumes": [
            "{data}:/data",
            "{peripherals.ble.url:?/run/dbus:/run/dbus:ro}",  # the system bus only with an adapter: gone without one
        ],
    },
)
