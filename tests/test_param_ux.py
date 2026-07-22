"""Light probes for the parameter-UX mapping — exercise the code paths (visibility curation,
metadata resolver) without re-hardcoding every device's expected parameters (that stays a manual
review; see the audit script + the docs recipe)."""

from uuid import uuid4

from majordom_matter.mapper import MatterMapper
from majordom_matter.matter_spec import (
    EVERYDAY_CONTROL_ATTRIBUTES,
    METADATA_SOURCES,
    USER_READINGS,
    AttributeKey,
)


def _mapper() -> MatterMapper:
    return MatterMapper(lambda s: uuid4(), lambda d, s: uuid4())


class _FakeEndpoint:
    def __init__(self, values):
        self._values = values

    def get_attribute_value(self, cluster_id, attribute_id):
        return self._values.get((cluster_id, attribute_id))


def test_runtime_bounds_prefer_device_limit_attributes():
    # priority 1: CurrentLevel's min/max come from the device's own Min/MaxLevel attrs
    ep = _FakeEndpoint({(0x008, 0x02): 5, (0x008, 0x03): 200})
    lo, hi = _mapper().resolve_runtime_bounds(AttributeKey(0x008, 0x00), 0x008, ep, "CurrentLevel", 0, 254)
    assert (lo, hi) == (5, 200)


def test_runtime_bounds_fall_back_to_defaults_when_device_silent():
    ep = _FakeEndpoint({})  # device reports no limit attrs
    lo, hi = _mapper().resolve_runtime_bounds(AttributeKey(0x008, 0x00), 0x008, ep, "CurrentLevel", 0, 254)
    assert (lo, hi) == (0, 254)


def test_no_metadata_source_returns_defaults_unchanged():
    ep = _FakeEndpoint({(0x008, 0x02): 5})
    lo, hi = _mapper().resolve_runtime_bounds(AttributeKey(0x999, 0x99), 0x999, ep, "X", 1, 2)
    assert (lo, hi) == (1, 2)


def test_curation_sets_are_consistent():
    # a parameter can't be both an everyday writable control and a read-only user reading
    assert USER_READINGS.isdisjoint(EVERYDAY_CONTROL_ATTRIBUTES)
    # every metadata source key is a curated user reading/control (we only resolve bounds for shown params)
    shown = USER_READINGS | EVERYDAY_CONTROL_ATTRIBUTES
    assert set(METADATA_SOURCES).issubset(shown)


# --- classification ladder probes (see classify_attribute) --------------------------------------

def test_ladder_system_cluster_and_sensitive_forced_hidden():
    from majordom_integration_sdk.schemas.parameter import ParameterVisibility

    from majordom_matter.matter_spec import classify_attribute

    spec, source = classify_attribute(0x0028, 0x0000, "VendorName", writable=False, in_system_cluster=True)
    assert source == "system-cluster" and spec.visibility is ParameterVisibility.system
    spec, source = classify_attribute(
        0x0006, 0x0000, "AliroReaderVerificationKey", writable=True, in_system_cluster=False
    )
    assert source == "sensitive" and spec.visibility is ParameterVisibility.system


def test_ladder_our_override_beats_ha():
    from majordom_matter.matter_spec import OUR_ATTRIBUTE_UX, classify_attribute

    key = next(iter(OUR_ATTRIBUTE_UX))
    spec, source = classify_attribute(key.cluster_id, key.attribute_id, "x", writable=False, in_system_cluster=False)
    assert source == "ours"


def test_ladder_harvested_ha_and_fallback():
    from majordom_matter.matter_spec import MATTER_HA_ATTRIBUTE_UX, OUR_ATTRIBUTE_UX, classify_attribute

    assert len(MATTER_HA_ATTRIBUTE_UX) > 50  # the vendored HA harvest loaded
    our_keys = {(x.cluster_id, x.attribute_id) for x in OUR_ATTRIBUTE_UX}
    ha_only = next(k for k in MATTER_HA_ATTRIBUTE_UX if k not in our_keys)
    _, source = classify_attribute(ha_only[0], ha_only[1], "x", writable=True, in_system_cluster=False)
    assert source == "ha"
    # a wholly-uncurated attribute falls to the warning fallback
    _, source = classify_attribute(0x0ABC, 0x0001, "mystery", writable=True, in_system_cluster=False)
    assert source.startswith("fallback")
