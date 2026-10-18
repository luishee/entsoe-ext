# -*- coding: utf-8 -*-
"""Single client entry point: :func:`get_client`.

Use this to construct any ``EntsoePandasClient`` / ``EntsoeRawClient`` so that
:func:`entsoe_ext.patch.install` (the version-guarded library patches) is always
applied first — callers cannot accidentally bypass it.
"""
import logging
import os

from entsoe_ext.patch import install

logger = logging.getLogger("entsoe_ext")

_TIMEOUT = 90       # seconds
_RETRY_COUNT = 1
_RETRY_DELAY = 5

_CLIENTS = {"pandas": "EntsoePandasClient", "raw": "EntsoeRawClient"}


def get_client(kind="pandas", api_key=None, **kwargs):
    """Construct an ENTSO-E client (single entry point).

    Parameters
    ----------
    kind : {'pandas','raw'}
        ``pandas`` → ``EntsoePandasClient`` (high-level DataFrame/Series API);
        ``raw`` → ``EntsoeRawClient`` (returns raw XML text).
    api_key : str, optional
        Defaults to the ``ENTSOE_API_KEY`` environment variable.

    Returns
    -------
    EntsoePandasClient | EntsoeRawClient
    """
    install()
    import entsoe

    if kind not in _CLIENTS:
        raise ValueError(f"unknown client kind={kind!r}, choose from {sorted(_CLIENTS)}")
    key = api_key or os.getenv("ENTSOE_API_KEY")
    if not key:
        raise RuntimeError(
            "ENTSOE_API_KEY is not set (api_key argument / environment variable)")
    cls = getattr(entsoe, _CLIENTS[kind])
    client = cls(api_key=key, **kwargs)
    client.timeout = _TIMEOUT
    client.retry_count = _RETRY_COUNT
    client.retry_delay = _RETRY_DELAY
    return client