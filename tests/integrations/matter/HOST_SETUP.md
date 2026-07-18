# lab-pi5 Matter/Thread hardware host setup

How the self-hosted runner is wired so the real-hardware Matter test
(`test_matter_controller_hardware.py`) can pass. Recreate this on the host if the rig is rebuilt.

## Host

- Raspberry Pi 5, **aarch64**, Debian 13 (trixie), kernel `6.12.75+rpt-rpi-2712` (**16 KB pages** →
  can't run the x86-64 MVD binaries; virtual suite stays on x86 CI).
- Docker daemon runs with **`userns-remap` (`dockremap`)** → every privileged/host-network
  container below needs **`--userns=host`**.
- BlueZ (host `bluetoothd`), adapter `hci0` = `88:A2:9E:A7:D6:6F`, powered.

## USB devices — always address by the stable by-id path (ttyUSBn reshuffles across reboots)

| Stick | by-id | Role |
|---|---|---|
| CH340 (1a86) | `usb-1a86_USB_Serial-if00-port0` | IoT-cage Arduino |
| SiLabs CP2102N | `usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_d44ac5b9369bed11b3a604bfa7669f5d-if00-port0` | SkyConnect Thread RCP |
| SONOFF ZWave | `usb-SONOFF_SONOFF_ZWave_Dongle-PZG23_...` | Z-Wave (unused by hub) |

## Two host services (both containers, host networking)

### 1. OTBR — Thread border router (port 8081 REST)

Use a **freshly pulled** image — a stale cached one has a broken RCP serial init (see the
integration readme). State persists at **`/data`** (not `/var/lib/thread`, which the image symlinks).

```sh
docker pull openthread/border-router:latest
SKY=/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_d44ac5b9369bed11b3a604bfa7669f5d-if00-port0
docker run -d --name otbr --restart unless-stopped \
  --userns=host --privileged --network host --cap-add NET_ADMIN \
  --tmpfs /run:exec,mode=1777 --tmpfs /tmp:exec,mode=1777 \
  -v /home/admin/otbr-data:/data \
  --device "$SKY":/dev/ttyUSB1 --device /dev/net/tun:/dev/net/tun \
  -e OT_INFRA_IF=eth0 -e OT_THREAD_IF=wpan0 -e OT_LOG_LEVEL=7 \
  -e OT_REST_LISTEN_ADDR=0.0.0.0 -e OT_REST_LISTEN_PORT=8081 \
  -e OT_RCP_DEVICE="spinel+hdlc+uart:///dev/ttyUSB1?uart-baudrate=460800&uart-flow-control" \
  openthread/border-router:latest
```

Form a Thread network once (persists in `/home/admin/otbr-data`):

```sh
docker exec otbr ot-ctl dataset init new
docker exec otbr ot-ctl dataset commit active
docker exec otbr ot-ctl ifconfig up
docker exec otbr ot-ctl thread start
docker exec otbr ot-ctl state          # -> leader
docker exec otbr ot-ctl dataset active -x   # operational dataset (hex TLV)
```

Recovery if the RCP wedges (`cp210x … -110`, `wpan0` missing): bus-reset the SkyConnect
(sysfs node `3-2` on this host) — `echo 0 | sudo tee /sys/bus/usb/devices/3-2/authorized; echo 1 | sudo tee /sys/bus/usb/devices/3-2/authorized` — then restart `otbr`.

### 2. matterjs-server — Matter controller (port 5580 WS, `matter_server_url`)

matter.js/noble over the host D-Bus BlueZ. State (fabric + the Thread dataset we push) persists in
`/home/admin/matterjs_data`.

```sh
docker run -d --name matterjs-server --restart unless-stopped \
  --userns=host --network host \
  -e NOBLE_BINDINGS=dbus -e BLUETOOTH_ADAPTER=0 -e STORAGE_PATH=/data \
  -v /home/admin/matterjs_data:/data -v /run/dbus:/run/dbus:ro \
  ghcr.io/matter-js/matterjs-server:dev
```

The hub does **not** push the Thread dataset — do it once after forming the network (the test's
`thread_provisioned` fixture automates this): read `ot-ctl dataset active -x` (or OTBR REST
`GET :8081/node/dataset/active`) and send it via the WS `set_thread_dataset` command.

## Before each commission — flush BlueZ (static-address discovery fix)

```sh
for m in $(bluetoothctl devices | awk '{print $2}'); do bluetoothctl remove "$m"; done
sudo systemctl restart bluetooth
```
Do not run any BLE scan between the flush and the commission.

## IoT cage

Arduino on the CH340 (115200), frame protocol `[<idx><cmd><val><chk>]` (see
`tests/hardware/iot_cage/aioiotrpc.py`). **Slot 2 = IKEA KAJPLATS** (DUT, code `2455-383-5850`) and
carries the A0 photoresistor; **slot 3 = Nanoleaf** (shares that one sensor). The Arduino resets all
relays OFF on every serial open (DTR) — drive it from a single held fd, and power everything else off
to isolate the DUT's light on the shared sensor.
