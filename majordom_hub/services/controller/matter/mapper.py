from uuid import NAMESPACE_DNS, UUID, uuid5
from typing import Any

from majordom_hub.schemas.device import CredentialsType
from majordom_hub.schemas.parameter import ParameterDataType


class MatterMapper():

    def matter_id_to_uuid(self, id: str) -> UUID:
        return uuid5(NAMESPACE_DNS, id)
    
    def define_credentials_type(
        self, commissioning_mode: int | None = None,
        pairing_hint: int | None = None,
        pairing_instruction: str | None = None,
    ) -> CredentialsType:

        if not commissioning_mode or commissioning_mode == 0:
            return CredentialsType.none
    
        hint = pairing_hint or 0
        instruction = (pairing_instruction or "").lower()
    
        if hint & (0x0004 | 0x0020 | 0x0008):
            return CredentialsType.qr
    
        if hint & (0x0002 | 0x0010) or "code" in instruction or "pin" in instruction:
            return CredentialsType.code.with_mask("DDD-DD-DDD")
    
        if hint & 0x0001:
            return CredentialsType.power_cycle
    
        return CredentialsType.none

    def get_parameter_data_type(self, value: Any) -> ParameterDataType:
        if value is None:
            return ParameterDataType.none
        if isinstance(value, bool):
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
