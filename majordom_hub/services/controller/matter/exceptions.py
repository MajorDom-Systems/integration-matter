class MatterConnectionError(Exception):
    """Raised when Matter client is not started or not available."""
    pass


class MatterUnexpectedError(Exception):
    """Raised when an unexpected internal error occurs."""
    pass