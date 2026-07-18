#!/usr/bin/env bash
# Fetch Google's Matter Virtual Device (MVD) `chef` binaries into tests/mvd/ (gitignored).
#
# Each binary is connectedhomeip's `chef` example app compiled per device type, shipped inside
# Google's MVD package (https://developers.home.google.com/tools/virtual-device) as a GPG-signed
# Debian archive. A .deb is a plain `ar` archive, so we extract without installing (portable —
# works on macOS/CI without dpkg). Only an amd64 Linux build exists upstream (no arm64 Linux),
# which is why the matter suite runs in an x86-64 container (Rosetta locally, native on CI).
set -euo pipefail

# The lowest version this integration is known to support. Probing for "latest" starts here, and
# it is the default when no version is requested (reproducible PR/push runs pin to a known build).
MVD_FLOOR="${MVD_FLOOR:-1.7.0}"
MVD_VERSION="${MVD_VERSION:-$MVD_FLOOR}"
BASE_URL="${MVD_BASE_URL:-https://dl.google.com/mvd}"
GOOGLE_KEY_URL="${GOOGLE_KEY_URL:-https://dl.google.com/linux/linux_signing_key.pub}"
# Google Inc. (Linux Packages Signing Authority) — the key the MVD .deb is signed with.
EXPECTED_KEY_FPR="${MVD_KEY_FPR:-EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/tests/mvd"

# Resolve MVD_VERSION=latest by probing the CDN. Google publishes no directory listing, `latest`
# alias, or manifest — but every version lives at a deterministic `mvd_<semver>_amd64.deb` URL
# that returns 200 (exists) or 404. So walk major, then minor, then patch upward from the floor,
# taking the highest that still exists. Bounded so a gap never loops forever. This is what lets
# the monthly canary discover a NEW upstream release; the GPG check below still gates integrity.
_exists() { [ "$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/mvd_${1}_amd64.deb")" = "200" ]; }
if [ "$MVD_VERSION" = "latest" ]; then
  IFS=. read -r M m p <<<"$MVD_FLOOR"
  for _ in $(seq 1 5);  do _exists "$((M+1)).0.0"     && { M=$((M+1)); m=0; p=0; } || break; done
  for _ in $(seq 1 20); do _exists "${M}.$((m+1)).0"  && { m=$((m+1)); p=0; }      || break; done
  for _ in $(seq 1 50); do _exists "${M}.${m}.$((p+1))" && p=$((p+1))              || break; done
  MVD_VERSION="${M}.${m}.${p}"
  echo "Resolved latest MVD version: ${MVD_VERSION} (floor ${MVD_FLOOR})"
fi
DEB="mvd_${MVD_VERSION}_amd64.deb"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT; cd "$work"

echo "Downloading ${DEB} (+ signature)…"
curl -fsSLO "${BASE_URL}/${DEB}"
curl -fsSLO "${BASE_URL}/${DEB}.asc"

echo "Verifying GPG signature against Google's Linux package key…"
export GNUPGHOME="$work/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
curl -fsSL "$GOOGLE_KEY_URL" | gpg --import 2>/dev/null
gpg --list-keys --with-colons | grep -q "$EXPECTED_KEY_FPR" \
  || { echo "ERROR: imported key does not match the pinned fingerprint ${EXPECTED_KEY_FPR}"; exit 1; }
gpg --verify "${DEB}.asc" "${DEB}"   # hard-fails on a bad/absent signature

echo "Extracting (ar + tar — no dpkg needed)…"
ar x "${DEB}"
mkdir -p data && tar -xf data.tar.* -C data

# The per-device chef binaries live under the Electron app's prebuilt tree, named
# `rootnode_<devicetype>_<hash>`. Map them to clean, hash-free names; device types that ship
# twice (on-off-light, generic-switch) get a `-2` on the second. A device type we don't have a
# nice name for is NOT an error — the integration maps device types flexibly, so we auto-name it
# from its prefix and still ship it, letting the test sweep commission it like any other. That is
# the point of the canary: a brand-new upstream device gets exercised automatically, and only a
# genuine mapping/commissioning failure (surfaced by the tests) flags it for a human.
python3 - "$OUT" <<'PY'
import os, re, sys, shutil, pathlib
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
src = pathlib.Path("data/usr/lib/mvd/resources/app/prebuilt/linux_x64")
NAME = {
    "airpurifier": "air-purifier", "airqualitysensor": "air-quality-sensor",
    "basicvideoplayer": "basic-video-player", "colortemperaturelight": "color-temperature-light",
    "contactsensor": "contact-sensor", "dimmablelight": "dimmable-light",
    "dimmablepluginunit": "dimmable-plug", "dishwasher": "dishwasher", "doorlock": "door-lock",
    "extendedcolorlight": "extended-color-light", "fan": "fan", "flowsensor": "flow-sensor",
    "genericswitch": "generic-switch", "humiditysensor": "humidity-sensor",
    "laundrywasher": "laundry-washer", "lightsensor": "light-sensor",
    "occupancysensor": "occupancy-sensor", "onofflight": "on-off-light",
    "onoffpluginunit": "on-off-plug", "pressuresensor": "pressure-sensor", "pump": "pump",
    "refrigerator_temperaturecontrolledcabinet_temperaturecontrolledcabinet": "refrigerator",
    "roboticvacuumcleaner": "robotic-vacuum", "roomairconditioner": "room-air-conditioner",
    "smokecoalarm": "smoke-co-alarm", "temperaturesensor": "temperature-sensor",
    "thermostat": "thermostat", "windowcovering": "window-covering",
}
# group by device-type prefix (strip the trailing _<hash>), sorted for deterministic -2 order
groups: dict[str, list[str]] = {}
for f in sorted(p.name for p in src.glob("rootnode_*")):
    prefix = re.sub(r"^rootnode_", "", f)
    prefix = re.sub(r"_[^_]+$", "", prefix)  # drop the hash segment
    groups.setdefault(prefix, []).append(f)
unknown = sorted(set(groups) - set(NAME))
if unknown:
    # Not fatal — auto-name and ship them so the flexible mapping is still exercised. Consider
    # adding a nice name to NAME above once the type is known-good.
    print(f"Note: auto-naming {len(unknown)} unmapped device type(s): {unknown}")
n = 0
for prefix, files in groups.items():
    base = NAME.get(prefix, prefix.replace("_", "-"))
    for i, f in enumerate(files):
        target = out / (base if i == 0 else f"{base}-{i+1}")
        shutil.copy(src / f, target)
        os.chmod(target, 0o755)
        n += 1
print(f"Extracted {n} MVD binaries -> {out}")
PY

count="$(find "$OUT" -type f | wc -l | tr -d ' ')"
echo "Done: ${count} MVD binaries (MVD ${MVD_VERSION})"
