# -*- coding: utf-8 -*-
"""entsoe_ext XML parsing kernel (replicates entsoe-py 0.8.1 series_parsers.py).

Key points (value-equivalent to the library; two deliberate hardenings marked):
- bs4 with html.parser: tag names are lower-cased (in_Domain.mRID ->
  in_domain.mrid); XMLParsedAsHTMLWarning is suppressed at module level (same as
  the library's parsers.py:14).
- ``parse_timeseries_generic``: buckets points by <resolution> (15min/30min/60min
  preset + dynamically added buckets); position aligns to
  ``start + (pos-1)*delta``; for curveType=A03 missing positions are rebuilt with
  ``reindex(date_range(start, end-delta)).ffill()`` (CIM A03 repeat semantics).
- resolution -> pandas frequency string reuses
  ``series_ext.resolution_to_timedelta_ext`` (a strict superset of the library's
  8 hard-coded values, identical for every known value; unknown values raise
  ValueError instead of the library's NotImplementedError).
- Hardening (1): a TimeSeries without any <Period> returns an empty Series
  (the library raises ValueError from ``pd.concat([None, None, None])`` on the
  merge_series=True path).
- Hardening (2): no ffill when curveType is missing (the library's
  ``soup.find('curvetype').text`` raises AttributeError; documents always carry
  curveType, so this branch is defensive only).

The library's ``_parse_datetimeindex`` is only referenced by dead code in 0.8.1
(all active parsing paths go through generic) and is not replicated here.
"""
import warnings

import bs4
import pandas as pd
from bs4.builder import XMLParsedAsHTMLWarning
from pandas.tseries.frequencies import to_offset

from entsoe_ext.series_ext import resolution_to_timedelta_ext

__all__ = ["extract_timeseries", "parse_timeseries_generic",
           "parse_timeseries_generic_whole"]

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def extract_timeseries(xml_text):
    """Yield the bs4 Tag of each <TimeSeries> (html.parser, lower-cased tags).

    Parameters
    ----------
    xml_text : str

    Yields
    ------
    bs4.element.Tag
    """
    if not xml_text:
        return
    soup = bs4.BeautifulSoup(xml_text, "html.parser")
    for timeseries in soup.find_all("timeseries"):
        yield timeseries


def parse_timeseries_generic(soup, label="quantity", to_float=True,
                             merge_series=False, period_name="period"):
    """Parse every <Period> of a single TimeSeries.

    Parameters
    ----------
    soup : bs4.element.Tag
        A single TimeSeries block.
    label : str
        Value tag inside each point ('quantity' / 'price.amount' /
        'imbalance_price.amount' ...).
    to_float : bool
        When True, strip thousands separators and cast position/value to float.
    merge_series : bool
        True -> concat all resolution buckets into a single Series (for
        endpoints without duplicate timestamps); False -> return a
        {frequency string: Series | None} dict (used by parse_prices).
    period_name : str
        Period tag name (default 'period').

    Returns
    -------
    pd.Series | dict
    """
    series = {
        "15min": [],
        "30min": [],
        "60min": [],
    }

    for period in soup.find_all(period_name):
        data = {}
        start = pd.Timestamp(period.find("start").text)
        end = pd.Timestamp(period.find("end").text)
        delta_text = resolution_to_timedelta_ext(period.find("resolution").text)
        delta = to_offset(delta_text)
        for point in period.find_all("point"):
            position = point.find("position").text
            value = point.find(label).text
            if to_float:
                position = position.replace(",", "")
                value = value.replace(",", "")
            position = int(position)
            data[start + (position - 1) * delta] = value
        S = pd.Series(data).sort_index()
        ct = soup.find("curvetype")
        if ct is not None and ct.text == "A03":
            # A03 allows missing positions (value-repeat semantics): rebuild the
            # continuous grid and forward-fill.
            S = S.reindex(pd.date_range(start, end - delta, freq=delta_text)).ffill()
        if delta_text not in series:
            series[delta_text] = []
        series[delta_text].append(S)
    for freq, S in series.items():
        if len(S) > 0:
            series[freq] = pd.concat(S).sort_index()
            if to_float:
                series[freq] = series[freq].astype(float)
        else:
            series[freq] = None

    if merge_series:
        # Library semantics: pd.concat drops the None buckets; when every bucket
        # is None (no Period at all) the library raises ValueError — hardened
        # here to an empty Series.
        if all(s is None for s in series.values()):
            return pd.Series(dtype="object")
        return pd.concat([s for s in series.values() if s is not None])
    return series


def parse_timeseries_generic_whole(xml_text, label="quantity", to_float=True):
    """Merge every TimeSeries, then concat + sort (single-value endpoints:
    A11 / A61 / A31 / A72 ...)."""
    series_all = []
    for soup in extract_timeseries(xml_text):
        series_all.append(
            parse_timeseries_generic(soup, label=label, to_float=to_float,
                                     merge_series=True))
    if not series_all:
        return pd.Series()
    series_all = pd.concat(series_all).sort_index()
    return series_all
