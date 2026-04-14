from majordom_hub.schemas.parameter import ParameterUnit  # ParameterDataType, ParameterRole, ParameterVisibility  


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
    (0x300, 0x1): ParameterUnit.percentage,  # ColorControl.CurrentHue
    (0x300, 0x3): ParameterUnit.percentage,  # ColorControl.CurrentSaturation
    (0x400, 0x0): ParameterUnit.lux,  # IlluminanceMeasurement.MeasuredValue
    (0x402, 0x0): ParameterUnit.celsius,  # TemperatureMeasurement.MeasuredValue
    (0x403, 0x0): ParameterUnit.pascal,  # PressureMeasurement.MeasuredValue
    (0x405, 0x0): ParameterUnit.percentage,  # RelativeHumidityMeasurement.MeasuredValue
    (0x40c, 0x0): ParameterUnit.ppm,  # CarbonMonoxideMeasurement.MeasuredValue
    (0x40d, 0x0): ParameterUnit.ppm,  # CarbonDioxideMeasurement.MeasuredValue
    (0x42a, 0x0): ParameterUnit.ppm,  # Pm25Measurement.MeasuredValue
    (0x56, 0x0): ParameterUnit.celsius,  # TemperatureControl.TemperatureSetpoint
    (0x90, 0x4): ParameterUnit.watt,  # ElectricalPower.ActivePower
    (0x90, 0x8): ParameterUnit.volt,  # ElectricalPower.Voltage
    (0x90, 0x9): ParameterUnit.ampere,  # ElectricalPower.ActiveCurrent
    (0x91, 0x1): ParameterUnit.joule,  # ElectricalEnergy.CumulativeImported
    (0x2f, 0xb): ParameterUnit.percentage,  # PowerSource.BatPercentRemaining
}

ATTRIBUTE_MIN_STEPS: dict[tuple[int, int], int | float] = {
    (0x0008, 0x0000): 1,                # CurrentLevel (uint8, 0-254)
    (0x0300, 0x0000): 1,                # CurrentHue (проверить ID)
    (0x0300, 0x0001): 1,                # CurrentSaturation (проверить ID)
    (0x0300, 0x0007): 1,                # ColorTemperatureMireds (проверить ID)
    (0x0201, 0x0010): 0.01,             # LocalTemperatureCalibration (SignedTemperature, -2.5°C to 2.5°C)
    (0x0201, 0x0011): 0.01,             # OccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0012): 0.01,             # OccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0013): 0.01,             # UnoccupiedCoolingSetpoint (temperature type, 0.01°C resolution)
    (0x0201, 0x0014): 0.01,             # UnoccupiedHeatingSetpoint (temperature type, 0.01°C resolution)
    (0x0402, 0x0000): 0.01,             # MeasuredValue (temperature type, 0.01°C resolution)
    (0x0405, 0x0000): 0.01,             # MeasuredValue (0.01% resolution)
    (0x0403, 0x0000): 0.1,              # MeasuredValue 
    (0x0102, 0x0008): 0.01,             # CurrentPositionLiftPercentage (percent type, 1%)
    (0x0102, 0x0009): 0.01,             # CurrentPositionTiltPercentage (percent type, 1%)
    (0x0102, 0x000B): 0.01,             # TargetPositionLiftPercent100ths (percent100ths, 0.01%)
    (0x0102, 0x000C): 0.01,             # TargetPositionTiltPercent100ths (percent100ths, 0.01%)
    
}
