#!/usr/bin/env python3
"""CI drift check for the vendored matter-HA harvest — the Dependabot-style refresher.

Re-runs the AST harvest against home-assistant/core and diffs vs the committed
``matter_spec_ha.py`` via the SDK's diff_specs. Exit 0 (none) / 1 (ADD/REMOVE) / 2 (RECLASSIFY).

Run: python scripts/check_matter_ha_drift.py --ref dev
"""

from __future__ import annotations

import argparse
import enum
import sys
import typing

import chip.clusters.ClusterObjects as co
from majordom_integration_sdk.spec_drift import diff_specs

from majordom_matter.matter_spec_ha import MATTER_HA_ATTRIBUTE_UX as COMMITTED
from scripts.harvest_matter_ha import harvest


def _mapped_data_type(attr: object) -> str:
    """MajorDom ``ParameterDataType`` for a chip attribute, from its declared TLV type. The unit in
    the harvested tuple is HA's semantic judgment; this is the orthogonal wire-type axis the Matter
    Data Model supplies. ``"unknown"`` when chip resolves the id but not a classifiable ``.Type``
    (odd union, struct/list, non-class) — e.g. HA referencing an attribute ahead of these bindings."""
    descriptor = getattr(attr, "attribute_type", None)
    tp = getattr(descriptor, "Type", None)
    if tp is None:
        return "unknown"
    union_args = typing.get_args(tp)  # nullable attrs are Union[Nullable, X] -> classify X
    if union_args:
        members = [a for a in union_args if getattr(a, "__name__", "") != "Nullable"]
        tp = members[0] if members else union_args[0]
    if not isinstance(tp, type):
        return "unknown"
    if issubclass(tp, (enum.Enum, enum.Flag)):  # chip enums/bitmaps are IntEnum/IntFlag
        return "enum"
    if issubclass(tp, bool):  # before int — bool is an int subclass
        return "bool"
    if issubclass(tp, int):
        return "integer"
    if issubclass(tp, float):
        return "decimal"
    if issubclass(tp, str):
        return "string"
    if issubclass(tp, bytes | bytearray):
        return "data"
    return "unknown"


def _key_label(key: tuple[int, int]) -> str:
    """Resolve a ``(cluster_id, attribute_id)`` key to ``Cluster.Attribute [data_type]`` via the chip
    bindings, so the drift PR names what changed (``Chime.SelectedChime [integer]``) instead of only
    ``(1366, 1)``. Falls back to the hex id / ``unknown`` for anything chip can't classify."""
    cid, aid = key
    cluster = co.ALL_CLUSTERS.get(cid)
    cluster_name = getattr(cluster, "__name__", None) or f"0x{cid:04x}"
    attr = co.ALL_ATTRIBUTES.get(cid, {}).get(aid)
    qualname = getattr(attr, "__qualname__", "")  # e.g. "Chime.Attributes.SelectedChime"
    attr_name = qualname.split(".")[-1] if qualname else f"0x{aid:04x}"
    return f"{cluster_name}.{attr_name} [{_mapped_data_type(attr)}]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="dev")
    args = ap.parse_args()
    current, _skipped = harvest(args.ref)
    report = diff_specs(current, COMMITTED)
    print(report.render(source="matter-ha", key_label=_key_label), file=sys.stderr)
    if report.is_empty:
        return 0
    return 2 if report.has_high_risk else 1


if __name__ == "__main__":
    raise SystemExit(main())
