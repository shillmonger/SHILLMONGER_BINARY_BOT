"""Typed errors mapped to structured API responses by the sidecar."""


class BrokerError(Exception):
    """Base class. `code` drives the HTTP/JSON error mapping."""

    code = "error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class BrokerAuthError(BrokerError):
    code = "auth"


class BrokerOtpRequired(BrokerError):
    """Broker demands a 2FA/OTP code before the session is established."""

    code = "otp_required"

    def __init__(self, message: str = "Two-factor code required"):
        super().__init__(message)


class BrokerTimeoutError(BrokerError):
    code = "timeout"


class BrokerUnavailableError(BrokerError):
    """Library missing or broker unreachable."""

    code = "unavailable"
