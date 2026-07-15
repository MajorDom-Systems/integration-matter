# Matter integration

The hub talks to a **Matter controller server** over its WebSocket API (`ws://<host>:5580/ws`)
via `matter_server.client.MatterClient`. The server owns the Matter fabric, the Thread/BLE
radios, and commissioning; the hub discovers, commissions, and controls devices through it.

Two test tiers:

- **Virtual** — `tests/test_controllers/test_matter/test_matter_controller.py`, driven by
  `docker-compose.matter-tests.yml`. Prebuilt **x86-64** Matter Virtual Device (MVD) binaries under
  `bins/` act as fake devices, commissioned over the network (no BLE). Runs in CI on x86-64 runners.
- **Hardware** — `tests/test_controllers/test_matter/test_matter_controller_real.py`, on the
  self-hosted `lab-pi5` runner: a real Thread bulb commissioned over **BLE → Thread** through a real
  OpenThread Border Router (OTBR + SkyConnect RCP), verified physically via the IoT-cage photoresistor.
  Manual only (`workflow_dispatch`). The target device is selectable in one line — `_TARGET` in the
  test, or the `MATTER_HW_TARGET=ikea|nanoleaf` env var (default `ikea`); both bulbs pass.

---

## Issues we hit (and their true causes)

- **Stale OTBR image was the whole ballgame.** A months-old cached `openthread/border-router:latest`
  had a broken RCP serial init: otbr-agent failed/hung at `spinel_driver.cpp:87`, `wpan0` never came
  up, and it crash-looped — producing red-herring `cp210x … -110` USB timeouts and uninterruptible
  `D`-state `open()` hangs. With no Thread network, **every** device's commissioning failed at
  `NetworkCommissioning.scanNetworks` ("Failure"). The RCP firmware/hardware were fine the whole time
  (the flasher + raw pyserial talked SPINEL reliably).
- **`uart-init-deassert` breaks this build.** Adding it to `OT_RCP_DEVICE` makes otbr-agent reject the
  URL (`otSysInit … InvalidArgument`). It's a valid param in other OT builds — not this one.
- **OTBR state isn't persisted by default** → the border router factory-resets on every restart. And
  mounting a volume over `/var/lib/thread` fails (`ln: cannot overwrite directory`) — the image
  symlinks `/var/lib/thread → /data`, so persist at **`/data`**.
- **Docker `userns-remap`**: privileged containers are rejected without `--userns=host`, and the
  OTBR image's s6-overlay refuses to start unless `/run` is a fresh root-owned tmpfs.
- **The x86-64 MVD binaries can't run on the Pi** (16 KB-page kernel; qemu-user can't map the
  4 KB-aligned segments) → the virtual suite is x86-64 CI only.
- **BLE discovery misses static-address devices.** Matter controllers discover by BlueZ's
  `InterfacesAdded` signal; a device with a *static* BLE address (IKEA) already in BlueZ's cache never
  re-fires it → "no commissionable device discovered." Nanoleaf uses a resolvable address, so it's fine.
- **The hub does not push the Thread dataset.** `MatterController.pair_device` just calls
  `commission_with_code`; the server must *already* hold the operational dataset.
- **The Arduino cage resets all relays OFF on every serial open** (DTR pulse) — bulbs lose power
  unless one fd holds the port open for the whole run.
- **The "Nanoleaf RX-wedge" was a broken-OTBR artifact, not firmware.** We'd blamed Nanoleaf fw 4.1.3
  for wedging its Thread RX after one `Invoke`. Once the border router was healthy (fresh image, above),
  it never reproduced: 25 back-to-back Toggle+read+ping cycles with an active subscription, plus
  Level/Color commands and a 90 s idle soak, all clean — `ot-ctl ping` never dropped a packet. The old
  symptom was a degraded Thread mesh from the spinel-failing RCP. **Both the IKEA and the Nanoleaf are
  CI-usable.** (The Nanoleaf's shared A0-photoresistor light and its slot-3 power are wired in the test.)

## What it takes to make it work

1. **Fresh OTBR image**: `docker pull openthread/border-router:latest`, then
   `OT_RCP_DEVICE=spinel+hdlc+uart:///dev/ttyUSB1?uart-baudrate=460800&uart-flow-control`
   (no `uart-init-deassert`), `--network host --privileged --cap-add NET_ADMIN --userns=host`,
   tmpfs `/run`+`/tmp`, `--device <SkyConnect by-id>:/dev/ttyUSB1 --device /dev/net/tun`,
   and **`-v <host-dir>:/data`** for persistence.
2. **Form a network once**: `ot-ctl dataset init new; dataset commit active; ifconfig up; thread start`
   → `state: leader`.
3. **Give the Matter server the dataset** before commissioning: read it from OTBR
   (`ot-ctl dataset active -x`, or the OTBR REST `GET /node/dataset/active`) and `set_thread_dataset`.
4. **Flush the BlueZ cache immediately before each commission** and do not scan afterward:
   `for m in $(bluetoothctl devices | awk '{print $2}'); do bluetoothctl remove "$m"; done; systemctl restart bluetooth`.
5. **Power the device on** (IKEA opens a ~5-min pairing window on any power-on; a decommissioned
   Nanoleaf re-advertises on power-on), driving the cage from a single held serial fd.
6. **Pick the target**: IKEA (`2455-383-5850`, cage slot 2) is the default CI device; switch to the
   Nanoleaf (`1321-631-8363`, cage slot 3) with `MATTER_HW_TARGET=nanoleaf`. Both pass the full
   discover → pair → OnOff-with-photoresistor → unpair flow.

See `otbr-skyconnect-fix` in the assistant memory for the exact commands and recovery steps.
