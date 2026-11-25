"""Quotex connection adapter (isolated from every other broker)."""

from .connector import QuotexConnector

__all__ = ["QuotexConnector"]
