"""matterjs-server: the Matter controller service this integration drives over its WebSocket API (``/ws``), lending it
the hub's Bluetooth over its BLE proxy endpoint (``/ble``)."""

from typing import Literal

from majordom_integration_sdk import Endpoint, Need, Service
from pydantic import BaseModel, Field


class MatterSettings(BaseModel):
    ble_mode: Literal["router", "direct"] = Field(
        "router",
        title="Bluetooth",
        description="Router lends matterjs-server every radio the hub hears with, and lists the devices waiting to be "
        "added. Direct leaves Bluetooth to matterjs-server on the adapter reserved for it: devices are then added by "
        "their pairing code.",
    )


MATTERJS_SERVER = Service(
    "matterjs-server",
    # Thread is optional: without a border router Matter still works over Wi-Fi and Ethernet. Bluetooth too: it has an
    # adapter of its own only when one is reserved for it (ble_mode "direct"), the router has every other.
    needs=[Need("ip"), Need("thread", ["otbr-rest"], required=False), Need("ble", ["bluez"], required=False)],
    provides=[Endpoint("matter", "matter-server", "ws://127.0.0.1:{ports.api}")],  # the base URL: /ws, /ble
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
            # Bluetooth comes from the hub (see ble_delegate) unless an adapter was reserved for it. BLUETOOTH_ADAPTER
            # follows matterjs-server's --bluetooth-adapter, which its README documents as a flag only (unverified)
            "BLE_PROXY": "{peripherals.ble.url:!true}",
            "BLUETOOTH_ADAPTER": "{peripherals.ble.params[index]}",
            # matter.js logs every command's fields at info, the Thread network key included
            "LOG_LEVEL": "warning",
        },
        "volumes": ["{data}:/data"],
    },
)
