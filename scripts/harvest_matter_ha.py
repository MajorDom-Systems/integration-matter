#!/usr/bin/env python3
"""Harvest Home Assistant's Matter entity judgment into ``majordom_matter/matter_spec_ha.py``.

DEV / BUILD TOOL. Unlike zha, HA's Matter discovery is **not** a standalone library — it lives in
`homeassistant.components.matter`. So instead of importing it (which would drag in all of HA core),
this downloads only the handful of platform files and parses them with **AST** (no execution, no
`homeassistant` install). Attribute references (``clusters.X.Attributes.Y``) are resolved to
``(cluster_id, attribute_id)`` via the ``chip`` bindings we already depend on.

What it takes: the genuinely-additive judgment (``entity_category`` -> visibility, platform ->
role, unit/device_class -> unit). The Matter Data Model already gives us names/types/bounds via
``chip``, so those are dropped. Value-transform lambdas (``device_to_ha``) can't be read statically
and are logged as skipped.

Usage:
    python scripts/harvest_matter_ha.py                    # writes the module (ref=dev)
    python scripts/harvest_matter_ha.py --ref 2025.1.0     # pin a HA release tag
    python scripts/harvest_matter_ha.py --stdout
"""

from __future__ import annotations

import argparse
import ast
import sys
import urllib.request

import chip.clusters as clusters

_PLATFORM_FILES = (
    "sensor", "binary_sensor", "switch", "number", "select",
    "climate", "cover", "fan", "light", "lock", "button", "event",
)
_RAW = "https://raw.githubusercontent.com/home-assistant/core/{ref}/homeassistant/components/matter/{f}.py"

# HA unit-enum leaf / device_class leaf -> our ParameterUnit value. Unmapped -> "plain".
_UNIT_BY_LEAF = {
    "CELSIUS": "celsius", "PERCENTAGE": "percentage", "KELVIN": "kelvin", "WATT": "watt",
    "VOLT": "volt", "AMPERE": "ampere", "KILO_WATT_HOUR": "kwh", "HERTZ": "hertz", "LUX": "lux",
    "PASCAL": "pascal", "KILO_PASCAL": "pascal", "HECTOPASCAL": "pascal",
    "PARTS_PER_MILLION": "ppm", "MICROGRAMS_PER_CUBIC_METER": "ugm3",
}
_UNIT_BY_DEVICE_CLASS = {
    "TEMPERATURE": "celsius", "HUMIDITY": "percentage", "BATTERY": "percentage",
    "ILLUMINANCE": "lux", "POWER": "watt", "VOLTAGE": "volt", "CURRENT": "ampere",
    "ENERGY": "kwh", "PRESSURE": "pascal", "FREQUENCY": "hertz",
}
_CONTROL_PLATFORMS = {"switch", "number", "select", "climate", "cover", "fan", "light", "lock", "button"}


def _leaf(node: ast.expr | None) -> str | None:
    """Leaf name of a node: ``Platform.SENSOR`` -> 'SENSOR', bare ``PERCENTAGE`` -> 'PERCENTAGE'."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node: ast.expr) -> list[str] | None:
    """Flatten ``clusters.X.Attributes.Y`` (an Attribute chain) into ['clusters','X','Attributes','Y']."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return list(reversed(parts))
    return None


def _resolve_attr(dotted: list[str]) -> tuple[int, int] | None:
    """['clusters','TemperatureMeasurement','Attributes','MeasuredValue'] -> (cluster_id, attr_id)."""
    if not dotted or dotted[0] != "clusters":
        return None
    obj: object = clusters
    for part in dotted[1:]:
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    cid, aid = getattr(obj, "cluster_id", None), getattr(obj, "attribute_id", None)
    return (cid, aid) if cid is not None and aid is not None else None


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def harvest(ref: str) -> tuple[dict[tuple[int, int], tuple[str, str, str]], list[str]]:
    out: dict[tuple[int, int], tuple[str, str, str]] = {}
    skipped: list[str] = []
    for f in _PLATFORM_FILES:
        try:
            src = urllib.request.urlopen(_RAW.format(ref=ref, f=f), timeout=30).read().decode()
        except Exception as e:  # noqa: BLE001 - a missing platform file is not fatal
            skipped.append(f"{f}.py: fetch failed ({e})")
            continue
        tree = ast.parse(src)
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id != "MatterDiscoverySchema":
                continue
            kw = _kwargs(call)
            platform = (_leaf(kw.get("platform")) or "").lower()
            req = kw.get("required_attributes")
            if not isinstance(req, ast.Tuple) or not req.elts:
                skipped.append(f"{f}.py: schema with no required_attributes")
                continue
            key = _resolve_attr(_dotted(req.elts[0]) or [])
            if key is None:
                skipped.append(f"{f}.py: unresolved {ast.dump(req.elts[0])[:60]}")
                continue
            desc = kw.get("entity_description")
            dkw = _kwargs(desc) if isinstance(desc, ast.Call) else {}
            entity_category = _leaf(dkw.get("entity_category"))
            visibility = "user" if entity_category is None else "setting"
            role = "control" if platform in _CONTROL_PLATFORMS else "sensor"
            unit = _unit(_leaf(dkw.get("device_class")), _leaf(dkw.get("native_unit_of_measurement")))
            # First writer wins; but a user (primary) classification upgrades a prior setting.
            prev = out.get(key)
            if prev is None or (prev[0] == "setting" and visibility == "user"):
                out[key] = (visibility, role, unit)
    return out, skipped


def _unit(device_class: str | None, unit_leaf: str | None) -> str:
    if unit_leaf and unit_leaf in _UNIT_BY_LEAF:
        return _UNIT_BY_LEAF[unit_leaf]
    if device_class and device_class in _UNIT_BY_DEVICE_CLASS:
        return _UNIT_BY_DEVICE_CLASS[device_class]
    return "plain"


def render(data: dict[tuple[int, int], tuple[str, str, str]], ref: str) -> str:
    lines = [
        '"""GENERATED by scripts/harvest_matter_ha.py — DO NOT EDIT BY HAND.',
        "",
        f"Source: home-assistant/core@{ref} homeassistant/components/matter (Apache-2.0), parsed via",
        "AST (no homeassistant dependency). Attribute ids resolved via chip. Harvested entity",
        "judgment only (entity_category -> visibility, platform -> role, unit); the Matter Data Model",
        "supplies names/types/bounds. Hand overrides live in matter_spec.py and win on merge.",
        '"""',
        "",
        "# (cluster_id, attribute_id) -> (visibility, role, unit)",
        "MATTER_HA_ATTRIBUTE_UX: dict[tuple[int, int], tuple[str, str, str]] = {",
    ]
    for (cid, aid), (vis, role, unit) in sorted(data.items()):
        lines.append(f"    ({cid:#06x}, {aid:#06x}): ({vis!r}, {role!r}, {unit!r}),")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="dev", help="home-assistant/core git ref (tag/branch)")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--out", default="majordom_matter/matter_spec_ha.py")
    args = ap.parse_args()

    data, skipped = harvest(args.ref)
    text = render(data, args.ref)
    if args.stdout:
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"[harvest_matter_ha] ref={args.ref}: wrote {len(data)} entries -> {args.out}", file=sys.stderr)
    print(f"[harvest_matter_ha] {len(skipped)} schemas skipped (composite/transform/unresolved)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
