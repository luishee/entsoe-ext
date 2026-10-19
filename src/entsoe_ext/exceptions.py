# -*- coding: utf-8 -*-
"""entsoe_ext exception hierarchy (replicates entsoe-py 0.8.1 exceptions.py).

Callers should always ``from entsoe_ext import NoMatchingDataError`` — the
``entsoe`` package is not a runtime dependency of this project.
"""
__all__ = [
    "PaginationError",
    "NoMatchingDataError",
    "InvalidPSRTypeError",
    "InvalidBusinessParameterError",
    "InvalidParameterError",
]


class PaginationError(Exception):
    """Request quota exceeded (error text 'amount of requested data exceeds
    allowed limit' / '...offset parameter...'). Caught by
    ``chunking.paginate``, which retries with a binary window split."""
    pass


class NoMatchingDataError(Exception):
    """No published data within the window (HTTP 400 body containing
    'No matching data found', or HTTP 200 + XML content containing it)."""
    pass


class InvalidPSRTypeError(Exception):
    """Error body contains 'is not valid for this area'."""
    pass


class InvalidBusinessParameterError(Exception):
    """Error body contains 'check you request against dependency tables'."""
    pass


class InvalidParameterError(Exception):
    """Same-named exception as the library's (dead code there; kept so that
    ``except`` clauses written against it keep working)."""
    pass