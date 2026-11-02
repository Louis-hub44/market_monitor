"""Socle partagé du terminal (réseau d'entreprise)."""

from terminal_core.network import (
    InsecureTLSError,
    NetworkConfigurationError,
    NetworkSettings,
    SecureSession,
    apply_to_yfinance,
    build_session,
    describe,
    get_session,
    reset_sessions,
)

__all__ = [
    "InsecureTLSError", "NetworkConfigurationError", "NetworkSettings", "SecureSession",
    "apply_to_yfinance", "build_session", "describe", "get_session", "reset_sessions",
]
__version__ = "0.1.0"
