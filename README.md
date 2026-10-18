# entsoe-ext

Opt-in fixes and explicit wrappers around **[entsoe-py](https://github.com/EnergieID/entsoe-py) 0.8.0**.

`entsoe-ext` is a thin, dependency-light layer that sits *on top of* the installed
`entsoe-py` package (it does **not** vendor or replace it). It addresses a small set
of "uncertain / TODO" spots in 0.8.0 and provides a **single client entry point** so
that callers cannot accidentally bypass the corrections.

> Scope note — this package is deliberately **opt-in and minimal**. It fixes only what
> is demonstrably wrong or ambiguous, and it does not change signals that are already
> correct. See the table below.

## What it does (and does not)

| # | entsoe-py 0.8.0 spot | Disposition in `entsoe-ext` |
|---|---|---|
| 1 | `series_parsers._resolution_to_timedelta` raises `NotImplementedError` on unknown resolutions (hard-coded 8 values, comment: *"Everything is hard coded"*) | **Fixed** → `resolution_to_timedelta_ext()` with a generic ISO-8601 fallback; raises a descriptive `ValueError` instead |
| 2 | `EntsoePandasClient.query_physical_crossborder_allborders(per_hour=True)` docstring says *"it will then take the mean"* but actually takes `resample('h').first()` | **Fixed** → `per_hour=True` now truly returns the hourly **mean** (via `to_hourly_mean`) |
| 3 | `query_net_position(dayahead=True)` uses `resample('h').first()` before the SDAC 15-min go-live | **Explicit wrapper** → `resample_sdac_native()` (`mean` before go-live, native resolution after). This is a **convention choice**, not a correctness fix |
| 4 | `parse_netpositions` implements `factor * series.abs()` to assign a sign per `TimeSeries` | **Not a bug** → verified against raw A25 XML: `<quantity>` is always a magnitude and the direction is fully encoded by `in_Domain`/`out_Domain`; the library logic matches. `parse_a25()` re-implements it *explicitly* (same output) and never rewrites the sign |
| 5 | `query_offered_capacity` timezone docstring *"--> to check"* | **No change** — the library already performs `tz_convert` |
| 6 | `@year_limited` decorator comment *"Assumes blocks are sorted"* | **Mitigated** → `dedupe_by_index()` to guarantee a unique, sorted index after merging chunks |

If you only care about the genuine general-purpose fixes, #1 and #2 are the ones worth
upstreaming to `entsoe-py` itself.

## Install

```bash
pip install entsoe-ext            # from PyPI (once published)
# or, directly from GitHub:
pip install "git+https://github.com/luishee/entsoe-ext.git"
```

Requires `entsoe-py==0.8.0` (pinned; see *Compatibility* below). Set your API key:

```bash
export ENTSOE_API_KEY="your-token"   # PowerShell: $env:ENTSOE_API_KEY="..."
```

## Usage

### Single client entry point

```python
from entsoe_ext import get_client

client = get_client("pandas")   # or "raw"; api_key=... overrides the env var
```

`get_client()` calls `install()` first (see below) and returns a configured
`EntsoePandasClient` / `EntsoeRawClient`.

### Apply the library patches

```python
from entsoe_ext import install
install()          # idempotent, version-guarded
```

Disable entirely with the environment variable `ENTSOE_EXT_PATCH=0`.

Applied patches:

* `entsoe.series_parsers._resolution_to_timedelta` / `entsoe.parsers._resolution_to_timedelta`
  → `resolution_to_timedelta_ext`
* `EntsoePandasClient.query_physical_crossborder_allborders` → `per_hour=True` uses the mean

### Standalone helpers

```python
from entsoe_ext import (
    resolution_to_timedelta_ext,   # "PT45M" -> "45min" (ISO-8601 fallback)
    resample_sdac_native,          # mean before SDAC go-live, native after
    to_hourly_mean,                # resample('h').mean()
    dedupe_by_index,               # unique, sorted index after merging chunks
)
```

### Net position (A25), explicit parsing

```python
from entsoe_ext import get_client, parse_a25, query_net_position_corrected

raw = get_client("raw")

# From raw XML (direction decided explicitly by in/out_Domain):
series = parse_a25(xml_text, area_code="RO", tz="Europe/Bucharest")

# Or end-to-end (fetch -> parse -> resample -> tz -> truncate):
series = query_net_position_corrected(
    raw, "RO",
    start=pd.Timestamp("2026-09-15", tz="Europe/Bucharest"),
    end=pd.Timestamp("2026-09-16", tz="Europe/Bucharest"),
)
```

`query_net_position_corrected()` output is **value-identical** to
`EntsoePandasClient.query_net_position` for the areas tested (RO); the only
differences are the explicit domain-direction parsing and the `mean` (instead of
`first`) hourly aggregation before the SDAC go-live.

## Compatibility

`entsoe-ext` wraps a couple of **private** helpers of `entsoe-py`
(`entsoe.series_parsers._extract_timeseries`, `_parse_timeseries_generic`) and patches
its internals, so it is validated against **`entsoe-py` 0.8.0 and 0.8.1**. `install()` is
a no-op (with a warning) on any other version. If you upgrade `entsoe-py`, re-check this
package — the upstream fixes may already be present, in which case the wrappers can be
dropped.

> 0.8.1 note: 0.8.1 only swaps several endpoint decorators from `@year_limited` to the
> new `@month_limited` (an ENTSO-E platform-side change: single queries on those
> endpoints are now limited to ≤ 1 month). The files that this package patches or
> reuses (`parsers.py`, `series_parsers.py`) are **byte-identical** between 0.8.0 and
> 0.8.1, and neither patched defect is fixed upstream, so the patches apply unchanged.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License & attribution

MIT — see [LICENSE](LICENSE). This project depends on and interoperates with
[entsoe-py](https://github.com/EnergieID/entsoe-py) (MIT). All credits for the
underlying Transparency Platform client go to the entsoe-py authors.