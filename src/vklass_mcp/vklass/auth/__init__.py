"""Interactive authentication adapters."""

from .goteborg import BankIDLoginError, authenticate

__all__ = ["BankIDLoginError", "authenticate"]
