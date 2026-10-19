# -*- coding: utf-8 -*-
"""entsoe-ext: a self-contained ENTSO-E Transparency Platform client.

Since 0.2.0 this package no longer wraps or patches entsoe-py: it implements
its own client (HTTP layer / chunking / pagination / exceptions / mappings)
plus parsers that replicate entsoe-py 0.8.1 semantics value-for-value
(equivalence was validated by an A/B suite during migration). entsoe-py is
**not** a runtime dependency.

Exports:

* ``EntsoeClient`` / ``get_client`` — the single client entry point (raw XML
  channels plus parsed Series/DataFrame methods);
* ``parse_a25`` — explicit A25 net-position parsing (domain direction);
* the exception hierarchy (``NoMatchingDataError`` / ``PaginationError`` /
  ``InvalidBusinessParameterError`` / ``InvalidPSRTypeError`` /
  ``InvalidParameterError``);
* series helpers (``resolution_to_timedelta_ext``, ``resample_sdac_native``,
  ``sdac_golive``, ``to_hourly_mean``, ``dedupe_by_index``).
"""
import logging

from entsoe_ext.a25 import parse_a25  # noqa: F401
from entsoe_ext.client import EntsoeClient, get_client  # noqa: F401
from entsoe_ext.exceptions import (  # noqa: F401
    InvalidBusinessParameterError,
    InvalidParameterError,
    InvalidPSRTypeError,
    NoMatchingDataError,
    PaginationError,
)
from entsoe_ext.series_ext import (  # noqa: F401
    dedupe_by_index,
    resolution_to_timedelta_ext,
    resample_sdac_native,
    sdac_golive,
    to_hourly_mean,
)

# Library best practice: attaching a NullHandler keeps merely importing this
# package from configuring the root logger; callers opt in to logging themselves.
logging.getLogger("entsoe_ext").addHandler(logging.NullHandler())

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "EntsoeClient",
    "get_client",
    "parse_a25",
    "NoMatchingDataError",
    "PaginationError",
    "InvalidBusinessParameterError",
    "InvalidPSRTypeError",
    "InvalidParameterError",
    "resolution_to_timedelta_ext",
    "resample_sdac_native",
    "sdac_golive",
    "to_hourly_mean",
    "dedupe_by_index",
]