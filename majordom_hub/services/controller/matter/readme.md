# Matter integration

The hub talks to a **Matter controller server** over its WebSocket API (`ws://<host>:5580/ws`)
via `matter_server.client.MatterClient`. The server owns the Matter fabric, the Thread/BLE
radios, and commissioning; the hub commissions/controls devices through it.

Two test tiers:

- **Virtual** — `tests/test_controllers/test_matter/`, driven by `docker-compose.matter-tests.yml`.
  Prebuilt **x86-64** Matter Virtual Device (MVD) binaries under `bins/` act as fake devices,
  commissioned over the network (no BLE). Runs in CI on **x86-64** GitHub runners.
- **Hardware** — `tests/test_controllers/test_matter/test_matter_controller_real.py`, driven by
  `docker-compose.matter-hardware.yml` on the self-hosted `lab-pi5` runner: a real Thread device
  commissioned over **BLE → Thread** through a real OpenThread Border Router (OTBR). Manual only
  (`workflow_dispatch`).

---

## Gotchas & their true causes (needed to replicate the setup)

### Docker
- **matter-server + OTBR need host networking + host D-Bus.** Matter/Thread commissioning relies on
  mDNS multicast, IPv6 RA/forwarding, and (for BLE) BlueZ over D-Bus — all broken by Docker's bridge
  network. Run with `--network host` and mount `-v /run/dbus:/run/dbus:ro`. Once you're on host
  networking + privileged, containerization buys little isolation for these services.
- **`userns-remap` daemons break privileged containers.** If the Docker daemon runs with
  `userns-remap`, `privileged: true` is rejected outright — add `userns_mode: "host"` to those
  services. Additionally, the **openthread/border-router** image uses s6-overlay, which refuses to
  start when `/run` is owned by a uid-shifted user (it appears as uid 100000); mount a fresh
  root-owned tmpfs over it: `--tmpfs /run:exec,mode=0755,uid=0,gid=0` (and `/tmp`).

### Architecture / CPU
- **The MVD binaries are x86-64; don't run the virtual suite on an ARM host.** Emulating them with
  qemu-user only works on a **4 KB-page** host. The Raspberry Pi 5's default kernel uses **16 KB
  pages**, on which qemu-user cannot map the 4 KB-aligned x86-64 segments (`failed to map segment`).
  → Run the **virtual** suite on x86-64 CI; use the Pi only for **hardware** tests.

### BLE commissioning (the important one)
- **Choose a controller server whose BLE stack matches your host BlueZ.** The CHIP-based
  `home-assistant-libs/python-matter-server` failed BLE discovery on recent BlueZ; the matter.js
  `ghcr.io/matter-js/matterjs-server` (noble, `-e NOBLE_BINDINGS=dbus -e BLUETOOTH_ADAPTER=0`) is the
  drop-in successor and works.
- **Flush the BlueZ device cache before each commission — and don't scan in between.** This is the
  real cause of "No commissionable device was discovered" on an otherwise-visible device: Matter
  controllers discover by listening for BlueZ's `InterfacesAdded` ("*new* device") D-Bus signal.
  A device advertising a **static** BLE address (e.g. IKEA) that is already in BlueZ's cache — from
  any prior scan — never re-fires that signal, so the controller never sees it. (Devices using a
  changing *resolvable* address always look new and don't hit this; and `bleak`/`dbus-fast` masks it
  because it *polls* managed objects instead of relying on the signal — so "bleak sees it, matter
  doesn't" is expected and misleading.) Fix:

  ```sh
  for m in $(bluetoothctl devices | awk '{print $2}'); do bluetoothctl remove "$m"; done
  systemctl restart bluetooth
  ```

  Run this immediately before commissioning, and do not run any BLE scan afterward (a scan re-caches
  the device and re-consumes the signal). See `tests/test_controllers/test_matter/helper.py`
  (`flush_ble_cache`) and the `matter-hardware-tests` workflow.
