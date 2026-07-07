class MatterConnectionError(Exception):
    """Raised when Matter client is not started or not available."""


class MatterUnexpectedError(Exception):
    """Raised when an unexpected internal error occurs."""


class MatterUnsupportedParameter(Exception):
    """Raised when a parameter is not supported by the device."""

class MatterNotFoundParameter(Exception):
    """Raised when a parameter was not found on the device."""