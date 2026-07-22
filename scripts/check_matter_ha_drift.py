#!/usr/bin/env python3
"""CI drift check for the vendored matter-HA harvest — the Dependabot-style refresher.

Re-runs the AST harvest against home-assistant/core and diffs vs the committed
``matter_spec_ha.py`` via the SDK's diff_specs. Exit 0 (none) / 1 (ADD/REMOVE) / 2 (RECLASSIFY).

Run: python scripts/check_matter_ha_drift.py --ref dev
"""

from __future__ import annotations

import argparse
import sys

from majordom_integration_sdk.spec_drift import diff_specs

from majordom_matter.matter_spec_ha import MATTER_HA_ATTRIBUTE_UX as COMMITTED
from scripts.harvest_matter_ha import harvest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="dev")
    args = ap.parse_args()
    current, _skipped = harvest(args.ref)
    report = diff_specs(current, COMMITTED)
    print(report.render(source="matter-ha"), file=sys.stderr)
    if report.is_empty:
        return 0
    return 2 if report.has_high_risk else 1


if __name__ == "__main__":
    raise SystemExit(main())
