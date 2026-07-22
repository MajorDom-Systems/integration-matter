from typing import Any, NamedTuple

from chip.tlv import (
    INT8_MAX,
    INT8_MIN,
    INT16_MAX,
    INT16_MIN,
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    UINT8_MAX,
    UINT16_MAX,
    UINT32_MAX,
    UINT64_MAX,
)
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterUnit,
    ParameterVisibility,
)

from .matter_spec_ha import MATTER_HA_ATTRIBUTE_UX

SYSTEM_CLUSTERS: set[int] = {
    0x3,  # Identify
    0x4,  # Groups
    0x5,  # Scenes
    0x1D,  # Descriptor
    0x1E,  # Binding
    0x1F,  # AccessControl
    0x25,  # Actions
    0x28,  # BasicInformation
    0x29,  # OtaSoftwareUpdateProvider
    0x2A,  # OtaSoftwareUpdateRequestor
    0x2B,  # LocalizationConfiguration
    0x2C,  # TimeFormatLocalization
    0x2D,  # UnitLocalization
    0x2E,  # PowerSourceConfiguration
    0x9C,  # PowerTopology
    0x30,  # GeneralCommissioning
    0x31,  # NetworkCommissioning
    0x32,  # DiagnosticLogs
    0x33,  # GeneralDiagnostics
    0x34,  # SoftwareDiagnostics
    0x35,  # ThreadNetworkDiagnostics
    0x36,  # WiFiNetworkDiagnostics
    0x37,  # EthernetNetworkDiagnostics
    0x38,  # TimeSynchronization
    0x3C,  # AdministratorCommissioning
    0x3E,  # OperationalCredentials
    0x3F,  # GroupKeyManagement
    0x40,  # FixedLabel
    0x41,  # UserLabel
    0x62,  # ScenesManagement
}

SYSTEM_ATTRIBUTES: set[int] = {
    0x0000FFF8,  # generatedCommandList
    0x0000FFF9,  # acceptedCommandList
    0x0000FFFA,  # eventList
    0x0000FFFB,  # attributeList
    0x0000FFFC,  # featureMap
    0x0000FFFD,  # clusterRevision
}


class ValueRange(NamedTuple):
    min: int
    max: int


MIN_MAX_VALUE: dict[str, ValueRange] = {
    "Unsigned Integer 1-byte value": ValueRange(0, UINT8_MAX),
    "Unsigned Integer 2-byte value": ValueRange(0, UINT16_MAX),
    "Unsigned Integer 4-byte value": ValueRange(0, UINT32_MAX),
    "Unsigned Integer 8-byte value": ValueRange(0, UINT64_MAX),
    "Signed Integer 1-byte value": ValueRange(INT8_MIN, INT8_MAX),
    "Signed Integer 2-byte value": ValueRange(INT16_MIN, INT16_MAX),
    "Signed Integer 4-byte value": ValueRange(INT32_MIN, INT32_MAX),
    "Signed Integer 8-byte value": ValueRange(INT64_MIN, INT64_MAX),
}


class AttributeKey(NamedTuple):
    """Identifies a single attribute on a cluster — the shared dict key shape for
    ATTRIBUTE_UNITS / ATTRIBUTE_SCALE / ATTRIBUTE_MIN_STEPS below."""

    cluster_id: int
    attribute_id: int


ATTRIBUTE_UNITS: dict[AttributeKey, ParameterUnit] = {
    AttributeKey(0x8, 0x0): ParameterUnit.percentage,  # LevelControl.CurrentLevel
    AttributeKey(0x102, 0x8): ParameterUnit.percentage,  # WindowCovering.LiftPercentage
    AttributeKey(0x102, 0x9): ParameterUnit.percentage,  # WindowCovering.TiltPercentage
    AttributeKey(0x201, 0x0): ParameterUnit.celsius,  # Thermostat.LocalTemperature
    AttributeKey(0x201, 0x11): ParameterUnit.celsius,  # Thermostat.OccupiedCoolingSetpoint
    AttributeKey(0x201, 0x12): ParameterUnit.celsius,  # Thermostat.OccupiedHeatingSetpoint
    AttributeKey(0x202, 0x2): ParameterUnit.percentage,  # FanControl.PercentSetting
    AttributeKey(0x202, 0x3): ParameterUnit.percentage,  # FanControl.PercentCurrent
    AttributeKey(
        0x300, 0x0
    ): ParameterUnit.arcdegree,  # ColorControl.CurrentHue (raw 0-254, degrees = value * 360 / 254)
    AttributeKey(0x300, 0x1): ParameterUnit.percentage,  # ColorControl.CurrentSaturation
    AttributeKey(0x300, 0x7): ParameterUnit.mired,  # ColorControl.ColorTemperatureMireds
    AttributeKey(0x404, 0x0): ParameterUnit.m3h,  # FlowMeasurement.MeasuredValue (deci-m3/h, see scale)
    AttributeKey(0x400, 0x0): ParameterUnit.lux,  # IlluminanceMeasurement.MeasuredValue
    AttributeKey(0x402, 0x0): ParameterUnit.celsius,  # TemperatureMeasurement.MeasuredValue
    AttributeKey(0x405, 0x0): ParameterUnit.percentage,  # RelativeHumidityMeasurement.MeasuredValue
    AttributeKey(0x40C, 0x0): ParameterUnit.ppm,  # CarbonMonoxideMeasurement.MeasuredValue
    AttributeKey(0x40D, 0x0): ParameterUnit.ppm,  # CarbonDioxideMeasurement.MeasuredValue
    # Pm25Measurement.MeasuredValue: unit is device-configured (mass or molar concentration,
    # via the cluster's MeasurementUnit attribute) — not necessarily ppm. Left unmapped
    # (defaults to plain) rather than asserting a possibly-wrong unit.
    AttributeKey(0x56, 0x0): ParameterUnit.celsius,  # TemperatureControl.TemperatureSetpoint
    AttributeKey(0x90, 0x8): ParameterUnit.watt,  # ElectricalPowerMeasurement.ActivePower (raw mW, see ATTRIBUTE_SCALE)
    AttributeKey(0x90, 0x4): ParameterUnit.volt,  # ElectricalPowerMeasurement.Voltage (raw mV, see ATTRIBUTE_SCALE)
    AttributeKey(
        0x90, 0x5
    ): ParameterUnit.ampere,  # ElectricalPowerMeasurement.ActiveCurrent (raw mA, see ATTRIBUTE_SCALE)
    AttributeKey(
        0x91,
        0x1,
        # ElectricalEnergyMeasurement.CumulativeEnergyImported (struct; .energy in mWh, see ATTRIBUTE_SCALE)
    ): ParameterUnit.kwh,
    AttributeKey(
        0x403, 0x0
    ): ParameterUnit.pascal,  # PressureMeasurement.MeasuredValue (raw deci-kPa, see ATTRIBUTE_SCALE)
    AttributeKey(0x2F, 0xC): ParameterUnit.percentage,  # PowerSource.BatPercentRemaining
}

# Multiplier applied to a raw attribute value to convert it into ATTRIBUTE_UNITS' base unit
# (e.g. Matter reports power in mW; watt is the base unit, so scale = 0.001).
ATTRIBUTE_SCALE: dict[AttributeKey, float] = {
    AttributeKey(0x90, 0x8): 0.001,  # ElectricalPowerMeasurement.ActivePower: mW -> W
    AttributeKey(0x90, 0x4): 0.001,  # ElectricalPowerMeasurement.Voltage: mV -> V
    AttributeKey(0x90, 0x5): 0.001,  # ElectricalPowerMeasurement.ActiveCurrent: mA -> A
    AttributeKey(0x91, 0x1): 1e-6,  # ElectricalEnergyMeasurement.CumulativeEnergyImported: mWh -> kWh
    AttributeKey(0x404, 0x0): 0.1,  # FlowMeasurement.MeasuredValue: deci-m3/h -> m3/h
    AttributeKey(0x403, 0x0): 100,  # PressureMeasurement.MeasuredValue: deci-kPa -> Pa
}


ATTRIBUTE_MIN_STEPS: dict[AttributeKey, int | float] = {
    AttributeKey(0x0008, 0x0000): 1,  # CurrentLevel (uint8, 0-254)
    AttributeKey(0x0300, 0x0000): 1,  # CurrentHue (verified: id 0x0000, uint8, step 1)
    AttributeKey(0x0300, 0x0001): 1,  # CurrentSaturation (verified: id 0x0001, uint8, step 1)
    AttributeKey(0x0300, 0x0007): 1,  # ColorTemperatureMireds (verified: id 0x0007, uint16, step 1)
    AttributeKey(0x0201, 0x0010): 0.01,  # LocalTemperatureCalibration (SignedTemperature, -2.5°C to 2.5°C)
    AttributeKey(0x0201, 0x0011): 0.01,  # OccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0012): 0.01,  # OccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0013): 0.01,  # UnoccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0014): 0.01,  # UnoccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0402, 0x0000): 0.01,  # MeasuredValue (temperature type, 0.01°C resolution)
    AttributeKey(0x0405, 0x0000): 0.01,  # MeasuredValue (0.01% resolution)
    AttributeKey(0x0403, 0x0000): 10,  # MeasuredValue (raw step 0.1 deci-kPa, scaled x100 to Pa -> 10 Pa)
    AttributeKey(0x0102, 0x0008): 1,  # CurrentPositionLiftPercentage (plain percent 0-100, not Percent100ths)
    AttributeKey(0x0102, 0x0009): 1,  # CurrentPositionTiltPercentage (plain percent 0-100, not Percent100ths)
    AttributeKey(0x0102, 0x000B): 0.01,  # TargetPositionLiftPercent100ths (percent100ths, 0.01%)
    AttributeKey(0x0102, 0x000C): 0.01,  # TargetPositionTiltPercent100ths (percent100ths, 0.01%)
}


class MetadataSource(NamedTuple):
    """Sibling attributes whose runtime VALUES provide a parameter's min/max — the device's own
    limit attributes (the ones we hide from the UI as metadata). Priority 1 in the resolver:
    runtime sibling value > spec table > wire-type default."""

    min_attr: int | None = None
    max_attr: int | None = None


METADATA_SOURCES: dict[AttributeKey, MetadataSource] = {
    AttributeKey(0x008, 0x00): MetadataSource(0x02, 0x03),  # LevelControl.CurrentLevel <- Min/MaxLevel
    AttributeKey(0x402, 0x00): MetadataSource(0x01, 0x02),  # TemperatureMeasurement <- Min/MaxMeasuredValue
    AttributeKey(0x405, 0x00): MetadataSource(0x01, 0x02),  # RelativeHumidity <- Min/MaxMeasuredValue
    AttributeKey(0x400, 0x00): MetadataSource(0x01, 0x02),  # Illuminance <- Min/MaxMeasuredValue
    AttributeKey(0x403, 0x00): MetadataSource(0x01, 0x02),  # Pressure <- Min/MaxMeasuredValue
    AttributeKey(0x404, 0x00): MetadataSource(0x01, 0x02),  # Flow <- Min/MaxMeasuredValue
    AttributeKey(0x201, 0x11): MetadataSource(0x05, 0x06),  # OccupiedCoolingSetpoint <- AbsMin/MaxCoolSetpointLimit
    AttributeKey(0x201, 0x12): MetadataSource(0x03, 0x04),  # OccupiedHeatingSetpoint <- AbsMin/MaxHeatSetpointLimit
    AttributeKey(0x300, 0x07): MetadataSource(0x400B, 0x400C),  # ColorTemperatureMireds <- physical min/max mireds
}


FIELD_TYPE_TO_DATA_TYPE: dict[type, ParameterDataType] = {
    bool: ParameterDataType.bool,
    int: ParameterDataType.integer,
    float: ParameterDataType.decimal,
    str: ParameterDataType.string,
}


# --- Visibility curation (see docs/device-integration/parameter-visibility recipe) ------------
# The mapper defaults a read-only attribute to `system` (hidden) and only promotes it to `user`
# if it's an explicitly curated live reading (USER_READINGS). Writable attributes default to
# `setting`, promoted to `user` only if they're an everyday control (EVERYDAY_CONTROL_ATTRIBUTES).
# This inverts the old "every read-only -> user" flood; anything not curated stays hidden and can
# be surfaced by the user, or added here. Bounds/capabilities/counts fall through to `system` and
# double as metadata sources (see ATTRIBUTE_MIN_STEPS / min-max resolution).

# Writable attributes that are everyday, main-surface controls (-> user) rather than
# configure-once settings.
EVERYDAY_CONTROL_ATTRIBUTES: set[AttributeKey] = {
    AttributeKey(0x202, 0x0),  # FanControl.FanMode (off/low/med/high/auto)
    AttributeKey(0x202, 0x2),  # FanControl.PercentSetting
    AttributeKey(0x202, 0x5),  # FanControl.SpeedSetting
    AttributeKey(0x201, 0x1C),  # Thermostat.SystemMode (off/heat/cool/auto)
    AttributeKey(0x201, 0x11),  # Thermostat.OccupiedCoolingSetpoint (the everyday "set the temp")
    AttributeKey(0x201, 0x12),  # Thermostat.OccupiedHeatingSetpoint
    AttributeKey(0x056, 0x00),  # TemperatureControl.TemperatureSetpoint
}

# Read-only attributes that ARE the live, everyday reading for their cluster (-> user). Anything
# read-only and NOT listed here stays `system` (bounds, capabilities, counts, diagnostics).
USER_READINGS: set[AttributeKey] = {
    AttributeKey(0x006, 0x00),  # OnOff.OnOff
    AttributeKey(0x008, 0x00),  # LevelControl.CurrentLevel
    AttributeKey(0x300, 0x00),  # ColorControl.CurrentHue (human color model)
    AttributeKey(0x300, 0x01),  # ColorControl.CurrentSaturation
    AttributeKey(0x300, 0x07),  # ColorControl.ColorTemperatureMireds
    # CurrentX/CurrentY (0x03/0x04) are the CIE machine encoding of the same colour -> system
    # (redundant with hue/sat for the user; still available to patch to user if wanted).
    AttributeKey(0x201, 0x00),  # Thermostat.LocalTemperature
    AttributeKey(0x202, 0x03),  # FanControl.PercentCurrent
    AttributeKey(0x202, 0x06),  # FanControl.SpeedCurrent (0x04 is SpeedMax, a bound -> stays system)
    AttributeKey(0x402, 0x00),  # TemperatureMeasurement.MeasuredValue
    AttributeKey(0x405, 0x00),  # RelativeHumidityMeasurement.MeasuredValue
    AttributeKey(0x400, 0x00),  # IlluminanceMeasurement.MeasuredValue
    AttributeKey(0x403, 0x00),  # PressureMeasurement.MeasuredValue
    AttributeKey(0x404, 0x00),  # FlowMeasurement.MeasuredValue
    AttributeKey(0x406, 0x00),  # OccupancySensing.Occupancy
    AttributeKey(0x045, 0x00),  # BooleanState.StateValue (contact/water leak)
    AttributeKey(0x40C, 0x00),  # CarbonMonoxideConcentrationMeasurement.MeasuredValue
    AttributeKey(0x40D, 0x00),  # CarbonDioxideConcentrationMeasurement.MeasuredValue
    AttributeKey(0x42A, 0x00),  # Pm25ConcentrationMeasurement.MeasuredValue
    AttributeKey(0x101, 0x00),  # DoorLock.LockState
    AttributeKey(0x101, 0x03),  # DoorLock.DoorState
    AttributeKey(0x102, 0x08),  # WindowCovering.CurrentPositionLiftPercentage
    AttributeKey(0x102, 0x09),  # WindowCovering.CurrentPositionTiltPercentage
    AttributeKey(0x02F, 0x0C),  # PowerSource.BatPercentRemaining
    AttributeKey(0x02F, 0x0E),  # PowerSource.BatChargeLevel
    AttributeKey(0x060, 0x04),  # OperationalState.OperationalState
    AttributeKey(0x061, 0x04),  # RvcOperationalState.OperationalState
    AttributeKey(0x05C, 0x00),  # SmokeCoAlarm.ExpressedState
    AttributeKey(0x05C, 0x01),  # SmokeCoAlarm.SmokeState
    AttributeKey(0x05C, 0x02),  # SmokeCoAlarm.COState
    AttributeKey(0x050, 0x03),  # ModeSelect.CurrentMode
    # Energy metering readings (the primaries; min/max/overload/phase variants stay system)
    AttributeKey(0x090, 0x04),  # ElectricalPowerMeasurement.Voltage
    AttributeKey(0x090, 0x05),  # ElectricalPowerMeasurement.ActiveCurrent
    AttributeKey(0x090, 0x08),  # ElectricalPowerMeasurement.ActivePower
    AttributeKey(0x091, 0x01),  # ElectricalEnergyMeasurement.CumulativeEnergyImported
}

# Attribute-name prefixes that carry security material and must never be shown to the user
# (-> system regardless of role). Matter DoorLock's Aliro* attributes are cryptographic keys /
# identifiers that the current "read-only -> user" rule was leaking straight into the tap-view.
SENSITIVE_ATTRIBUTE_NAME_PREFIXES: tuple[str, ...] = ("Aliro",)

# Commands that are everyday one-tap actions (-> user). Every other command on a non-system
# cluster defaults to `setting` — advanced management (schedules, credentials, logs, calibration).
EVERYDAY_COMMANDS: set[tuple[int, int]] = {
    (0x006, 0x0),
    (0x006, 0x1),
    (0x006, 0x2),  # OnOff Off / On / Toggle
    (0x008, 0x0),
    (0x008, 0x4),  # LevelControl MoveToLevel / MoveToLevelWithOnOff
    (0x300, 0x0),
    (0x300, 0x3),
    (0x300, 0x6),
    (0x300, 0x7),
    (0x300, 0xA),  # ColorControl hue/sat/hue+sat/color/temp
    (0x102, 0x0),
    (0x102, 0x1),
    (0x102, 0x2),
    (0x102, 0x5),  # WindowCovering up / down / stop / goToLift%
    (0x101, 0x0),
    (0x101, 0x1),  # DoorLock Lock / Unlock  (credential/schedule cmds stay setting)
    (0x201, 0x0),  # Thermostat SetpointRaiseLower
    (0x056, 0x0),  # TemperatureControl SetTemperature
    (0x060, 0x0),
    (0x060, 0x1),
    (0x060, 0x2),
    (0x060, 0x3),  # OperationalState Pause/Stop/Start/Resume
    (0x061, 0x0),
    (0x061, 0x3),  # RvcOperationalState Pause / Resume
    (0x050, 0x0),  # ModeSelect ChangeToMode
    (0x506, 0x0),
    (0x506, 0x1),
    (0x506, 0x2),  # MediaPlayback Play / Pause / Stop
}


# Arguments to send along with a main-parameter command/attribute when the parameter itself
# doesn't carry an obvious "activate" value (e.g. a mode/setpoint command). None means the
# command takes no arguments; an int means a raw attribute value (only used for the FanControl
# case below, where command_or_attribute_id is actually an attribute id).
DefaultParams = dict[str, Any] | int | None


class MainParameterSpec(NamedTuple):
    """Value of MAIN_PARAMETER_BY_CLUSTER, keyed by cluster_id: which command (or — for
    FanControl specifically — attribute) makes a sensible main_parameter for that cluster,
    and what default_params to send with it if the parameter has no obvious "activate"
    value. Iteration order is priority order — the first cluster below that's present on
    the device wins."""

    command_or_attribute_id: int
    default_params: DefaultParams


MAIN_PARAMETER_BY_CLUSTER: dict[int, MainParameterSpec] = {
    0x00000006: MainParameterSpec(0x00000002, None),  # OnOff.Toggle
    # Thermostat intentionally has NO one-tap: "raise or lower?" isn't a sensible tile action —
    # tapping a thermostat should open its screen, not nudge a setpoint blind.
    0x00000202: MainParameterSpec(0x00000000, 0x04),  # FanControl.FanMode.On(attribute)
    0x00000056: MainParameterSpec(
        0x00000000,
        {"targetTemperature": 22},
        # TemperatureControl.SetTemperature (targetTemperature/targetTemperatureLevel are
        # feature-gated & mutually exclusive; targetTemperature covers the common "TN" case)
    ),
    0x00000060: MainParameterSpec(0x00000002, None),  # OperationalState.Start
    0x00000061: MainParameterSpec(0x00000003, None),  # RVCOperationalState.Resume
    0x0000005F: MainParameterSpec(0x00000001, {"timeToAdd": 30}),  # MicrowaveOvenControl.AddMoreTime
    0x00000050: MainParameterSpec(0x00000000, {"newMode": 0}),  # ModeSelect.ChangeToMode
    0x00000101: MainParameterSpec(0x00000001, {"PINCode": None}),  # DoorLock.UnlockDoor
    # 0x0000005C: MainParameterSpec(0x00000000, None),  # SmokeCoAlarm.SelfTestRequest
    0x00000506: MainParameterSpec(0x00000000, None),  # MediaPlayback.Play
    0x00000509: MainParameterSpec(0x00000000, {"keyCode": 0x44}),  # KeyPadInpud.SendKey.Play
    0x00000102: MainParameterSpec(
        0x00000000,
        None,
        # WindowCovering.UpOrOpen — the everyday one-tap for a cover (open); StopMotion
        # (0x02) only matters mid-motion, so it's not the primary action
    ),
    0x00000104: MainParameterSpec(
        0x00000001,
        {"position": 1},
        # ClosureControl.MoveTo{position=MoveToFullyOpen} — open is the everyday one-tap;
        # ClosureControl has no no-arg Open command (a latched closure may also need latch=False)
    ),
    0x00000099: MainParameterSpec(0x00000001, None),  # EnergyEvse.Disable
    0x0000009E: MainParameterSpec(0x00000000, {"newMode": 0}),  # WaterHeaterMode.ChangeToMode
    0x00000553: MainParameterSpec(
        0x00000000, {"streamUsage": 0, "originatingEndpointID": 0}
    ),  # WebRTCTransportProvider.SolicitOffer
    0x00000556: MainParameterSpec(0x00000000, None),  # Chime.PlayChimeSound
    0x00000081: MainParameterSpec(0x00000000, None),  # ValveConfigurationAndControl.Open
}


# --- Merged UX classification ladder (mirror of the zigbee integration; see the matter README) ---
# Matter has no runtime quirk layer, so the ladder is: our hand overrides > harvested HA judgment >
# fallback. Safety rules (system cluster, sensitive crypto material) are forced hidden on top.


class UxSpec(NamedTuple):
    visibility: ParameterVisibility
    role: ParameterRole | None = None
    unit: ParameterUnit | None = None


def _our_attribute_ux() -> dict[AttributeKey, UxSpec]:
    out: dict[AttributeKey, UxSpec] = {}
    for key in USER_READINGS:
        out[key] = UxSpec(ParameterVisibility.user, ParameterRole.sensor)
    for key in EVERYDAY_CONTROL_ATTRIBUTES:
        out[key] = UxSpec(ParameterVisibility.user, ParameterRole.control)
    return out


OUR_ATTRIBUTE_UX: dict[AttributeKey, UxSpec] = _our_attribute_ux()


def _ha_uxspec(key: AttributeKey) -> UxSpec | None:
    t = MATTER_HA_ATTRIBUTE_UX.get((key.cluster_id, key.attribute_id))
    if t is None:
        return None
    return UxSpec(ParameterVisibility(t[0]), ParameterRole(t[1]), ParameterUnit(t[2]))


# Flip once harvested coverage is validated on real devices: unmatched writable attrs then hide
# (system) instead of defaulting to a settings toggle. See the matter README (fallback).
_FALLBACK_HIDE_UNCURATED = False


def classify_attribute(
    cluster_id: int,
    attribute_id: int,
    name: str,
    *,
    writable: bool,
    in_system_cluster: bool,
) -> tuple[UxSpec, str]:
    """Resolve an attribute's (visibility, role, unit) by the priority ladder, returning the spec
    and a source tag. First match wins:

      - system cluster / sensitive crypto material -> system (safety, top priority)
      1. OUR_ATTRIBUTE_UX          — hand curation
      2. MATTER_HA_ATTRIBUTE_UX    — harvested HA entity judgment
      3. fallback policy           — writable -> setting, else system; WARNS (uncurated)
    """
    if in_system_cluster:
        return UxSpec(ParameterVisibility.system), "system-cluster"
    if name.startswith(SENSITIVE_ATTRIBUTE_NAME_PREFIXES):
        return UxSpec(ParameterVisibility.system), "sensitive"

    key = AttributeKey(cluster_id, attribute_id)
    if (spec := OUR_ATTRIBUTE_UX.get(key)) is not None:
        return spec, "ours"
    if (spec := _ha_uxspec(key)) is not None:
        return spec, "ha"

    if not _FALLBACK_HIDE_UNCURATED and writable:
        return UxSpec(ParameterVisibility.setting, ParameterRole.control), "fallback-writable"
    return UxSpec(ParameterVisibility.system), "fallback-system"
