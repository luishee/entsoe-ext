# -*- coding: utf-8 -*-
"""entsoe-ext: opt-in fixes and explicit wrappers around entsoe-py 0.8.0.

A thin layer on top of the installed ``entsoe-py`` package (it does not vendor or
replace it). It:

* patches two "uncertain / TODO" spots of 0.8.0 (unknown-resolution handling and the
  ``per_hour`` aggregation of ``query_physical_crossborder_allborders``);
* exposes an explicit, single client entry point :func:`get_client`;
* provides explicit helpers (``resample_sdac_native``, ``to_hourly_mean``,
  ``resolution_to_timedelta_ext``, ``dedupe_by_index``) and an explicit A25
  net-position parser (:func:`parse_a25`, :func:`query_net_position_corrected`).

It deliberately does **not** rewrite the A25 sign: the raw XML ``<quantity>`` is a
magnitude and the direction is fully encoded by ``in_Domain``/``out_Domain``; the
library's ``factor * series.abs()`` already matches that semantics. See
``entsoe_ext.a25`` for the details.

See README.md for the full disposition table.
"""
import logging

from entsoe_ext.a25 import parse_a25, query_net_position_corrected  # noqa: F401
from entsoe_ext.client import get_client  # noqa: F401
from entsoe_ext.patch import install  # noqa: F401
from entsoe_ext.series_ext import (  # noqa: F401
    dedupe_by_index,
    resample_sdac_native,
    resolution_to_timedelta_ext,
    to_hourly_mean,
)

# Library best practice: attach a NullHandler so importing this package never
# configures the root logger, and callers can opt in to logging themselves.
logging.getLogger("entsoe_ext").addHandler(logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "get_client",
    "install",
    "parse_a25",
    "query_net_position_corrected",
    "resolution_to_timedelta_ext",
    "resample_sdac_native",
    "to_hourly_mean",
    "dedupe_by_index",
]