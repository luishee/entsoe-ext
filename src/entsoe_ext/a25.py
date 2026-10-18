# -*- coding: utf-8 -*-
"""A25 (day-ahead net position) parsing: explicit domain-direction, single entry point.

**Why this exists (verified against raw A25 XML):**

1. Every ``<quantity>`` in the A25 ``TimeSeries`` is a **magnitude (>= 0)**; the
   direction is encoded 100% by ``in_Domain.mRID`` / ``out_Domain.mRID``:

   * ``out_Domain`` == own EIC → export (positive)
   * ``in_Domain``  == own EIC → import (negative)

2. ``entsoe-py``'s ``parsers.parse_netpositions`` applies
   ``factor = -1 if 'REGION' in out_domain else +1`` and then ``factor * series.abs()``.
   That is **semantically identical** to the table above — the library does **not**
   fabricate a sign flip. :func:`parse_a25` reproduces the same semantics *explicitly*
   (reusing the library's private ``_parse_timeseries_generic`` for ``<position>``
   alignment / multi-``<Period>`` merging / A03 gap filling), and never rewrites the
   sign.

3. Model note (case study, Romania): A25 is the **SDAC-neighbour net position**, not
   the "all-borders physical net export". An OLS against the direction-explicit
   ``scheduled_exchanges`` (all borders) gives, for the 15-min era:

       A25 ≈ 1.000*HU + 0.996*BG + 0.04*RS + 0.12*UA + 0.11*MD + 323.8   (R^2 = 0.9889)

   i.e. ``A25 ≈ (HU + BG) + ~ +349 MW`` constant offset. This explains why an
   "A25 vs all-borders" comparison shows only ~87% sign agreement — a pure
   mathematical consequence of the offset, **not** a parsing bug. Do not overwrite
   A25's sign with an all-borders series; they are not the same quantity.
"""
import logging

import pandas as pd

from entsoe.mappings import lookup_area
from entsoe.series_parsers import _extract_timeseries, _parse_timeseries_generic

from entsoe_ext.series_ext import dedupe_by_index, resample_sdac_native

logger = logging.getLogger("entsoe_ext")

# bs4 with the html.parser lower-cases tag names (in_Domain.mRID -> in_domain.mrid)
_IN_TAGS = ("in_domain.mrid", "in_Domain.mRID")
_OUT_TAGS = ("out_domain.mrid", "out_Domain.mRID")


def _domain_text(soup, tags):
    for tag in tags:
        el = soup.find(tag)
        if el is not None:
            return el.text.strip()
    return None


def parse_a25(xml_text, area_code="RO", tz=None, strict_other=False):
    """Parse A25 raw XML → net-position Series (magnitude × domain direction).

    Parameters
    ----------
    xml_text : str
        Raw A25 XML (the return value of ``EntsoeRawClient.query_net_position``).
    area_code : str
        Own country code or EIC (normalised to an EIC via ``lookup_area``).
    tz : str, optional
        Target timezone; if given, the UTC index is converted to it.
    strict_other : bool
        When True, raise if a ``TimeSeries`` has neither ``in_Domain`` nor
        ``out_Domain`` equal to the own EIC (default: log a warning and skip).

    Returns
    -------
    pandas.Series
        Ascending, unique index. An empty input yields an empty float64 Series.
    """
    eic = lookup_area(area_code).code
    parts = []
    other = 0
    for soup in _extract_timeseries(xml_text):
        series = _parse_timeseries_generic(soup, merge_series=True)
        if series is None or len(series) == 0:
            continue
        in_dom = _domain_text(soup, _IN_TAGS)
        out_dom = _domain_text(soup, _OUT_TAGS)
        if out_dom == eic:
            direction = 1        # own country is the exporter (out == own EIC)
        elif in_dom == eic:
            direction = -1       # own country is the importer (in == own EIC)
        else:
            other += 1
            if strict_other:
                raise ValueError(
                    f"A25 TimeSeries with neither in/out == own EIC {eic}: "
                    f"in={in_dom} out={out_dom}")
            logger.warning(
                f"[A25] skipping non-own TimeSeries: in={in_dom} out={out_dom}")
            continue
        # A25 quantity is a magnitude; the sign comes from the domain (same as the
        # library's factor * abs(), written out explicitly here).
        parts.append(direction * series.abs())
    if other:
        logger.warning(
            f"[A25] skipped {other} non-own TimeSeries (does not affect own net position)")
    if not parts:
        return pd.Series(dtype="float64")
    out = pd.concat(parts).sort_index()
    out = dedupe_by_index(out, keep="last")
    if tz is not None:
        out.index = out.index.tz_convert(tz)
    return out


def query_net_position_corrected(client, area_code="RO", start=None, end=None,
                                 dayahead=True, tz=None, golive=None):
    """Fetch A25 net position end-to-end (parse → resample → tz → truncate).

    Equivalent to ``EntsoePandasClient.query_net_position`` with two differences:

    * parsing goes through :func:`parse_a25` (explicit domain direction, non-own
      domains logged, index de-duplicated);
    * before the SDAC go-live it uses :func:`resample_sdac_native` (mean) instead of
      the library's ``resample('h').first()``.

    Parameters
    ----------
    client : EntsoeRawClient | EntsoePandasClient
        Either works — raw XML is always fetched via
        ``EntsoeRawClient.query_net_position`` to avoid losing information that the
        PandasClient wrapper would strip.

    Window splitting (2026-10-18, entsoe-py 0.8.1): ``EntsoeRawClient``
    methods carry **no** ``limited`` decorator, so long windows are never split
    by the library. Empirically (2026-10-18, binary search on the RO A25
    endpoint): the platform's window limit for A25 is **365 days** (366 days
    returns HTTP 400) — entsoe-py 0.8.1 switching the PandasClient
    ``query_net_position`` to ``@month_limited`` is a defensive tightening (the
    platform does not enforce one month on A25). This function therefore splits
    windows at a **365-day** cap (stepping 365 days from ``start``; the
    library's ``year_blocks`` is not used because its calendar-year blocks can
    reach 366 days across a leap year — empirically rejected). Windows of up to
    one year stay a **single call** (identical to the 0.8.0 behaviour, no extra
    request quota); longer windows are fetched block by block and merged, and
    the first instant of every block is kept (the 0.8.0 ``@year_limited``
    left-open truncation, which silently dropped it, is not replicated).
    """
    from entsoe.entsoe import EntsoeRawClient
    from entsoe.exceptions import NoMatchingDataError

    area = lookup_area(area_code)

    # left-closed/right-open blocks of at most 365 days (measured platform cap)
    blocks = []
    if start is not None and end is not None and start < end:
        _cur = start
        while _cur < end:
            _nxt = min(_cur + pd.Timedelta(days=365), end)
            blocks.append((_cur, _nxt))
            _cur = _nxt
    else:
        # Same as the library: inverted/zero-length windows fall through to the
        # underlying client, which raises the appropriate error.
        blocks = [(start, end)]

    parts = []
    for _start, _end in blocks:
        try:
            text = EntsoeRawClient.query_net_position(
                client, area_code, start=_start, end=_end, dayahead=dayahead)
            series = parse_a25(text, area_code=area_code)
        except NoMatchingDataError:
            # No published data in this block: skip (NoMatchingDataError is only
            # raised below when every block is empty — same as @month_limited).
            continue
        if len(series):
            parts.append(series)
    if not parts:
        raise NoMatchingDataError
    series = pd.concat(parts) if len(parts) > 1 else parts[0]
    series = dedupe_by_index(series, keep="last")

    if dayahead:
        series = resample_sdac_native(series, golive=golive)
    target_tz = tz or area.tz
    series = series.tz_convert(target_tz).sort_index()
    series = series.truncate(before=start, after=end)
    if len(series) == 0:
        raise NoMatchingDataError
    return series