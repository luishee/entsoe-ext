# entsoe-ext

A **self-contained** client for the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/).

Since **0.2.0** this package no longer wraps or patches
[entsoe-py](https://github.com/EnergieID/entsoe-py): it implements its own HTTP
layer, chunking/pagination, exception hierarchy, area/type mappings and document
parsers, replicating entsoe-py **0.8.1** semantics value-for-value (validated by
a three-layer A/B equivalence suite during the migration). `entsoe-py` is **not**
a runtime dependency.

> **Breaking changes in 0.2.0** (upgrading from the 0.1.x wrapper line):
> `get_client()` now returns the self-built `EntsoeClient` (no more
> `kind="pandas"/"raw"` two-state client), the `patch` module is gone, and
> `query_net_position_corrected()` was absorbed into
> `EntsoeClient.net_position()`. The A25 helpers (`parse_a25`, series helpers)
> keep working.

## Install

```bash
pip install entsoe-ext            # from PyPI (once published)
# or, directly from GitHub:
pip install "git+https://github.com/luishee/entsoe-ext.git"
```

Requires Python >= 3.9; dependencies are `requests`, `beautifulsoup4` and
`pandas`. Set your API key:

```bash
export ENTSOE_API_KEY="your-token"   # PowerShell: $env:ENTSOE_API_KEY="..."
```

## API access and documentation

The Transparency Platform REST interface is token-based: every request carries
a `securityToken` that ENTSO-E issues per account.

1. Create an account on the [Transparency Platform](https://transparency.entsoe.eu/).
2. From the email address used at registration, write to
   `transparency@entsoe.eu` — subject `Restful API access`, and state that same
   registered address in the body — to request API access.
3. Once access is granted, generate your security token in the account
   settings on the platform.
4. Provide the token to this package via `get_client(api_key="...")`, or
   export it as `ENTSOE_API_KEY` (see [Install](#install)).

ENTSO-E is the association of Europe's electricity transmission system
operators, and its Transparency Platform is the source of every dataset this
client fetches. Reference material for the underlying REST interface:

- Postman collection (endpoint reference): <https://documenter.getpostman.com/view/7009892/2s93JtP3F6>
- Static Web API guide on the platform: <https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html>

## Usage

### Single client entry point

```python
from entsoe_ext import get_client

client = get_client()          # api_key=... overrides the env var
```

`EntsoeClient` exposes two channels: raw XML methods (`*_xml`) and parsed data
methods (Series/DataFrame).

```python
s  = client.net_position("RO", start=..., end=...)   # A25 day-ahead net position
df = client.generation("RO", start=..., end=...)     # A75 per-psr generation
xml = client.net_position_xml("RO", start, end)      # raw XML (audit / custom parsing)
```

### Data methods

| method | document | returns | index timezone |
|---|---|---|---|
| `day_ahead_prices(a, s, e)` | A44 | Series | area.tz |
| `net_position(a, s, e, dayahead=True)` | A25 | Series | area.tz |
| `load(a, s, e)` / `load_forecast(a, s, e)` | A65 | Series | area.tz |
| `generation(a, s, e)` | A75 | DataFrame (psr or MultiIndex(psr, metric)) | area.tz |
| `generation_forecast(a, s, e)` | A71 | Series (always) | area.tz |
| `wind_solar_forecast(a, s, e)` | A69 | DataFrame (netted) | area.tz |
| `installed_capacity(a, year)` | A68 | DataFrame | area.tz |
| `imbalance_prices(a, s, e)` | A85 | DataFrame (Long/Short) | area.tz |
| `imbalance_volumes(a, s, e)` | A86 | Series | area.tz |
| `activated_balancing_prices(a, s, e)` | A84 | DataFrame (Price/Direction/ReserveType) | area.tz |
| `procured_capacity(a, process_type, s, e)` | A15 | **long table** `[timestamp, direction, mrid, price, volume]` | area.tz |
| `hydro_reservoir_storage(a, s, e)` | A72 | Series | native (not converted) |
| `crossborder_flows(f, t, s, e)` | A11 | Series | from-area tz |
| `scheduled_exchanges(f, t, s, e)` | A09 | Series | from-area tz |
| `ntc_dayahead(f, t, s, e)` | A61 | Series | from-area tz |
| `offered_capacity(f, t, s, e, contract="A01")` | A31/A25 | Series | from-area tz |
| `outage_units(a, s, e, kind="generation")` | A80/A77 | DataFrame (19 cols) | area.tz |
| `outage_transmission(f, t, s, e)` | A78 | DataFrame (14 cols) | from-area tz |

Raw XML channels exist for every endpoint above (`*_xml` suffix); ZIP endpoints
additionally expose `*_xml` variants that unzip and join the payloads to a
string, for auditing.

### Series helpers and exceptions

```python
from entsoe_ext import (
    resolution_to_timedelta_ext,   # "PT45M" -> "45min" (ISO-8601 fallback)
    resample_sdac_native,          # mean before the SDAC go-live, native after
    sdac_golive,                   # 2025-10-01 Europe/Amsterdam (local constant)
    to_hourly_mean,                # resample('h').mean()
    dedupe_by_index,               # unique, sorted index after merging chunks
    NoMatchingDataError, PaginationError, InvalidBusinessParameterError,
    InvalidPSRTypeError, InvalidParameterError,
)
```

## Deliberate differences from entsoe-py

Everything else replicates 0.8.1 value-for-value; these are the intentional
differences (all verified against the live platform):

1. **365-day year blocks** — the library's calendar-year stepping reaches 366
   days across a leap year, which the platform rejects with HTTP 400 (measured);
   this client steps 365 days.
2. **Block-first instant kept** — the library's `@year_limited` left-open
   truncation silently drops the first instant of every non-first block; here
   duplicates at block boundaries are collapsed by dedupe keep-last (identical
   values, lossless).
3. **`procured_capacity` returns a long table** `[timestamp, direction, mrid,
   price, volume]` (the library's 3-level MultiIndex concatenates days with NaN
   misalignment).
4. **`load` / `load_forecast` / `generation_forecast` always return a Series**
   (removes the dual-shape behaviour for callers).
5. **`offered_capacity` takes `contract=`** (was `contract_marketagreement_type`).
6. **Month-block chunking follows 0.8.1 `@month_limited`** (load / generation /
   cross-border families ...). Two exceptions: `ntc_dayahead` is still
   `@year_limited` in 0.8.1 while this client uses uniform month blocks
   (fully equivalent for <= 1-month windows); `net_position` uses the 365-day
   blocks from `query_net_position_corrected` (neither drops a block's first
   instant — identical point set, different request granularity).
7. **Security-token masking** — any re-raised `requests.HTTPError` is rebuilt
   with the `securityToken` value replaced by `<hidden>`, and every mapped
   error raises with `from None`, so the raw URL (which embeds the plain API
   key) never reaches tracebacks, error messages or logs. Adopted from
   entsoe-py master (commit `411e4f7`, ahead of the 0.8.1 release this
   package replicates); the response and request objects are preserved on
   the rebuilt error.

## Window handling (0.2.0)

`EntsoeClient` caps long windows itself where the raw client has no `limited`
decorator of its own:

* `net_position()` splits windows at a measured **365-day** cap (binary-searched
  against the live A25 endpoint: 365 d OK, 366 d HTTP 400 — the platform does
  *not* enforce the one-month limit that 0.8.1's `@month_limited` anticipates).
  Windows up to one year stay a **single call** (identical to 0.8.0 behaviour,
  no extra quota); longer windows are fetched block by block (left-closed /
  right-open), merged with `dedupe_by_index`, and the **first instant of every
  block is kept**; `NoMatchingDataError` is raised only when *every* block is
  empty.
* A44 day-ahead prices keep the library's +-1 day outer padding and page with
  `offset` steps of 100.

## Parsing caveats replicated on purpose

* **A84** reads only the first `<Period>` of each `TimeSeries` (verbatim from the
  library; multi-Period revision segments are truncated).
* **A84 / A15 output is not deduplicated** — the source may return multiple
  documents for the same key (original + revisions); consumers collapse
  duplicates themselves (`drop_duplicates(keep="last")`).
* **Outages** keep every document revision as a separate row (dedupe exempt,
  like the library) and filter the window with an OR condition.
* **A72 hydro** keeps the native index (no tz conversion, no truncation).

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite is fully offline (constructed XML/ZIP fixtures, fake transport
sessions); it covers the parsers, the client's error mapping/retry/params
casing, the 365-day window split and the A25 domain-direction parsing.

## Changelog

- **0.2.1** — Security hardening and documentation accuracy:
  - Security-token masking at the error-raising source (adopted from
    entsoe-py master `411e4f7`): re-raised `requests.HTTPError` messages no
    longer embed the plain API key, and mapped errors raise with `from None`
    so suppressed chains cannot leak it either. `error.response` /
    `error.request` are preserved.
  - Fixed the `procured_capacity` document-type label in docs/docstrings:
    the endpoint queries **A15** (procured balancing capacity), not A81
    (contracted reserves); the query itself was always correct.
- **0.2.0** — Self-built client replacing the entsoe-py wrapper (see
  the breaking-changes note at the top).
- **0.1.x** — Wrapper/patch line around entsoe-py 0.8.1.

## License & attribution

MIT — see [LICENSE](LICENSE). The area/EIC mappings and the parsing semantics
replicate [entsoe-py](https://github.com/EnergieID/entsoe-py) (MIT License,
Copyright (c) EnergieID / entsoe-py contributors); all credits for the original
Transparency Platform client design go to its authors. This package neither
depends on nor patches entsoe-py at runtime.