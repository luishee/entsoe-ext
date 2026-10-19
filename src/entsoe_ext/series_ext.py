# -*- coding: utf-8 -*-
"""Series-level corrections for entsoe-py 0.8.0/0.8.1 (resolution / resample / dedupe).

(Historical module: the client itself is now self-built; this module remains as
entsoe_ext's series utility layer, used by ``client.py`` / ``a25.py``. The
defect descriptions below refer to entsoe-py 0.8.0 / 0.8.1.)

Corresponds to three "uncertain declaration / TODO" spots of entsoe-py 0.8.0:

1. ``series_parsers._resolution_to_timedelta`` raises ``NotImplementedError`` for
   unknown resolutions (hard-coded values, comment "Everything is hard coded")
   → :func:`resolution_to_timedelta_ext` (generic ISO-8601 fallback).
2. ``EntsoePandasClient.query_net_position(dayahead=True)`` uses
   ``resample('h').first()`` before the SDAC 15-min go-live
   (``QUARTER_MTU_SDAC_GOLIVE``) and drops the other native MTUs of the hour
   → :func:`resample_sdac_native` (mean before go-live, native after).
3. ``query_physical_crossborder_allborders(per_hour=True)`` documents "it will then
   take the mean" but actually calls ``resample('h').first()``
   → :func:`to_hourly_mean`.
"""
import logging
import re

import pandas as pd
from pandas.tseries.frequencies import to_offset

logger = logging.getLogger("entsoe_ext")

# The 8 values hard-coded in the library + common ISO-8601 duration equivalents
# (avoids the generic regex path).
KNOWN_RESOLUTIONS = {
    "PT1M": "1min", "PT5M": "5min", "PT15M": "15min", "PT30M": "30min",
    "PT60M": "60min", "PT1H": "1h",
    "P1D": "1D", "P1W": "7D", "P7D": "7D",
    "P1M": "1MS", "P3M": "3MS", "P6M": "6MS", "P1Y": "12MS",
}

_ISO_RE = re.compile(
    r"^P(?:(?P<y>\d+)Y)?(?:(?P<mo>\d+)M)?(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<mi>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def resolution_to_timedelta_ext(res_text: str) -> str:
    """resolution string → pandas frequency string (robust replacement for the
    library's ``_resolution_to_timedelta``).

    Returns a match from :data:`KNOWN_RESOLUTIONS` directly; otherwise parses the
    string as a generic ISO-8601 duration (calendar components Y/M map to the
    anchored frequencies ``MS`` / ``12MS``).

    Unlike the library, an unrecognised value raises a **``ValueError`` that
    includes the original text and the known set**, instead of a bare
    ``NotImplementedError``.
    """
    if not res_text:
        raise ValueError("resolution is empty")
    if res_text in KNOWN_RESOLUTIONS:
        return KNOWN_RESOLUTIONS[res_text]
    m = _ISO_RE.match(res_text.strip())
    if not m:
        raise ValueError(
            f"cannot parse resolution {res_text!r} (not an ISO-8601 duration); "
            f"known: {sorted(KNOWN_RESOLUTIONS)}")
    g = m.groupdict()
    y = int(g["y"] or 0)
    mo = int(g["mo"] or 0)
    w = int(g["w"] or 0)
    d = int(g["d"] or 0)
    h = int(g["h"] or 0)
    mi = int(g["mi"] or 0)
    s = float(g["s"] or 0)
    if y or mo:
        return f"{y * 12 + mo}MS"
    if w:
        return f"{w * 7}D"
    if d:
        return f"{d}D"
    td = pd.Timedelta(hours=h, minutes=mi, seconds=s)
    if td <= pd.Timedelta(0):
        raise ValueError(f"resolution {res_text!r} resolves to a zero/negative duration")
    return to_offset(td).freqstr


def dedupe_by_index(obj, keep: str = "last"):
    """Drop duplicate index entries (keeping ``keep``) and sort ascending.

    Works for both Series and DataFrame. Useful after merging multiple chunks /
    ``TimeSeries`` results, whose concatenation does not guarantee a unique index.
    """
    if obj is None or len(obj) == 0:
        return obj
    if not obj.index.is_unique:
        n = len(obj)
        obj = obj[~obj.index.duplicated(keep=keep)].sort_index()
        logger.warning(f"  [dedupe] index de-duplicated {n} -> {len(obj)} (keep={keep})")
    return obj


def to_hourly_mean(obj):
    """Resample to hourly **mean** (fixes the library's docstring-vs-``first`` gap)."""
    if obj is None or len(obj) == 0:
        return obj
    return obj.resample("h").mean()


def sdac_golive():
    """SDAC 15-min MTU go-live timestamp (local constant, same value as entsoe-py)."""
    return pd.Timestamp("2025-10-01", tz="Europe/Amsterdam")


def resample_sdac_native(series, golive=None):
    """Normalise the resolution around the SDAC go-live (fixes ``resample('h').first()``).

    * ``< golive``: ``resample('h').mean()`` (mean over the native MTUs of the hour;
      when there is only one MTU per hour, ``mean ≡ first``);
    * ``>= golive``: keep the native resolution (15 min).

    Keeps the library's "resample the whole pre-golive block" semantics (including
    the empty-segment / DST-boundary behaviour).
    """
    if series is None or len(series) == 0:
        return series
    if golive is None:
        golive = sdac_golive()
    if series.index.max() < golive:
        return series.resample("h").mean()
    before = series[series.index < golive]
    after = series[series.index >= golive]
    parts = [p for p in (before.resample("h").mean(), after) if len(p)]
    return pd.concat(parts).sort_index()