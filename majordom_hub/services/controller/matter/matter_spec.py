from typing import Any, NamedTuple

from majordom_hub.schemas.parameter import ParameterUnit, ParameterDataType


from chip.tlv import (
    UINT8_MAX,
    UINT16_MAX,
    UINT32_MAX,
    UINT64_MAX,
    INT8_MIN,
    INT8_MAX,
    INT16_MIN,
    INT16_MAX,
    INT32_MIN,
    INT32_MAX,
    INT64_MIN,
    INT64_MAX,
)

SYSTEM_CLUSTERS: set[int] = {
    0x3,  # Identify
    0x4,  # Groups
    0x5,  # Scenes
    0x1d,  # Descriptor
    0x1e,  # Binding
    0x1f,  # AccessControl
    0x25,  # Actions
    0x28,  # BasicInformation
    0x29,  # OtaSoftwareUpdateProvider
    0x2a,  # OtaSoftwareUpdateRequestor
    0x2b,  # LocalizationConfiguration
    0x2c,  # TimeFormatLocalization
    0x2d,  # UnitLocalization
    0x2e,  # PowerSourceConfiguration
    0x9c,  # PowerTopology
    0x30,  # GeneralCommissioning
    0x31,  # NetworkCommissioning
    0x32,  # DiagnosticLogs
    0x33,  # GeneralDiagnostics
    0x34,  # SoftwareDiagnostics
    0x35,  # ThreadNetworkDiagnostics
    0x36,  # WiFiNetworkDiagnostics
    0x37,  # EthernetNetworkDiagnostics
    0x38,  # TimeSynchronization
    0x3c,  # AdministratorCommissioning
    0x3e,  # OperationalCredentials
    0x3f,  # GroupKeyManagement
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
    0x0000FFFD  # clusterRevision
}


class ValueRange(NamedTuple):
    min: int
    max: int


MIN_MAX_VALUE: dict[str, ValueRange] = {
    "Unsigned Integer 1-byte value": ValueRange(0, UINT8_MAX),
    "Unsigned Integer 2-byte value": ValueRange(0, UINT16_MAX),
    "Unsigned Integer 4-byte value": ValueRange(0, UINT32_MAX),
    "Unsigned Integer 8-byte value": ValueRange(0, UINT64_MAX),
    "Signed Integer 1-byte value":   ValueRange(INT8_MIN,  INT8_MAX),
    "Signed Integer 2-byte value":   ValueRange(INT16_MIN, INT16_MAX),
    "Signed Integer 4-byte value":   ValueRange(INT32_MIN, INT32_MAX),
    "Signed Integer 8-byte value":   ValueRange(INT64_MIN, INT64_MAX),
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
    AttributeKey(0x300, 0x0): ParameterUnit.arcdegree,  # ColorControl.CurrentHue (raw 0-254, degrees = value * 360 / 254)
    AttributeKey(0x300, 0x1): ParameterUnit.percentage,  # ColorControl.CurrentSaturation
    AttributeKey(0x400, 0x0): ParameterUnit.lux,  # IlluminanceMeasurement.MeasuredValue
    AttributeKey(0x402, 0x0): ParameterUnit.celsius,  # TemperatureMeasurement.MeasuredValue
    AttributeKey(0x405, 0x0): ParameterUnit.percentage,  # RelativeHumidityMeasurement.MeasuredValue
    AttributeKey(0x40c, 0x0): ParameterUnit.ppm,  # CarbonMonoxideMeasurement.MeasuredValue
    AttributeKey(0x40d, 0x0): ParameterUnit.ppm,  # CarbonDioxideMeasurement.MeasuredValue
    # Pm25Measurement.MeasuredValue: unit is device-configured (mass or molar concentration,
    # via the cluster's MeasurementUnit attribute) — not necessarily ppm. Left unmapped
    # (defaults to plain) rather than asserting a possibly-wrong unit.
    AttributeKey(0x56, 0x0): ParameterUnit.celsius,  # TemperatureControl.TemperatureSetpoint
    AttributeKey(0x90, 0x8): ParameterUnit.watt,  # ElectricalPowerMeasurement.ActivePower (raw mW, see ATTRIBUTE_SCALE)
    AttributeKey(0x90, 0x4): ParameterUnit.volt,  # ElectricalPowerMeasurement.Voltage (raw mV, see ATTRIBUTE_SCALE)
    AttributeKey(0x90, 0x5): ParameterUnit.ampere,  # ElectricalPowerMeasurement.ActiveCurrent (raw mA, see ATTRIBUTE_SCALE)
    AttributeKey(0x91, 0x1): ParameterUnit.joule,  # ElectricalEnergyMeasurement.CumulativeEnergyImported (struct; .energy in mWh, see ATTRIBUTE_SCALE)
    AttributeKey(0x403, 0x0): ParameterUnit.pascal,  # PressureMeasurement.MeasuredValue (raw deci-kPa, see ATTRIBUTE_SCALE)
    AttributeKey(0x2f, 0xc): ParameterUnit.percentage,  # PowerSource.BatPercentRemaining
}

# Multiplier applied to a raw attribute value to convert it into ATTRIBUTE_UNITS' base unit
# (e.g. Matter reports power in mW; watt is the base unit, so scale = 0.001).
ATTRIBUTE_SCALE: dict[AttributeKey, float] = {
    AttributeKey(0x90, 0x8): 0.001,   # ElectricalPowerMeasurement.ActivePower: mW -> W
    AttributeKey(0x90, 0x4): 0.001,   # ElectricalPowerMeasurement.Voltage: mV -> V
    AttributeKey(0x90, 0x5): 0.001,   # ElectricalPowerMeasurement.ActiveCurrent: mA -> A
    AttributeKey(0x91, 0x1): 3.6,     # ElectricalEnergyMeasurement.CumulativeEnergyImported: mWh -> Wh -> J (x0.001 x3600)
    AttributeKey(0x403, 0x0): 100,    # PressureMeasurement.MeasuredValue: deci-kPa -> Pa
}


ATTRIBUTE_MIN_STEPS: dict[AttributeKey, int | float] = {
    AttributeKey(0x0008, 0x0000): 1,                # CurrentLevel (uint8, 0-254)
    AttributeKey(0x0300, 0x0000): 1,                # CurrentHue (verified: id 0x0000, uint8, step 1)
    AttributeKey(0x0300, 0x0001): 1,                # CurrentSaturation (verified: id 0x0001, uint8, step 1)
    AttributeKey(0x0300, 0x0007): 1,                # ColorTemperatureMireds (verified: id 0x0007, uint16, step 1)
    AttributeKey(0x0201, 0x0010): 0.01,             # LocalTemperatureCalibration (SignedTemperature, -2.5°C to 2.5°C)
    AttributeKey(0x0201, 0x0011): 0.01,             # OccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0012): 0.01,             # OccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0013): 0.01,             # UnoccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0201, 0x0014): 0.01,             # UnoccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    AttributeKey(0x0402, 0x0000): 0.01,             # MeasuredValue (temperature type, 0.01°C resolution)
    AttributeKey(0x0405, 0x0000): 0.01,             # MeasuredValue (0.01% resolution)
    AttributeKey(0x0403, 0x0000): 10,               # MeasuredValue (raw step 0.1 deci-kPa, scaled x100 to Pa -> 10 Pa)
    AttributeKey(0x0102, 0x0008): 1,                # CurrentPositionLiftPercentage (plain percent 0-100, not Percent100ths)
    AttributeKey(0x0102, 0x0009): 1,                # CurrentPositionTiltPercentage (plain percent 0-100, not Percent100ths)
    AttributeKey(0x0102, 0x000B): 0.01,             # TargetPositionLiftPercent100ths (percent100ths, 0.01%)
    AttributeKey(0x0102, 0x000C): 0.01,             # TargetPositionTiltPercent100ths (percent100ths, 0.01%)
}


FIELD_TYPE_TO_DATA_TYPE: dict[type, ParameterDataType] = {
    bool: ParameterDataType.bool,
    int: ParameterDataType.integer,
    float: ParameterDataType.decimal,
    str: ParameterDataType.string,
}


# Writable attributes that are everyday, main-surface controls (ParameterVisibility.user)
# rather than configure-once settings. Only writable attributes need to be listed here —
# read-only attributes already map to `user`, and everyday controls exposed as *commands*
# (brightness/level, on/off, cover open-close, color changes) are `user` via parse_commands.
# So this set mostly covers clusters whose everyday control genuinely IS an attribute write.
EVERYDAY_CONTROL_ATTRIBUTES: set[AttributeKey] = {
    AttributeKey(0x202, 0x0),   # FanControl.FanMode (off/low/med/high/auto)
    AttributeKey(0x202, 0x2),   # FanControl.PercentSetting
    AttributeKey(0x202, 0x5),   # FanControl.SpeedSetting
    AttributeKey(0x201, 0x1C),  # Thermostat.SystemMode (off/heat/cool/auto)
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
    0x00000201: MainParameterSpec(0x00000000, {'mode': 1, 'amount': 5}),  # Thermostat.SetpointRaiseLower.Cool
    0x00000202: MainParameterSpec(0x00000000, 0x04),  # FanControl.FanMode.On(attribute)
    0x00000056: MainParameterSpec(0x00000000, {'targetTemperature': 22}),  # TemperatureControl.SetTemperature (targetTemperature/targetTemperatureLevel are feature-gated & mutually exclusive; targetTemperature covers the common "TN" feature case)
    0x00000060: MainParameterSpec(0x00000002, None),  # OperationalState.Start
    0x00000061: MainParameterSpec(0x00000003, None),  # RVCOperationalState.Resume
    0x0000005F: MainParameterSpec(0x00000001, {'timeToAdd': 30}),  # MicrowaveOvenControl.AddMoreTime
    0x00000050: MainParameterSpec(0x00000000, {'newMode': 0}),  # ModeSelect.ChangeToMode
    0x00000101: MainParameterSpec(0x00000001, {'PINCode': None}),  # DoorLock.UnlockDoor
    # 0x0000005C: MainParameterSpec(0x00000000, None),  # SmokeCoAlarm.SelfTestRequest
    0x00000506: MainParameterSpec(0x00000000, None),  # MediaPlayback.Play
    0x00000509: MainParameterSpec(0x00000000, {'keyCode': 0x44}),  # KeyPadInpud.SendKey.Play
    0x00000102: MainParameterSpec(0x00000000, None),  # WindowCovering.UpOrOpen — the everyday one-tap for a cover (open); StopMotion (0x02) only matters mid-motion, so it's not the primary action
    0x00000104: MainParameterSpec(0x00000001, {'position': 1}),  # ClosureControl.MoveTo{position=MoveToFullyOpen} — open is the everyday one-tap; ClosureControl has no no-arg Open command (a latched closure may also need latch=False)
    0x00000099: MainParameterSpec(0x00000001, None),  # EnergyEvse.Disable
    0x0000009E: MainParameterSpec(0x00000000, {'newMode': 0}),  # WaterHeaterMode.ChangeToMode
    0x00000553: MainParameterSpec(0x00000000, {'streamUsage': 0, 'originatingEndpointID': 0}),  # WebRTCTransportProvider.SolicitOffer
    0x00000556: MainParameterSpec(0x00000000, None),  # Chime.PlayChimeSound
    0x00000081: MainParameterSpec(0x00000000, None),  # ValveConfigurationAndControl.Open
}
