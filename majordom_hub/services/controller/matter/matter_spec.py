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

MIN_MAX_VALUE: dict[str, tuple[int, int]] = {
    "Unsigned Integer 1-byte value": (0, UINT8_MAX),
    "Unsigned Integer 2-byte value": (0, UINT16_MAX),
    "Unsigned Integer 4-byte value": (0, UINT32_MAX),
    "Unsigned Integer 8-byte value": (0, UINT64_MAX),
    "Signed Integer 1-byte value":   (INT8_MIN,  INT8_MAX),
    "Signed Integer 2-byte value":   (INT16_MIN, INT16_MAX),
    "Signed Integer 4-byte value":   (INT32_MIN, INT32_MAX),
    "Signed Integer 8-byte value":   (INT64_MIN, INT64_MAX),
}


ATTRIBUTE_UNITS: dict[tuple[int, int], ParameterUnit] = {
    (0x8, 0x0): ParameterUnit.percentage,  # LevelControl.CurrentLevel
    (0x102, 0x8): ParameterUnit.percentage,  # WindowCovering.LiftPercentage
    (0x102, 0x9): ParameterUnit.percentage,  # WindowCovering.TiltPercentage
    (0x201, 0x0): ParameterUnit.celsius,  # Thermostat.LocalTemperature
    (0x201, 0x11): ParameterUnit.celsius,  # Thermostat.OccupiedCoolingSetpoint
    (0x201, 0x12): ParameterUnit.celsius,  # Thermostat.OccupiedHeatingSetpoint
    (0x202, 0x2): ParameterUnit.percentage,  # FanControl.PercentSetting
    (0x202, 0x3): ParameterUnit.percentage,  # FanControl.PercentCurrent
    (0x300, 0x0): ParameterUnit.arcdegree,  # ColorControl.CurrentHue (raw 0-254, degrees = value * 360 / 254)
    (0x300, 0x1): ParameterUnit.percentage,  # ColorControl.CurrentSaturation
    (0x400, 0x0): ParameterUnit.lux,  # IlluminanceMeasurement.MeasuredValue
    (0x402, 0x0): ParameterUnit.celsius,  # TemperatureMeasurement.MeasuredValue
    (0x405, 0x0): ParameterUnit.percentage,  # RelativeHumidityMeasurement.MeasuredValue
    (0x40c, 0x0): ParameterUnit.ppm,  # CarbonMonoxideMeasurement.MeasuredValue
    (0x40d, 0x0): ParameterUnit.ppm,  # CarbonDioxideMeasurement.MeasuredValue
    # Pm25Measurement.MeasuredValue: unit is device-configured (mass or molar concentration,
    # via the cluster's MeasurementUnit attribute) — not necessarily ppm. Left unmapped
    # (defaults to plain) rather than asserting a possibly-wrong unit.
    (0x56, 0x0): ParameterUnit.celsius,  # TemperatureControl.TemperatureSetpoint
    (0x90, 0x8): ParameterUnit.watt,  # ElectricalPowerMeasurement.ActivePower (raw mW, see ATTRIBUTE_SCALE)
    (0x90, 0x4): ParameterUnit.volt,  # ElectricalPowerMeasurement.Voltage (raw mV, see ATTRIBUTE_SCALE)
    (0x90, 0x5): ParameterUnit.ampere,  # ElectricalPowerMeasurement.ActiveCurrent (raw mA, see ATTRIBUTE_SCALE)
    (0x91, 0x1): ParameterUnit.joule,  # ElectricalEnergyMeasurement.CumulativeEnergyImported (struct; .energy in mWh, see ATTRIBUTE_SCALE)
    (0x403, 0x0): ParameterUnit.pascal,  # PressureMeasurement.MeasuredValue (raw deci-kPa, see ATTRIBUTE_SCALE)
    (0x2f, 0xc): ParameterUnit.percentage,  # PowerSource.BatPercentRemaining
}

# Multiplier applied to a raw attribute value to convert it into ATTRIBUTE_UNITS' base unit
# (e.g. Matter reports power in mW; watt is the base unit, so scale = 0.001).
ATTRIBUTE_SCALE: dict[tuple[int, int], float] = {
    (0x90, 0x8): 0.001,   # ElectricalPowerMeasurement.ActivePower: mW -> W
    (0x90, 0x4): 0.001,   # ElectricalPowerMeasurement.Voltage: mV -> V
    (0x90, 0x5): 0.001,   # ElectricalPowerMeasurement.ActiveCurrent: mA -> A
    (0x91, 0x1): 3.6,     # ElectricalEnergyMeasurement.CumulativeEnergyImported: mWh -> Wh -> J (x0.001 x3600)
    (0x403, 0x0): 100,    # PressureMeasurement.MeasuredValue: deci-kPa -> Pa
}


ATTRIBUTE_MIN_STEPS: dict[tuple[int, int], int | float] = {
    (0x0008, 0x0000): 1,                # CurrentLevel (uint8, 0-254)
    (0x0300, 0x0000): 1,                # CurrentHue (verified: id 0x0000, uint8, step 1)
    (0x0300, 0x0001): 1,                # CurrentSaturation (verified: id 0x0001, uint8, step 1)
    (0x0300, 0x0007): 1,                # ColorTemperatureMireds (verified: id 0x0007, uint16, step 1)
    (0x0201, 0x0010): 0.01,             # LocalTemperatureCalibration (SignedTemperature, -2.5°C to 2.5°C)
    (0x0201, 0x0011): 0.01,             # OccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0012): 0.01,             # OccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0013): 0.01,             # UnoccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0014): 0.01,             # UnoccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    (0x0402, 0x0000): 0.01,             # MeasuredValue (temperature type, 0.01°C resolution)
    (0x0405, 0x0000): 0.01,             # MeasuredValue (0.01% resolution)
    (0x0403, 0x0000): 10,               # MeasuredValue (raw step 0.1 deci-kPa, scaled x100 to Pa -> 10 Pa)
    (0x0102, 0x0008): 1,                # CurrentPositionLiftPercentage (plain percent 0-100, not Percent100ths)
    (0x0102, 0x0009): 1,                # CurrentPositionTiltPercentage (plain percent 0-100, not Percent100ths)
    (0x0102, 0x000B): 0.01,             # TargetPositionLiftPercent100ths (percent100ths, 0.01%)
    (0x0102, 0x000C): 0.01,             # TargetPositionTiltPercent100ths (percent100ths, 0.01%)
}


FIELD_TYPE_TO_DATA_TYPE: dict[type, ParameterDataType] = {
    bool: ParameterDataType.bool,
    int: ParameterDataType.integer,
    float: ParameterDataType.decimal,
    str: ParameterDataType.string,
}


MAIN_PARAMETER_BY_CLUSTER = [
    # cluster_id, command_id, default_params
    (0x00000006, 0x00000002, None),  # OnOff.Toggle
    (0x00000201, 0x00000000, {'mode': 1, 'amount': 5}),  # Thermostat.SetpointRaiseLower.Cool
    (0x00000202, 0x00000000, 0x04),  # FanControl.FanMode.On(attribute)
    (0x00000056, 0x00000000, {'targetTemperature': 22}),  # TemperatureControl.SetTemperature (targetTemperature/targetTemperatureLevel are feature-gated & mutually exclusive; targetTemperature covers the common "TN" feature case)
    (0x00000060, 0x00000002, None),  # OperationalState.Start
    (0x00000061, 0x00000003, None),  # RVCOperationalState.Resume
    (0x0000005F, 0x00000001, {'timeToAdd': 30}),  # MicrowaveOvenControl.AddMoreTime
    (0x00000050, 0x00000000, {'newMode': 0}),  # ModeSelect.ChangeToMode
    (0x00000101, 0x00000001, {'PINCode': None}),  # DoorLock.UnlockDoor
    # (0x0000005C, 0x00000000, None),  # SmokeCoAlarm.SelfTestRequest
    (0x00000506, 0x00000000, None),  # MediaPlayback.Play
    (0x00000509, 0x00000000, {'keyCode': 0x44}),  # KeyPadInpud.SendKey.Play
    (0x00000102, 0x00000002, None),  # WindowCovering.StopMotion
    # (0x00000102, 0x00000000, None),  # WindowCovering.UpOrOpen
    (0x00000104, 0x00000000, None),  # ClosureControl.Stop
    (0x00000099, 0x00000001, None),  # EnergyEvse.Disable
    (0x0000009E, 0x00000000, {'newMode': 0}),  # WaterHeaterMode.ChangeToMode
    (0x00000553, 0x00000000, {'streamUsage': 0, 'originatingEndpointID': 0}),  # WebRTCTransportProvider.SolicitOffer
    (0x00000556, 0x00000000, None),  # Chime.PlayChimeSound
    (0x00000081, 0x00000000, None),  # ValveConfigurationAndControl.Open
]
