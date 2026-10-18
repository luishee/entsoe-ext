# -*- coding: utf-8 -*-
"""Runtime patches for entsoe-py 0.8.0 (idempotent, opt-out, version-guarded).

Only the **low-risk, signature-compatible** spots of the library are patched; the
A25 sign is left untouched (the library logic is already correct — see
:mod:`entsoe_ext.a25`).

Patched:

* ``series_parsers._resolution_to_timedelta`` / ``parsers._resolution_to_timedelta``
  → :func:`entsoe_ext.series_ext.resolution_to_timedelta_ext` (unknown resolutions no
  longer raise ``NotImplementedError``);
* ``EntsoePandasClient.query_physical_crossborder_allborders`` → wrapper that, for
  ``per_hour=True``, fetches the native resolution and takes the hourly **mean**
  (the docstring says mean; 0.8.0 actually used ``first``).

Opt out with the environment variable ``ENTSOE_EXT_PATCH=0``; if the installed
``entsoe-py`` version differs from the validated one, ``install()`` is a no-op
(unless ``force=True``).
"""
import logging
import os

from entsoe_ext.series_ext import resolution_to_timedelta_ext, to_hourly_mean

logger = logging.getLogger("entsoe_ext")

_EXPECTED_VERSION = "0.8.0"


def _patch_allborders(EntsoePandasClient):
    """Wrap ``query_physical_crossborder_allborders``: ``per_hour`` → mean."""
    if getattr(EntsoePandasClient.query_physical_crossborder_allborders,
               "_ents_ext_patched", False):
        return
    orig = EntsoePandasClient.query_physical_crossborder_allborders

    def _patched(self, *args, per_hour=False, **kwargs):
        # Always fetch the native resolution (per_hour=False), then resample to the
        # hourly mean → matches the docstring.
        df = orig(self, *args, per_hour=False, **kwargs)
        return to_hourly_mean(df) if per_hour else df

    _patched._ents_ext_patched = True
    EntsoePandasClient.query_physical_crossborder_allborders = _patched


def install(force=False):
    """Install the library patches (idempotent). Returns True if installed/already latest.

    Version guard: patches are only applied when ``entsoe.__version__ == 0.8.0``
    (other versions may have changed behaviour — skipped to avoid surprises); setting
    ``ENTSOE_EXT_PATCH=0`` skips entirely.
    """
    if os.getenv("ENTSOE_EXT_PATCH", "1") == "0":
        logger.info("[entsoe_ext] ENTSOE_EXT_PATCH=0, skipping library patches")
        return False
    import entsoe
    ver = getattr(entsoe, "__version__", None)
    if ver != _EXPECTED_VERSION and not force:
        logger.warning(
            f"[entsoe_ext] entsoe-py version {ver!r} != {_EXPECTED_VERSION}, "
            f"skipping library patches (force with install(force=True))")
        return False
    import entsoe.parsers as P
    import entsoe.series_parsers as SP
    import entsoe.entsoe as E

    if getattr(SP, "_ENTS_EXT_PATCHED", False):
        return True

    SP._resolution_to_timedelta = resolution_to_timedelta_ext
    P._resolution_to_timedelta = resolution_to_timedelta_ext
    _patch_allborders(E.EntsoePandasClient)

    SP._ENTS_EXT_PATCHED = True
    logger.info("[entsoe_ext] library patches installed "
                "(resolution fallback / per_hour mean)")
    return True