from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid5

from chip.tlv import TLVReader
from chip.clusters.Types import NullValue

from majordom_hub.schemas.device import CredentialsType
from majordom_hub.schemas.parameter import ParameterDataType

from .matter_spec import MIN_MAX_VALUE


class MatterMapper:
    def matter_id_to_uuid(self, id: str) -> UUID:
        """Deterministically converts a Matter string identifier to a UUID."""
        return uuid5(NAMESPACE_DNS, id)

    def define_credentials_type(
        self,
        commissioning_mode: int | None = None,
        pairing_hint: int | None = None,
        pairing_instruction: str | None = None,
    ) -> CredentialsType:
        if not commissioning_mode or commissioning_mode == 0:
            return CredentialsType.none

        hint = pairing_hint or 0
        instruction = (pairing_instruction or "").lower()

        # Bitmask checks based on the Matter spec pairing hint bitmap.
        # Each bit indicates a supported commissioning method.
        if hint & (0x0004 | 0x0020 | 0x0008):
            return CredentialsType.qr
        if hint & (0x0002 | 0x0010) or "code" in instruction or "pin" in instruction:
            return CredentialsType.code.with_mask("DDD-DD-DDD")
        if hint & 0x0001:
            return CredentialsType.power_cycle

        return CredentialsType.none

    def get_parameter_data_type_from_value(self, value: Any) -> ParameterDataType:
        """Infers ParameterDataType from a Python runtime value."""
        if value is None:
            return ParameterDataType.none
        if isinstance(value, bool):
            # bool must be checked before int because bool is a subclass of int.
            return ParameterDataType.bool
        if isinstance(value, int):
            return ParameterDataType.integer
        if isinstance(value, float):
            return ParameterDataType.decimal
        if isinstance(value, str):
            return ParameterDataType.string
        if isinstance(value, (bytes, bytearray, memoryview)):
            return ParameterDataType.data
        return ParameterDataType.none

    def get_min_max_value(self, attribute, value) -> tuple[int | None, int | None]:
        """
        Encodes the attribute value to TLV to discover its wire type,
        then looks up the valid numeric range for that type from matter_spec.
        Returns (None, None) if the type is unknown or encoding fails.
        """
        try:
            tlv_bytes = attribute.ToTLV(None, value)
            reader = TLVReader(tlv_bytes)
            reader.get()
            tlv_type = reader.decoding[0]["type"]
            return MIN_MAX_VALUE.get(tlv_type, (None, None))
        except Exception:
            return None, None


    def parse_data_for_command(self, cmd_class: type, data: dict) -> dict:
        from typing import Type, get_args, get_origin, get_type_hints, override
        from dataclasses import fields, is_dataclass
        import enum
        hints = get_type_hints(cmd_class)
        result = {}
        for field in fields(cmd_class):
            if field.name not in data:
                continue
            raw = data[field.name]
            field_type = hints.get(field.name, field.type)

            # Unwrap Optional / Union
            if get_origin(field_type):
                args = [a for a in get_args(field_type) if a is not type(None)]
                field_type = args[0] if args else field_type

            if raw is None:
                result[field.name] = NullValue
            elif isinstance(field_type, type) and issubclass(field_type, enum.Enum):
                result[field.name] = field_type(int(raw))
            elif field_type in (bytes, bytearray):
                if isinstance(raw, str):
                    result[field.name] = raw.encode('utf-8')
                elif isinstance(raw, (list, tuple)):
                    result[field.name] = bytes(raw)
                else:
                    result[field.name] = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw
            else:
                result[field.name] = raw

        return result