import os

# The MajorDom Matter integration talks to a python-matter-server instance over WebSocket.
# Point it at yours via MATTER_SERVER_URL; the default matches a `matter-server` service on
# the same Docker network.
matter_server_url = os.environ.get("MATTER_SERVER_URL", "ws://matter-server:5580/ws")


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# BLE-via-matter-server mode.
#
# By default the Hub discovers BLE-only commissionable devices itself, with a LOCAL bleak scan
# (the shared SDK BLE service). That needs a working host Bluetooth/D-Bus stack in the Hub's own
# process — which the Hub has on bare metal, but NOT inside a de-rooted (userns-remapped) container
# such as the CI runner, where D-Bus SASL EXTERNAL auth rejects the remapped uid.
#
# When this flag is set, the integration does NOT open a local BLE scanner. matter-server (which
# owns the Bluetooth radio) performs the BLE scan+commission itself via `commission_with_code`
# (it locates the device over BLE by the pairing code's discriminator). Because matter-server's
# WebSocket API cannot ENUMERATE commissionable BLE devices (only IP/mDNS ones via
# discover_commissionable_nodes), BLE-only devices are not auto-listed in this mode: the user
# commissions them by entering the pairing code against a single generic "commission over BLE"
# discovery. On-network (IP/mDNS) discovery is unchanged.
matter_ble_via_server = _flag("MATTER_BLE_VIA_SERVER", default=False)
