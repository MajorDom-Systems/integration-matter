# integration-matter

A [MajorDom](https://majordom.io) integration — bridges **Matter** (CSA Connectivity Standard)
devices into the MajorDom language.

Built for the **MajorDom Hub**, but it doesn't need it: this is a standalone, standardized
library for Matter that you can use on its own (see **Run it standalone** below). Built on the
[MajorDom Integration SDK](https://github.com/MajorDom-Systems/integration-sdk). The entry point
is `MatterController` (`majordom_matter/controller.py`), which the Hub — or the SDK's dev runner —
instantiates and drives through its lifecycle: discovery → pairing → commands → teardown.

- **Other protocols:** browse the [MajorDom integrations](https://github.com/orgs/MajorDom-Systems/repositories?q=integration-).
- **Create your own:** start from the [integration template](https://github.com/MajorDom-Systems/integration-template).

## Documentation

Full integration-author docs — the controller lifecycle, data models, storing data, discovery,
and a worked example — live at **[docs.majordom.io](https://docs.majordom.io/device-integration)**.

## Development

```sh
poetry install && poetry run poe install
```

| Task | Description |
|------|-------------|
| `poe check` | Full quality pipeline (ruff, ty, pytest, poetry build/check) |
| `poe check --ci` | Same, plus `git diff --exit-code` |

Work lands on `develop`; `master` is protected and released via **Actions → Release**. The default
suite drives the controller against a canned node with the SDK's test doubles — no matter-server,
no docker (`tests/test_controller_stub.py`). The exhaustive real-device coverage commissions
Google's Matter Virtual Devices in a dockerized matter-server (`tests/test_controller.py`, see
`docker-compose.matter-tests.yml`); the MVD binaries + PAA certs are fetched, not committed.

## Run it standalone (without the Hub)

`majordom-matter` is a standalone library — import it into your own app, or run **just this
integration** interactively (discover, pair, control, and inspect devices from a prompt) with no Hub.
It needs a running [python-matter-server](https://github.com/home-assistant-libs/python-matter-server)
reachable over its WebSocket (`MATTER_SERVER_URL`, default `ws://localhost:5580/ws`); Thread devices
also need an OpenThread Border Router.

See **[Standalone mode](https://docs.majordom.io/device-integration/standalone)** for the interactive
CLI, watch mode, and the programmatic API.

## About this integration

- **Protocol / platform:** Matter (Connectivity Standards Alliance) via `python-matter-server` + `chip`.
- **Transport(s):** IP over Thread / Wi-Fi / Ethernet; BLE for commissioning.
- **Supported devices:** any Matter-certified device — lights, plugs, switches, sensors, locks,
  thermostats, covers, fans, appliances (verified against 30 Matter Virtual Device types).
- **Credentials needed to pair:** `code` (manual pairing code) or `qr`.

### Required harness

- **Hardware adapters:** an 802.15.4 radio (e.g. a SkyConnect / Thread dongle) for Thread devices —
  driven by matter-server / the OTBR, not this package directly.
- **Third-party software services:** a **matter-server** instance reachable over WebSocket
  (`MATTER_SERVER_URL`), and an **OpenThread Border Router (OTBR)** for Thread devices.
- **OS / permissions:** BLE access for commissioning; mDNS on the LAN for on-network discovery.

### Protocol stack (OSI)

| OSI layer | Protocol | Implemented by |
|-----------|----------|----------------|
| Application (7) | Matter clusters / data model | **this integration** (via `chip` lib) |
| Session (5) | CASE / PASE secure session | library (matter-server) |
| Transport (4) | UDP | OS |
| Network (3) | IPv6 · 6LoWPAN | OS · OTBR (harness) |
| Data link / Physical (1–2) | Thread · IEEE 802.15.4 (or Wi-Fi / Ethernet) | radio adapter (harness) |

### Progress

- [x] Discovery services registered (mDNS on-network via matter-server; BLE for commissionable devices); cancel closures called in `stop`
- [x] Discovery listeners fire and call `controller_did_receive_discovery`
- [x] Re-discovery of already-paired devices on reconnect (`controller_did_connect_device`)
- [x] Device pairing (BLE→Thread and on-network commissioning)
- [x] Device schema mapped: device info, parameter list, per-parameter metadata → MajorDom's domain model
- [x] Hub → Device control (`send_command`)
- [x] Device → Hub event subscription (`controller_did_receive_events`)
- [x] `identify`
- [x] `unpair`
- [x] `fetch`
- [x] Availability tracking while running (`controller_did_lose_device` / `last_error`)
- [x] Graceful shutdown in `stop`
- [x] Tests pass against virtual/simulated devices (stub + dockerized MVD suite)

### Notes

The MVD `chef` binaries are x86-64 Linux only, so the real-device suite runs in an amd64 container
(Rosetta on Apple Silicon). A monthly canary fetches the *latest* upstream MVD release and fails if
it ships something unsupported — the signal to add support.

## License

See [LICENSE](LICENSE). For commercial licensing or partnership inquiries regarding MajorDom,
contact us via [parker-industries.org/partnership](https://parker-industries.org/partnership).
