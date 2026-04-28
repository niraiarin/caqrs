"""Shared utilities for ``caqrs.data.*`` clients.

The leading underscore marks this package as **internal**: stable
across CAQRS slices but not part of the public OSS API. Module
contents:

- :mod:`caqrs.data._common.rate_limit` — :class:`AsyncRateLimiter`,
  the shared throttling primitive used by J-Quants, Polymarket,
  yfinance, and EDINET clients.
"""

from caqrs.data._common.rate_limit import AsyncRateLimiter

__all__ = ["AsyncRateLimiter"]
