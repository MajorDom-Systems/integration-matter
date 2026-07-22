"""Run majordom's MATTER visibility mapping across the full chip data model (all clusters'
attributes), replicating mapper.parse_attributes, and bucket like the iOS app: user/setting/system."""

import inspect
from typing import cast

import chip.clusters.Objects as M
from chip.clusters.CHIPClusters import ChipClusters
from chip.clusters.ClusterObjects import ClusterAttributeDescriptor

from majordom_matter.matter_spec import (
    EVERYDAY_CONTROL_ATTRIBUTES,
    MAIN_PARAMETER_BY_CLUSTER,
    SENSITIVE_ATTRIBUTE_NAME_PREFIXES,
    SYSTEM_ATTRIBUTES,
    SYSTEM_CLUSTERS,
    USER_READINGS,
    AttributeKey,
)

CI = ChipClusters(None)


def writable(cid, aid):
    try:
        return bool(CI.GetClusterInfoById(cid).get("attributes", {}).get(aid, {}).get("writable"))
    except Exception:
        return False


clusters = [
    c
    for _, c in inspect.getmembers(M, inspect.isclass)
    if hasattr(c, "id") and hasattr(c, "Attributes") and isinstance(c.id, int)
]
clusters = sorted({c.id: c for c in clusters}.items())

tot = {"user": 0, "setting": 0, "system": 0}
rows = []
for cid, cls in clusters:
    cid = cast("int", cid)  # guaranteed by the isinstance guard above; inspect types it object
    is_sys = cid in SYSTEM_CLUSTERS
    b = {"user": [], "setting": [], "system": []}
    for name, attr in inspect.getmembers(cls.Attributes, inspect.isclass):
        if not issubclass(attr, ClusterAttributeDescriptor):
            continue
        aid = getattr(attr, "attribute_id", -1)
        if aid in SYSTEM_ATTRIBUTES:
            continue
        key = AttributeKey(cid, aid)
        if is_sys or name.startswith(SENSITIVE_ATTRIBUTE_NAME_PREFIXES):
            vis = "system"
        elif writable(cid, aid):
            vis = "user" if key in EVERYDAY_CONTROL_ATTRIBUTES else "setting"
        elif key in USER_READINGS:
            vis = "user"
        else:
            vis = "system"  # read-only, uncurated -> hidden (inverted default)
        b[vis].append(name)
    for k in tot:
        tot[k] += len(b[k])
    rows.append((cid, cls.__name__, is_sys, cid in MAIN_PARAMETER_BY_CLUSTER, b))

print(f"=== MATTER full-datamodel mapping: {len(rows)} clusters ===")
print(f"total attr-params by bucket: user={tot['user']} setting={tot['setting']} system={tot['system']}")
print(f"clusters providing a MAIN param: {sorted(hex(c) for c in MAIN_PARAMETER_BY_CLUSTER)}\n")
SPOT = {
    0x6: "OnOff",
    0x8: "LevelControl",
    0x300: "ColorControl",
    0x201: "Thermostat",
    0x202: "FanControl",
    0x102: "WindowCovering",
    0x101: "DoorLock",
    0x402: "TempMeas",
    0x405: "Humidity",
    0x2F: "PowerSource",
    0x50: "ModeSelect",
}
for cid, name, _is_sys, has_main, b in rows:
    if cid in SPOT:
        main = " [MAIN]" if has_main else ""
        print(
            f"--- 0x{cid:04X} {name}{main} — "
            f"user={len(b['user'])} setting={len(b['setting'])} system={len(b['system'])} ---"
        )
        if b["user"]:
            print(f"   user: {', '.join(b['user'][:16])}{' …' if len(b['user']) > 16 else ''}")
# biggest user buckets (over-exposure hotspots)
print("\n=== TOP over-exposed 'user' clusters (most read-only attrs shown to user) ===")
for cid, name, _is_sys, _has_main, b in sorted(rows, key=lambda r: -len(r[4]["user"]))[:10]:
    print(f"  0x{cid:04X} {name}: user={len(b['user'])}")
