# -*- coding: utf-8 -*-
"""entsoe_ext chunking / pagination (replicates entsoe-py 0.8.1
decorators.py + misc.py semantics; functional implementation orchestrated by
client.py). Two deliberate differences from the library (both verified fixes):

1. **365-day year blocks**: the library's ``year_blocks`` steps one calendar year
   at a time, so a block spanning a leap year reaches 366 days -> HTTP 400 from
   the platform (measured; see a25.py); this implementation steps 365 days.
2. **Block-first instant kept**: the library's ``year_limited`` left-open
   truncation drops the first instant of every non-first block (silently losing
   each year's first point); blocks are not truncated here, and duplicates at
   block boundaries are collapsed by the caller (dedupe keep-last).

Everything else is equivalent: month blocks (DateOffset(months=1) step, no
truncation), documents pagination (offset 0..4800 step n, stop on
NoMatchingDataError, groupby-ffill-last dedupe), paginate (PaginationError ->
binary window split).
"""
import logging
from typing import Callable, List, Tuple

import pandas as pd

from entsoe_ext.exceptions import NoMatchingDataError, PaginationError

__all__ = ["iter_year_blocks", "iter_month_blocks", "fetch_in_blocks",
           "fetch_documents", "paginate"]

logger = logging.getLogger("entsoe_ext")

_DOCUMENTS_OFFSET_HARD_LIMIT = 4800


def iter_year_blocks(start: pd.Timestamp, end: pd.Timestamp) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """Left-closed/right-open 365-day blocks (a <=1-year window stays a single
    block; adjacent blocks share their boundary instant)."""
    blocks = []
    cur = start
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=365), end)
        blocks.append((cur, nxt))
        cur = nxt
    return blocks


def iter_month_blocks(start: pd.Timestamp, end: pd.Timestamp) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """Month blocks (replicates the library's misc.month_blocks: UTC
    normalisation + DateOffset(months=1) stepping)."""
    start_ts = pd.to_datetime(start, utc=True)
    end_ts = pd.to_datetime(end, utc=True)
    if start_ts >= end_ts:
        return []
    blocks = []
    current_start = start_ts
    while current_start < end_ts:
        next_end = min(current_start + pd.DateOffset(months=1), end_ts)
        blocks.append((current_start, next_end))
        current_start = next_end
    return blocks


def fetch_in_blocks(fetch_fn: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame],
                    blocks) -> pd.DataFrame:
    """Call ``fetch_fn(_start, _end)`` per block; a block raising
    NoMatchingDataError is skipped; all blocks empty -> raise
    NoMatchingDataError; results are concatenated (sorted or not per pandas
    defaults)."""
    frames = []
    for _start, _end in blocks:
        try:
            frames.append(fetch_fn(_start, _end))
        except NoMatchingDataError:
            logger.debug(f"NoMatchingDataError: between {_start} and {_end}")
    if not frames or all(f is None for f in frames):
        raise NoMatchingDataError
    return pd.concat([f for f in frames if f is not None])


def _deduplicate_documents_limited(group: pd.DataFrame) -> pd.DataFrame:
    if group.shape[0] == 1:
        return group
    return group.ffill().iloc[[-1]]


def fetch_documents(fetch_fn: Callable[..., pd.DataFrame], n: int,
                    dedupe: bool = True, **kwargs) -> pd.DataFrame:
    """offset pagination (replicates the library's documents_limited).

    ``fetch_fn(offset=..., **kwargs)`` is called page by page; offset in
    range(0, 4800+n, n); NoMatchingDataError on one page -> stop (later pages
    hold no documents); everything empty -> NoMatchingDataError.

    Parameters
    ----------
    dedupe : bool
        True (default): duplicate index entries are collapsed to the last valid
        value (groupby-ffill-last); False for unavailability, which the library
        exempts from dedupe (document revisions must stay separate rows).
    """
    frames = []
    for offset in range(0, _DOCUMENTS_OFFSET_HARD_LIMIT + n, n):
        try:
            frames.append(fetch_fn(offset=offset, **kwargs))
        except NoMatchingDataError:
            logger.debug(f"NoMatchingDataError: for offset {offset}")
            break
    if not frames:
        raise NoMatchingDataError
    valid = [f for f in frames if not f.empty and not f.isna().all().all()]
    if not valid:
        # The library raises ValueError from pd.concat([]) here — hardened to
        # the semantically correct empty-data exception.
        raise NoMatchingDataError
    df = pd.concat(valid, sort=True)
    if dedupe and df.index.has_duplicates:
        df = df.groupby(df.index).agg(_deduplicate_documents_limited)
    return df


def paginate(fetch_fn: Callable, start: pd.Timestamp, end: pd.Timestamp,
             **kwargs) -> pd.DataFrame:
    """PaginationError -> binary window split (replicates the library's
    @paginated)."""
    try:
        return fetch_fn(start=start, end=end, **kwargs)
    except PaginationError:
        pivot = start + (end - start) / 2
        df1 = paginate(fetch_fn, start=start, end=pivot, **kwargs)
        df2 = paginate(fetch_fn, start=pivot, end=end, **kwargs)
        return pd.concat([df1, df2])
