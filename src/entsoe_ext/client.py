# -*- coding: utf-8 -*-
"""entsoe_ext self-built ENTSO-E client (EntsoeClient).

Replaces the entsoe-py runtime dependency (the library served only as
reference). The HTTP layer / chunking / pagination / parsing semantics
replicate entsoe-py 0.8.1 (equivalence was validated by an A/B suite during
migration), with the following deliberate differences (all verified):

  1. 365-day year blocks (the library steps one calendar year, reaching 366
     days across a leap year -> HTTP 400 from the platform; measured);
  2. block-first instant not dropped (the library's @year_limited left-open
     truncation loses it; duplicates at block boundaries are collapsed by
     dedupe keep-last — identical values, lossless);
  3. procured_capacity returns a long table
     [timestamp, direction, mrid, price, volume] (the library's 3-level
     MultiIndex concatenates days with NaN misalignment);
  4. load / load_forecast / generation_forecast always return a Series
     (removes the dual-shape behaviour);
  5. offered_capacity takes ``contract=`` (was contract_marketagreement_type);
  6. month-block chunking matches the library's 0.8.1 @month_limited
     (load / generation / cross-border family ...; 0.8.0 used @year_limited —
     an A/B reference must use 0.8.1). Two exceptions: ntc_dayahead is still
     @year_limited in 0.8.1 while this client uses uniform month blocks
     (fully equivalent for <=1-month windows); net_position uses the 365-day
     blocks absorbed from query_net_position_corrected (also keeping every
     block's first instant — identical point set).

Timezone contract (same as the library): zone endpoints = area.tz;
cross-border family = the "from" area timezone; hydro = native index
(no conversion).
"""
import logging
import os
import socket
import time
from http.client import RemoteDisconnected
from typing import Dict, Optional, Union

import pandas as pd
import requests
from bs4 import BeautifulSoup

from entsoe_ext.a25 import parse_a25
from entsoe_ext import chunking
from entsoe_ext.exceptions import (InvalidBusinessParameterError,
                                   InvalidPSRTypeError, NoMatchingDataError,
                                   PaginationError)
from entsoe_ext.mappings import lookup_area
from entsoe_ext.parsers import (
    parse_activated_balancing_energy_prices,
    parse_crossborder_flows,
    parse_generation,
    parse_imbalance_prices_zip,
    parse_imbalance_volumes_zip,
    parse_loads,
    parse_prices,
    parse_procured_capacity_long,
    parse_unavailabilities,
    parse_water_hydro,
    unzip_xmls,
)
from entsoe_ext.series_ext import dedupe_by_index, resample_sdac_native

logger = logging.getLogger("entsoe_ext")

URL = os.getenv("ENTSOE_ENDPOINT_URL") or "https://web-api.tp.entsoe.eu/api"

QUARTER_MTU_SDAC_GOLIVE = pd.Timestamp("2025-10-01", tz="Europe/Amsterdam")

_AGGREGATED_BIDS_PROCESS_TYPES = {"A51", "A46", "A47", "A60", "A61", "A67", "A68"}
_PROCURED_PROCESS_TYPES = {"A51", "A52", "A47"}


class EntsoeClient:
    """ENTSO-E Transparency Platform client (raw XML + parsed-data channels).

    Parameters
    ----------
    api_key : str
        Defaults to the ENTSOE_API_KEY environment variable; TypeError when
        still absent.
    timeout : int
        Per-request timeout (seconds).
    retry_count : int
        Attempts for connection-class errors
        (ConnectionError / gaierror / RemoteDisconnected); HTTP 4xx/5xx are not
        retried here (the caller's retry layer handles those).
    retry_delay : int
        Delay between retries (seconds, fixed — no backoff, same as the library).
    """

    def __init__(self, api_key: str = None,
                 session: Optional[requests.Session] = None,
                 timeout: int = 90, retry_count: int = 1,
                 retry_delay: int = 5,
                 proxies: Optional[Dict] = None):
        self.api_key = api_key
        if self.api_key is None:
            self.api_key = os.getenv("ENTSOE_API_KEY")
        if self.api_key is None:
            raise TypeError("API key cannot be None")
        if session is None:
            session = requests.Session()
        self.session = session
        self.session.headers.update({
            "user-agent": "entsoe-ext self-built"})
        self.proxies = proxies
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.timeout = timeout

    # ============================================================
    # HTTP core (replicates the library's entsoe.py:81-166)
    # ============================================================

    @staticmethod
    def _datetime_to_str(dtm: pd.Timestamp) -> str:
        """tz-aware -> UTC; rounded to the nearest hour (round, not floor) -> YYYYMMDDHH00."""
        if dtm.tzinfo is not None:
            dtm = dtm.tz_convert("UTC")
        return dtm.round(freq="h").strftime("%Y%m%d%H00")

    def _base_request(self, params: Dict, start: pd.Timestamp,
                      end: pd.Timestamp) -> requests.Response:
        error = None
        for _ in range(self.retry_count):
            try:
                return self._request_once(params, start, end)
            except (requests.ConnectionError, socket.gaierror,
                    RemoteDisconnected) as e:
                error = e
                logger.warning(f"Connection Error, retrying in "
                               f"{self.retry_delay} seconds")
                time.sleep(self.retry_delay)
        raise error

    def _request_once(self, params: Dict, start: pd.Timestamp,
                      end: pd.Timestamp) -> requests.Response:
        params = dict(params)
        params.update({
            "securityToken": self.api_key,
            "periodStart": self._datetime_to_str(start),
            "periodEnd": self._datetime_to_str(end),
        })
        response = self.session.get(url=URL, params=params,
                                    proxies=self.proxies, timeout=self.timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.find_all("text")
            if len(text):
                error_text = soup.find("text").text
                if "No matching data found" in error_text:
                    raise NoMatchingDataError
                elif "check you request against dependency tables" in error_text:
                    raise InvalidBusinessParameterError
                elif "is not valid for this area" in error_text:
                    raise InvalidPSRTypeError
                elif ("amount of requested data exceeds allowed limit"
                      in error_text):
                    requested = error_text.split(" ")[-2]
                    allowed = error_text.split(" ")[-5]
                    raise PaginationError(
                        f"The API is limited to {allowed} elements per "
                        f"request. This query requested for {requested} "
                        f"documents and cannot be fulfilled as is.")
                elif ("requested data to be gathered via the offset parameter "
                      "exceeds the allowed limit" in error_text):
                    requested = error_text.split(" ")[-9]
                    allowed = error_text.split(" ")[-30][:-2]
                    raise PaginationError(
                        f"The API is limited to {allowed} elements per "
                        f"request. This query requested for {requested} "
                        f"documents and cannot be fulfilled as is.")
            raise e
        else:
            # The platform sometimes answers 200 with no data for otherwise valid
            # parameters — matched by content text.
            if response.headers.get("content-type", "") in (
                    "application/xml", "text/xml"):
                if "No matching data found" in response.text:
                    raise NoMatchingDataError
            return response

    def _request_xml(self, params: Dict, start, end) -> str:
        return self._base_request(params, start, end).text

    def _request_zip(self, params: Dict, start, end) -> bytes:
        return self._base_request(params, start, end).content

    # ============================================================
    # raw XML / ZIP channels (params key casing copied verbatim from the
    # library — the platform is case-insensitive, but byte-identical request
    # strings keep A/B comparisons clean)
    # ============================================================

    def day_ahead_prices_xml(self, country_code, start, end,
                             offset: int = 0, sequence: int = None) -> str:
        """A44. DE_LU / AT need sequence=1 (classificationSequence...position)."""
        area = lookup_area(country_code)
        params = {
            "documentType": "A44",
            "in_Domain": area.code,
            "out_Domain": area.code,
            "offset": offset,                       # always present (including 0), verbatim from the library
            "contract_MarketAgreement.type": "A01",  # lowercase t
        }
        if sequence is not None:
            params["classificationSequence_AttributeInstanceComponent.position"] = sequence
        return self._request_xml(params, start, end)

    def aggregated_bids_xml(self, country_code, process_type: str,
                            start, end) -> str:
        """A24 (no offset key — verbatim from the library). processType allow-list checked."""
        if process_type not in _AGGREGATED_BIDS_PROCESS_TYPES:
            raise ValueError("processType allowed values: "
                             + ", ".join(sorted(_AGGREGATED_BIDS_PROCESS_TYPES)))
        area = lookup_area(country_code)
        params = {
            "documentType": "A24",
            "area_Domain": area.code,
            "processType": process_type,
        }
        return self._request_xml(params, start, end)

    def net_position_xml(self, country_code, start, end,
                         dayahead: bool = True) -> str:
        """A25 ('Contract_MarketAgreement.Type' with an uppercase T — verbatim from the library)."""
        area = lookup_area(country_code)
        params = {
            "documentType": "A25",
            "businessType": "B09",
            "Contract_MarketAgreement.Type": "A01" if dayahead else "A07",
            "in_Domain": area.code,
            "out_Domain": area.code,
        }
        return self._request_xml(params, start, end)

    def load_xml(self, country_code, start, end) -> str:
        """A65 realised (processType A16)."""
        area = lookup_area(country_code)
        params = {
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": area.code,
            "out_Domain": area.code,
        }
        return self._request_xml(params, start, end)

    def load_forecast_xml(self, country_code, start, end,
                          process_type: str = "A01") -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A65",
            "processType": process_type,
            "outBiddingZone_Domain": area.code,
        }
        return self._request_xml(params, start, end)

    def generation_forecast_xml(self, country_code, start, end,
                                process_type: str = "A01") -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A71",
            "processType": process_type,
            "in_Domain": area.code,
        }
        return self._request_xml(params, start, end)

    def wind_solar_forecast_xml(self, country_code, start, end,
                                psr_type: str = None,
                                process_type: str = "A01") -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A69",
            "processType": process_type,
            "in_Domain": area.code,
        }
        if psr_type:
            params["psrType"] = psr_type
        return self._request_xml(params, start, end)

    def generation_xml(self, country_code, start, end,
                       psr_type: str = None) -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": area.code,
        }
        if psr_type:
            params["psrType"] = psr_type
        return self._request_xml(params, start, end)

    def installed_capacity_xml(self, country_code, start, end,
                               psr_type: str = None) -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A68",
            "processType": "A33",
            "in_Domain": area.code,
        }
        if psr_type:
            params["psrType"] = psr_type
        return self._request_xml(params, start, end)

    def hydro_reservoir_storage_xml(self, country_code, start, end) -> str:
        area = lookup_area(country_code)
        params = {
            "documentType": "A72",
            "processType": "A16",
            "in_Domain": area.code,
        }
        return self._request_xml(params, start, end)

    def _crossborder_xml(self, country_code_from, country_code_to, start, end,
                         doctype: str,
                         contract_marketagreement_type: str = None,
                         auction_type: str = None,
                         business_type: str = None,
                         classification_sequence: int = None) -> str:
        area_in = lookup_area(country_code_to)
        area_out = lookup_area(country_code_from)
        params = {
            "documentType": doctype,
            "in_Domain": area_in.code,
            "out_Domain": area_out.code,
        }
        if contract_marketagreement_type is not None:
            params["contract_MarketAgreement.Type"] = contract_marketagreement_type
        if auction_type is not None:
            params["Auction.Type"] = auction_type
        if business_type is not None:
            params["businessType"] = business_type
        if classification_sequence is not None:
            params["ClassificationSequence_AttributeInstanceComponent.Position"] = classification_sequence
        return self._request_xml(params, start, end)

    def crossborder_flows_xml(self, country_code_from, country_code_to,
                              start, end) -> str:
        """A11 physical cross-border flows."""
        return self._crossborder_xml(country_code_from, country_code_to,
                                     start, end, doctype="A11")

    def scheduled_exchanges_xml(self, country_code_from, country_code_to,
                                start, end, dayahead: bool = True) -> str:
        """A09 scheduled exchanges (dayahead=True -> contract A01, else A05)."""
        return self._crossborder_xml(
            country_code_from, country_code_to, start, end, doctype="A09",
            contract_marketagreement_type="A01" if dayahead else "A05")

    def ntc_dayahead_xml(self, country_code_from, country_code_to,
                         start, end) -> str:
        """A61 day-ahead NTC (contract A01)."""
        return self._crossborder_xml(
            country_code_from, country_code_to, start, end, doctype="A61",
            contract_marketagreement_type="A01")

    def offered_capacity_xml(self, country_code_from, country_code_to,
                             start, end, contract: str = "A01",
                             implicit: bool = True) -> str:
        """A31 (implicit) / A25 (explicit) allocated capacity."""
        return self._crossborder_xml(
            country_code_from, country_code_to, start, end,
            doctype="A31" if implicit else "A25",
            contract_marketagreement_type=contract,
            auction_type="A01" if implicit else "A02",
            business_type=None if implicit else "B05")

    # ---- bytes endpoints (A84 is plain XML — take .content; the rest are real ZIPs) ----
    def _activated_balancing_prices_raw(self, area, start, end,
                                        process_type: str = "A16") -> bytes:
        """A84 raw response bytes (this endpoint returns **plain XML, not a
        ZIP** — same disposition as the library: pass response.content to bs4;
        mis-treating it as a ZIP yields BadZipFile / empty documents)."""
        params = {
            "documentType": "A84",
            "processType": process_type,
            "controlArea_Domain": area.code,
        }
        return self._request_zip(params, start, end)  # content only — not required to be a ZIP

    def _imbalance_prices_zip(self, area, start, end,
                              psr_type: str = None) -> bytes:
        params = {
            "documentType": "A85",
            "controlArea_Domain": area.code,
        }
        if psr_type:
            params["psrType"] = psr_type
        return self._request_zip(params, start, end)

    def _imbalance_volumes_zip(self, area, start, end,
                               psr_type: str = None) -> bytes:
        params = {
            "documentType": "A86",
            "controlArea_Domain": area.code,
        }
        if psr_type:
            params["psrType"] = psr_type
        return self._request_zip(params, start, end)

    def _procured_capacity_zip(self, area, start, end, process_type: str,
                               type_marketagreement_type: str = None,
                               offset: int = 0) -> bytes:
        if process_type not in _PROCURED_PROCESS_TYPES:
            raise ValueError("processType allowed values: "
                             + ", ".join(sorted(_PROCURED_PROCESS_TYPES)))
        params = {
            "documentType": "A15",
            "area_Domain": area.code,
            "processType": process_type,
            "offset": offset,
        }
        if type_marketagreement_type:
            params["type_MarketAgreement.Type"] = type_marketagreement_type
        return self._request_zip(params, start, end)

    def _unavailability_zip(self, area, start, end, doctype: str,
                            docstatus: str = None, offset: int = 0) -> bytes:
        params = {
            "documentType": doctype,
            "biddingZone_domain": area.code,
            "offset": offset,
        }
        if docstatus:
            params["docStatus"] = docstatus
        return self._request_zip(params, start, end)

    def _unavailability_transmission_zip(self, area_from, area_to, start, end,
                                         offset: int = 0) -> bytes:
        params = {
            "documentType": "A78",
            "in_Domain": area_to.code,
            "out_Domain": area_from.code,
            "offset": offset,
        }
        return self._request_zip(params, start, end)

    # ---- *_xml variants of the ZIP endpoints (unzip + join to str, for A/B and audit re-runs) ----
    def imbalance_prices_xml(self, country_code, start, end,
                             psr_type: str = None) -> str:
        return "\n".join(unzip_xmls(self._imbalance_prices_zip(
            lookup_area(country_code), start, end, psr_type)))

    def imbalance_volumes_xml(self, country_code, start, end,
                              psr_type: str = None) -> str:
        return "\n".join(unzip_xmls(self._imbalance_volumes_zip(
            lookup_area(country_code), start, end, psr_type)))

    def activated_balancing_prices_xml(self, country_code, start, end,
                                       process_type: str = "A16") -> str:
        """A84 raw response text (plain-XML endpoint, not a ZIP; for A/B and
        audit re-runs)."""
        return self._activated_balancing_prices_raw(
            lookup_area(country_code), start, end,
            process_type).decode("utf-8", "replace")

    def procured_capacity_xml(self, country_code, start, end,
                              process_type: str) -> str:
        return "\n".join(unzip_xmls(self._procured_capacity_zip(
            lookup_area(country_code), start, end, process_type)))

    # ============================================================
    # Data methods (parse + chunk/paginate + post-processing)
    # ============================================================

    def day_ahead_prices(self, country_code, start,
                         end) -> pd.Series:
        """A44 SDAC day-ahead prices -> Series (area.tz).

        Outer +-1 day padding (the library's issue-187 fix); the SDAC MTU
        switch happens in Europe/Amsterdam time (resample('h').first() before
        go-live, native resolution after); year blocks + offset=100 pagination.
        """
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            def offset_fetch(offset):
                text = self.day_ahead_prices_xml(
                    area, start=s_, end=e_, offset=offset,
                    sequence=1 if area.name in ("DE_LU", "AT") else None)
                series_all = parse_prices(text)
                series = pd.concat([x for x in series_all.values()
                                    if len(x) > 0]).sort_index()
                if len(series) == 0:
                    raise NoMatchingDataError
                series = series.tz_convert("Europe/Amsterdam")
                if series.index.max() < QUARTER_MTU_SDAC_GOLIVE:
                    series = series.resample("h").first()
                else:
                    series_60 = series[series.index < QUARTER_MTU_SDAC_GOLIVE]
                    series_15 = series[series.index >= QUARTER_MTU_SDAC_GOLIVE]
                    series = pd.concat(
                        [series_60.resample("h").first(), series_15]
                    ).sort_index()
                series = series.tz_convert(area.tz).sort_index()
                series = series.truncate(before=s_, after=e_)
                if len(series) == 0:
                    raise NoMatchingDataError
                return series
            return chunking.fetch_documents(offset_fetch, 100)

        series = chunking.fetch_in_blocks(
            window_fetch,
            chunking.iter_year_blocks(start - pd.Timedelta(days=1),
                                      end + pd.Timedelta(days=1)))
        series = dedupe_by_index(series, keep="last")
        series = series.tz_convert(area.tz).sort_index()
        series = series.truncate(before=start, after=end)
        if len(series) == 0:
            raise NoMatchingDataError
        return series

    def net_position(self, country_code, start, end, dayahead: bool = True,
                     tz: str = None, golive=None) -> pd.Series:
        """A25 day-ahead net position -> Series (area.tz).

        Absorbs the former entsoe_ext.query_net_position_corrected: self-managed
        365-day blocks (the library's 0.8.1 uses @month_limited month blocks —
        neither drops a block's first instant, so the point set is identical;
        only the request granularity differs), parse_a25 (explicit in/out domain
        direction, native A25 sign), dedupe keep-last, hourly **mean** before
        go-live (not the library's first — zero change measured on RO),
        tz_convert + truncate.
        """
        area = lookup_area(country_code)
        if start is not None and end is not None and start < end:
            blocks = chunking.iter_year_blocks(start, end)
        else:
            blocks = [(start, end)]  # inverted/zero-length windows fall through to the underlying error
        parts = []
        for _s, _e in blocks:
            try:
                text = self.net_position_xml(area, start=_s, end=_e,
                                             dayahead=dayahead)
                series = parse_a25(text, area_code=country_code)
            except NoMatchingDataError:
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

    def _month_series(self, country_code, start, end, fetch_parse) -> pd.Series:
        """Month blocks + single-column DataFrame -> Series
        (load / load_forecast / generation_forecast).

        Replicates the library's 0.8.1 @month_limited (month blocks, no
        truncation; 0.8.0 used @year_limited).
        """
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            df = fetch_parse(area, s_, e_)
            df = df.tz_convert(area.tz)
            return df.truncate(before=s_, after=e_)

        df = chunking.fetch_in_blocks(window_fetch,
                                      chunking.iter_month_blocks(start, end))
        if isinstance(df, pd.DataFrame):
            if len(df.columns) == 1:
                return df.iloc[:, 0]
            raise ValueError(f"expected a single column, got: {list(df.columns)}")
        return df

    def load(self, country_code, start, end) -> pd.Series:
        """A65 actual load -> Series ('Actual Load', area.tz)."""
        def fetch_parse(area, s_, e_):
            return parse_loads(self.load_xml(area, start=s_, end=e_),
                               process_type="A16")
        return self._month_series(country_code, start, end, fetch_parse)

    def load_forecast(self, country_code, start, end,
                      process_type: str = "A01") -> pd.Series:
        """A65 load forecast -> Series ('Forecasted Load', area.tz)."""
        def fetch_parse(area, s_, e_):
            return parse_loads(self.load_forecast_xml(
                area, start=s_, end=e_, process_type=process_type),
                process_type=process_type)
        return self._month_series(country_code, start, end, fetch_parse)

    def generation(self, country_code, start, end,
                   psr_type: str = None) -> pd.DataFrame:
        """A75 actual generation -> DataFrame (columns = psr or MultiIndex(psr, metric), area.tz)."""
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            df = parse_generation(
                self.generation_xml(area, start=s_, end=e_, psr_type=psr_type))
            df = df.tz_convert(area.tz)
            return df.truncate(before=s_, after=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_month_blocks(start, end))

    def generation_forecast(self, country_code, start, end,
                            process_type: str = "A01",
                            nett: bool = False) -> Union[pd.Series, pd.DataFrame]:
        """A71 generation forecast -> **always a Series** (single psr; column name Actual -> Scheduled, area.tz)."""
        def fetch_parse(area, s_, e_):
            df = parse_generation(
                self.generation_forecast_xml(area, start=s_, end=e_,
                                             process_type=process_type),
                nett=nett)
            if isinstance(df, pd.DataFrame):
                df = df.rename(columns=lambda c: c.replace("Actual", "Scheduled"))
            return df
        return self._month_series(country_code, start, end, fetch_parse)

    def wind_solar_forecast(self, country_code, start, end,
                            psr_type: str = None) -> pd.DataFrame:
        """A69 wind & solar forecast -> DataFrame (columns = psr, nett=True, area.tz)."""
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            df = parse_generation(
                self.wind_solar_forecast_xml(area, start=s_, end=e_,
                                             psr_type=psr_type), nett=True)
            df = df.tz_convert(area.tz)
            return df.truncate(before=s_, after=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_month_blocks(start, end))

    def installed_capacity(self, country_code, year: int) -> pd.DataFrame:
        """A68 yearly installed capacity -> DataFrame (columns = psr, area.tz; published yearly, not truncated)."""
        area = lookup_area(country_code)
        start = pd.Timestamp(year=year, month=1, day=1, tz=area.tz)
        end = pd.Timestamp(year=year, month=12, day=31,
                           tz=area.tz) + pd.Timedelta(days=1)

        def window_fetch(s_, e_):
            df = parse_generation(
                self.installed_capacity_xml(area, start=s_, end=e_))
            df = df.tz_convert(area.tz)
            # the library widens to year bounds (truncate(start-YearBegin, end+YearEnd)) — equivalent to no truncation
            return df

        df = chunking.fetch_in_blocks(window_fetch,
                                      chunking.iter_year_blocks(start, end))
        return df

    def imbalance_prices(self, country_code, start, end) -> pd.DataFrame:
        """A85 imbalance prices -> DataFrame (Long/Short, area.tz). Year blocks (the library's @year_limited)."""
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            df = parse_imbalance_prices_zip(
                self._imbalance_prices_zip(area, start=s_, end=e_))
            df = df.tz_convert(area.tz)
            return df.truncate(before=s_, after=e_)

        df = chunking.fetch_in_blocks(window_fetch,
                                      chunking.iter_year_blocks(start, end))
        return dedupe_by_index(df, keep="last")

    def imbalance_volumes(self, country_code, start, end) -> pd.Series:
        """A86 imbalance volumes -> Series (area.tz). Month blocks (replicates the library's 0.8.1 @month_limited)."""
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            s = parse_imbalance_volumes_zip(
                self._imbalance_volumes_zip(area, start=s_, end=e_))
            s = s.tz_convert(area.tz)
            return s.truncate(before=s_, after=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_month_blocks(start, end))

    def activated_balancing_prices(self, country_code, start, end,
                                   process_type: str = "A16") -> pd.DataFrame:
        """A84 activated balancing energy prices -> DataFrame
        (Price/Direction/ReserveType, area.tz).

        Year blocks; **not deduplicated** (verbatim from the library): the
        source may return multiple documents for the same key (original +
        revisions) and consumers rely on those duplicate rows; each consumer
        collapses them itself (drop_duplicates(keep="last")).
        """
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            df = parse_activated_balancing_energy_prices(
                self._activated_balancing_prices_raw(
                    area, start=s_, end=e_, process_type=process_type))
            df = df.tz_convert(area.tz)
            return df.truncate(before=s_, after=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_year_blocks(start, end))

    def procured_capacity(self, country_code, process_type: str, start,
                          end) -> pd.DataFrame:
        """A81 procured balancing capacity -> **long table**
        [timestamp, direction, mrid, price, volume].

        Year blocks + paginate + offset=100 pagination (the library nests three
        decorators). Timestamps convert to area.tz and truncate per block with
        the library's semantics (df.tz_convert(area.tz) + truncate, implemented
        column-wise on the long table; duplicates at block boundaries are
        collapsed by the final keep-last dedupe).
        """
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            def paginated_fetch(start, end):
                def offset_fetch(offset):
                    df = parse_procured_capacity_long(
                        self._procured_capacity_zip(
                            area, start=start, end=end,
                            process_type=process_type, offset=offset))
                    return df
                return chunking.fetch_documents(offset_fetch, 100)
            df = chunking.paginate(paginated_fetch, start=s_, end=e_)
            if df is None or df.empty:
                return df
            # library semantics: tz_convert(area.tz) + truncate(before=s_, after=e_)
            # (truncate includes both ends -> block-boundary duplicates,
            # collapsed by the outer keep-last dedupe)
            df["timestamp"] = pd.DatetimeIndex(
                df["timestamp"]).tz_convert(area.tz)
            df = df[(df["timestamp"] >= s_) & (df["timestamp"] <= e_)]
            return df.reset_index(drop=True)

        df = chunking.fetch_in_blocks(window_fetch,
                                      chunking.iter_year_blocks(start, end))
        if not df.empty:
            df = df.drop_duplicates(
                subset=["timestamp", "direction", "mrid"], keep="last")
            df = df.sort_values("timestamp", ignore_index=True)
        return df

    def hydro_reservoir_storage(self, country_code, start, end) -> pd.Series:
        """A72 reservoir filling / hydro storage -> Series (**native index, no
        tz_convert and no truncate** — library behaviour).

        Year blocks + paginate (isomorphic to the library's
        @year_limited @paginated).
        """
        area = lookup_area(country_code)

        def window_fetch(s_, e_):
            def fetch(start, end):
                return parse_water_hydro(
                    self.hydro_reservoir_storage_xml(area, start=start, end=end))
            return chunking.paginate(fetch, start=s_, end=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_year_blocks(start, end))

    def _border_series(self, country_code_from, country_code_to, start, end,
                       fetch_xml) -> pd.Series:
        """Shared cross-border helper: month blocks + generic parsing + tz_convert(from area) + per-block truncation."""
        area_from = lookup_area(country_code_from)

        def window_fetch(s_, e_):
            ts = parse_crossborder_flows(
                fetch_xml(country_code_from, country_code_to, s_, e_))
            ts = ts.tz_convert(area_from.tz)
            return ts.truncate(before=s_, after=e_)

        return chunking.fetch_in_blocks(
            window_fetch, chunking.iter_month_blocks(start, end))

    def crossborder_flows(self, country_code_from, country_code_to, start,
                          end) -> pd.Series:
        """A11 physical cross-border flows -> Series (from-area timezone)."""
        return self._border_series(
            country_code_from, country_code_to, start, end,
            self.crossborder_flows_xml)

    def scheduled_exchanges(self, country_code_from, country_code_to, start,
                            end, dayahead: bool = True) -> pd.Series:
        """A09 scheduled exchanges -> Series (from-area timezone)."""
        return self._border_series(
            country_code_from, country_code_to, start, end,
            lambda f, t, s, e: self.scheduled_exchanges_xml(
                f, t, s, e, dayahead=dayahead))

    def ntc_dayahead(self, country_code_from, country_code_to, start,
                     end) -> pd.Series:
        """A61 day-ahead NTC -> Series (from-area timezone)."""
        return self._border_series(
            country_code_from, country_code_to, start, end,
            self.ntc_dayahead_xml)

    def offered_capacity(self, country_code_from, country_code_to, start,
                         end, contract: str = "A01",
                         implicit: bool = True) -> pd.Series:
        """A31 allocated capacity (implicit auction) -> Series (from-area timezone). Year blocks + paginate."""
        area_from = lookup_area(country_code_from)

        def window_fetch(s_, e_):
            def paginated_fetch(start, end):
                return parse_crossborder_flows(
                    self.offered_capacity_xml(
                        country_code_from, country_code_to, start, end,
                        contract=contract, implicit=implicit))
            return chunking.paginate(paginated_fetch, start=s_, end=e_)

        series = chunking.fetch_in_blocks(
            window_fetch, chunking.iter_year_blocks(start, end))
        series = series.tz_convert(area_from.tz).sort_index()
        series = series.truncate(before=start, after=end)
        return series

    def outage_units(self, country_code, start, end,
                     kind: str = "generation",
                     docstatus: str = None) -> pd.DataFrame:
        """A80/A77 outages -> DataFrame (19 columns, index created_doc_time,
        area.tz).

        Year blocks + paginate + offset=200 pagination (dedupe exempt — each
        document revision stays a separate row, verbatim from the library);
        the window filter is OR (verbatim from the library :2338).
        """
        if kind not in ("generation", "production"):
            raise ValueError("kind must be 'generation' or 'production'")
        area = lookup_area(country_code)
        doctype = "A80" if kind == "generation" else "A77"

        def window_fetch(s_, e_):
            def paginated_fetch(start, end):
                def offset_fetch(offset):
                    df = parse_unavailabilities(
                        self._unavailability_zip(
                            area, start=start, end=end, doctype=doctype,
                            docstatus=docstatus, offset=offset),
                        doctype)
                    df = df.tz_convert(area.tz)
                    df["start"] = df["start"].apply(
                        lambda x: x.tz_convert(area.tz))
                    df["end"] = df["end"].apply(
                        lambda x: x.tz_convert(area.tz))
                    return df
                return chunking.fetch_documents(offset_fetch, 200, dedupe=False)
            return chunking.paginate(paginated_fetch, start=s_, end=e_)

        df = chunking.fetch_in_blocks(window_fetch,
                                      chunking.iter_year_blocks(start, end))
        if not df.empty:
            df = df.drop_duplicates(keep="first")  # block-boundary duplicate fallback (identical full rows)
        return df[(df["start"] < end) | (df["end"] > start)]

    def outage_transmission(self, country_code_from, country_code_to, start,
                            end) -> pd.DataFrame:
        """A78 transmission outages -> DataFrame (14 columns, from-area timezone). Paginate only (verbatim from the library)."""
        area_from = lookup_area(country_code_from)
        area_to = lookup_area(country_code_to)

        def paginated_fetch(start, end):
            df = parse_unavailabilities(
                self._unavailability_transmission_zip(
                    area_from, area_to, start=start, end=end, offset=0),
                "A78")
            df = df.tz_convert(area_from.tz)
            df["start"] = df["start"].apply(lambda x: x.tz_convert(area_from.tz))
            df["end"] = df["end"].apply(lambda x: x.tz_convert(area_from.tz))
            return df

        df = chunking.paginate(paginated_fetch, start=start, end=end)
        return df[(df["start"] < end) | (df["end"] > start)]


# ============================================================
# Factory (the single construction point)
# ============================================================
def get_client(api_key: str = None, timeout: int = 90, retry_count: int = 1,
               retry_delay: int = 5) -> EntsoeClient:
    """Construct the self-built ENTSO-E client (single entry point).

    Parameters
    ----------
    api_key : str, optional
        Defaults to the ENTSOE_API_KEY environment variable.
    timeout / retry_count / retry_delay
        See the EntsoeClient docs (defaults 90 s / 1 try / 5 s).
    """
    key = api_key or os.getenv("ENTSOE_API_KEY")
    if not key:
        raise RuntimeError(
            "ENTSOE_API_KEY is not set (api_key argument / environment variable)")
    return EntsoeClient(api_key=key, timeout=timeout,
                        retry_count=retry_count, retry_delay=retry_delay)
