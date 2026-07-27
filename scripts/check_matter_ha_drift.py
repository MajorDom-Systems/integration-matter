#!/usr/bin/env python3
"""CI drift check for the vendored matter-HA harvest — the Dependabot-style refresher.

Re-runs the AST harvest against home-assistant/core and diffs vs the committed
``matter_spec_ha.py`` via the SDK's diff_specs. Exit 0 (none) / 1 (ADD/REMOVE) / 2 (RECLASSIFY).

Run: python scripts/check_matter_ha_drift.py --ref dev
"""

from __future__ import annotations

import argparse
import sys

import chip.clusters.ClusterObjects as co
from majordom_integration_sdk.spec_drift import diff_specs

from majordom_matter.matter_spec_ha import MATTER_HA_ATTRIBUTE_UX as COMMITTED
from scripts.harvest_matter_ha import harvest


def _key_label(key: tuple[int, int]) -> str:
    """Resolve a ``(cluster_id, attribute_id)`` key to ``Cluster.Attribute`` via the chip bindings,
    so the drift PR names what changed (``Chime.SelectedChime``) instead of only ``(1366, 1)``.
    Falls back to the hex id for anything chip doesn't know."""
    cid, aid = key
    cluster = co.ALL_CLUSTERS.get(cid)
    cluster_name = getattr(cluster, "__name__", None) or f"0x{cid:04x}"
    attr = co.ALL_ATTRIBUTES.get(cid, {}).get(aid)
    qualname = getattr(attr, "__qualname__", "")  # e.g. "Chime.Attributes.SelectedChime"
    attr_name = qualname.split(".")[-1] if qualname else f"0x{aid:04x}"
    return f"{cluster_name}.{attr_name}"


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
