# -*- coding: utf-8 -*-
"""A25 (day-ahead net position) self-parsed: explicit domain direction.

**Core findings (measured 2026-10-18 against raw A25 XML):**

1. Every ``<quantity>`` of an A25 ``TimeSeries`` is **always a magnitude
   (>= 0)**; the direction is encoded 100% by ``in_Domain.mRID`` /
   ``out_Domain.mRID``:
     - ``out_Domain`` == own EIC -> export (positive)
     - ``in_Domain``  == own EIC -> import (negative)
2. The library's ``parse_netpositions`` (``factor * series.abs()``, with
   'REGION' in out_domain -> -1) is semantically identical to the table above.
   The quantity is the SDAC-neighbour net position (for RO an OLS against the
   all-borders scheduled exchanges gives ~1.00*HU + 1.00*BG with a ~ +349 MW
   constant offset, R^2 = 0.99), *not* an all-borders physical net export, so
   no A09 sign override is ever applied.

This module is fully self-contained (xmlcore / mappings are built into
entsoe_ext); window chunking and normalisation live in
``EntsoeClient.net_position`` (the former ``query_net_position_corrected`` was
removed).
"""
import logging

import pandas as pd

from entsoe_ext.mappings import lookup_area
from entsoe_ext.series_ext import dedupe_by_index
from entsoe_ext.xmlcore import extract_timeseries, parse_timeseries_generic

logger = logging.getLogger("entsoe_ext")

# bs4 with html.parser lower-cases tag names (in_Domain.mRID -> in_domain.mrid)
_IN_TAGS = ("in_domain.mrid", "in_Domain.mRID")
_OUT_TAGS = ("out_domain.mrid", "out_Domain.mRID")


def _domain_text(soup, tags):
    for tag in tags:
        el = soup.find(tag)
        if el is not None:
            return el.text.strip()
    return None


def parse_a25(xml_text, area_code="RO", tz=None, strict_other=False):
    """Parse A25 raw XML -> net-position Series (magnitude x domain direction).

    Parameters
    ----------
    xml_text : str
        Raw A25 XML (the return value of ``EntsoeClient.net_position_xml``).
    area_code : str | Area
        Own country code or EIC (normalised to an EIC via ``lookup_area``).
    tz : str, optional
        Target timezone; when given, the UTC index is converted to it.
    strict_other : bool
        When True, raise if a TimeSeries has neither in/out domain equal to the
        own EIC (default: log a warning and exclude it).

    Returns
    -------
    pd.Series
        Ascending, unique index; an empty input yields an empty float64 Series.
    """
    eic = lookup_area(area_code).code
    parts = []
    other = 0
    for soup in extract_timeseries(xml_text):
        series = parse_timeseries_generic(soup, merge_series=True)
        if series is None or len(series) == 0:
            continue
        in_dom = _domain_text(soup, _IN_TAGS)
        out_dom = _domain_text(soup, _OUT_TAGS)
        if out_dom == eic:
            direction = 1        # own country exports (out == own EIC)
        elif in_dom == eic:
            direction = -1       # own country imports (in == own EIC)
        else:
            other += 1
            if strict_other:
                raise ValueError(
                    f"A25 TimeSeries with neither in/out == own EIC {eic}: "
                    f"in={in_dom} out={out_dom}")
            logger.warning(
                f"[A25] skipping non-own TimeSeries: in={in_dom} out={out_dom}")
            continue
        # A25 quantity is a magnitude; the sign comes from the domain (same as
        # the library's factor * abs(), written out explicitly here).
        parts.append(direction * series.abs())
    if other:
        logger.warning(
            f"[A25] skipped {other} non-own TimeSeries in total "
            f"(does not affect the own net position)")
    if not parts:
        return pd.Series(dtype="float64")
    out = pd.concat(parts).sort_index()
    out = dedupe_by_index(out, keep="last")
    if tz is not None:
        out.index = out.index.tz_convert(tz)
    return out
