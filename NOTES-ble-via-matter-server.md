# DRAFT: Matter BLE commissioning via matter-server (no local Hub BLE)

Branch `matter-ble-via-matter-server`. Goal: let Matter BLE commissioning work when the **Hub
process has no usable Bluetooth/D-Bus** — specifically the CI hardware runner, a de-rooted
(userns-remapped) container where D-Bus SASL EXTERNAL auth rejects the remapped uid, so the SDK's
local `bleak` scan can't reach `bluetoothd`.

## What the Hub does today
- **On-network** commissionable devices → discovered from matter-server (`discover_commissionable_nodes`, mDNS).
- **BLE-only** commissionable devices → discovered by the Hub itself with a **local bleak scan**
  (SDK `BLEDiscoveryService`), which parses the Matter commissionable advert (0xFFF6) into a
  `Discovery`. The actual commission then already goes through **matter-server** (`commission_with_code`,
  which does its own BLE scan by the code's discriminator).

So local BLE is used **only to pre-list** the device. Commissioning is already matter-server's job.

## The constraint
matter-server's WebSocket API can **commission** a specific device over BLE (`commission_with_code`)
but cannot **enumerate** commissionable BLE devices — `discover_commissionable_nodes` is mDNS/IP only.
So we cannot simply move the BLE *listing* to matter-server; there is no such call.

## This branch's approach (`matter_ble_via_server`, env `MATTER_BLE_VIA_SERVER`)
When set:
1. `start()` does **not** register the local BLE scanner.
2. Instead it surfaces **one generic discovery**: "Matter device (enter pairing code)" (transport BLE,
   accepts a manual code / QR).
3. Pairing it feeds the code to `commission_with_code` (unchanged) → matter-server scans BLE by
   discriminator and commissions. No local BLE needed anywhere in the Hub.

On-network (mDNS) discovery is unchanged. Default (flag off) keeps today's local-scan behavior.

## Still to finish (this is a draft)
- **`pair_device` provisional state:** it does `device_repository.state(discovery.id)` + `assert device`.
  Confirm the Hub seeds provisional device-state for the synthetic discovery id when the user starts
  pairing; if not, seed it in `_emit_ble_via_server_discovery` (or relax the assert for this path).
- **Re-emit after pairing:** `pair_device` pops the discovery on success; re-emit the placeholder so
  additional BLE devices can be added in one session.
- **Hardware test:** `tests/integrations/matter/test_hardware.py` expects a device-specific BLE
  discovery to appear, then pairs it. Under this mode it must instead pair the generic placeholder
  with the bulb's known pairing code (`MATTER_BULB_PAIRING_CODE` / the target's code). Add a
  mode-aware path (or a separate test) so CI can exercise Matter without a local BLE adapter.
- **UX:** a permanent "enter code" entry is a placeholder; a nicer flow would prompt for the code as
  a dedicated "add Matter device by code" action rather than a synthetic discovery.

## Why this is the durable fix vs. BLE-into-the-container
Passing BLE into the runner needs `userns=host` (real root) so D-Bus auth works — it re-privileges
the runner. This branch keeps the runner de-rooted: only matter-server (which already runs
`userns=host` and owns the radio) touches Bluetooth.
