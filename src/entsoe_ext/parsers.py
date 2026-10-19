# -*- coding: utf-8 -*-
"""entsoe_ext document-type parsers (replicate every parsing path of
entsoe-py 0.8.1 parsers.py that this project uses; value-equivalent — only two
documented differences):

- A81 ``parse_procured_capacity_long``: outputs a **long table**
  [timestamp, direction, mrid, price, volume] (the library returns a 3-level
  MultiIndex (direction, mrid, unit) whose multi-day concat leaves NaN
  misalignment).
- A84 ``parse_activated_balancing_energy_prices``: verbatim from the library —
  only the **first <Period> of each TimeSeries** is read
  (``soup.find('period')``), so multi-Period revision segments are truncated
  (the same defect class as A24 is not fixed on this endpoint upstream).

Library paths not used by this project (per-plant installed capacity, offshore
A79, contracted reserves A89 ...) are not replicated.
"""
import zipfile
from io import BytesIO
from typing import Iterator, List

import bs4
import pandas as pd

from entsoe_ext.mappings import (PSRTYPE_MAPPINGS, DOCSTATUS, BSNTYPE, Area)
from entsoe_ext.xmlcore import (extract_timeseries, parse_timeseries_generic,
                                parse_timeseries_generic_whole)
from entsoe_ext.series_ext import resolution_to_timedelta_ext

__all__ = [
    "parse_prices", "parse_loads", "parse_generation", "parse_water_hydro",
    "parse_crossborder_flows", "parse_activated_balancing_energy_prices",
    "parse_imbalance_prices", "parse_imbalance_prices_zip",
    "parse_imbalance_volumes", "parse_imbalance_volumes_zip",
    "parse_procured_capacity_long", "parse_unavailabilities", "unzip_xmls",
]

CONSUMPTION_ELEMENT = "outBiddingZone_Domain.mRID"

# Derived from the Area enum: EIC -> enum name (bidding-zone label in unavailability parsing)
_INV_BIDDING_ZONE_DICO = {area.code: area.name for area in Area}


def unzip_xmls(content) -> Iterator[str]:
    """ZIP bytes -> yield each inner .xml text (a ZIP without XML yields nothing)."""
    with zipfile.ZipFile(BytesIO(content), "r") as arc:
        for f in arc.infolist():
            if f.filename.endswith("xml"):
                yield arc.read(f).decode("utf-8", "replace")


# ============================================================
# A44 day-ahead prices
# ============================================================

def parse_prices(xml_text) -> dict:
    """A44 -> {'15min': Series, '30min': Series, '60min': Series} (empty buckets are empty Series)."""
    series = {
        "15min": [],
        "30min": [],
        "60min": [],
    }
    for soup in extract_timeseries(xml_text):
        soup_series = parse_timeseries_generic(soup, "price.amount")
        for key in series.keys():
            series[key].append(soup_series[key])

    for freq, freq_series in series.items():
        try:
            series[freq] = pd.concat(freq_series).sort_index()
        except ValueError:
            series[freq] = pd.Series()
    return series


# ============================================================
# A65 load (actual / forecast / week- & month-ahead Min-Max)
# ============================================================

def _parse_load_timeseries(soup) -> pd.Series:
    return parse_timeseries_generic(soup, merge_series=True)


def parse_loads(xml_text, process_type: str = "A01") -> pd.DataFrame:
    """A65 -> DataFrame.

    - processType A01 -> {'Forecasted Load'}; A16 -> {'Actual Load'};
    - others (week/month-ahead) bucket by businessType A60/A61 ->
      {'Min Forecasted Load', 'Max Forecasted Load'}.
    """
    if process_type in ("A01", "A16"):
        series = []
        for soup in extract_timeseries(xml_text):
            series.append(_parse_load_timeseries(soup))
        series = pd.concat(series)
        series = series.sort_index()
        return pd.DataFrame({
            "Forecasted Load" if process_type == "A01" else "Actual Load": series,
        })
    else:
        series_min = pd.Series(dtype="object")
        series_max = pd.Series(dtype="object")
        for soup in extract_timeseries(xml_text):
            t = _parse_load_timeseries(soup)
            if soup.find("businesstype").text == "A60":
                series_min = pd.concat([series_min, t])
            elif soup.find("businesstype").text == "A61":
                series_max = pd.concat([series_max, t])
            else:
                continue
        return pd.DataFrame({
            "Min Forecasted Load": series_min,
            "Max Forecasted Load": series_max,
        })


# ============================================================
# A68/A69/A71/A75 generation family (installed capacity / wind & solar forecast / generation forecast / actual generation)
# ============================================================

def _parse_generation_timeseries(soup) -> pd.Series:
    """Single TimeSeries -> Series named 'Actual Aggregated' | 'Actual
    Consumption' (flat) or (psr, metric) (multi-level). Consumption is
    detected by the presence of an outBiddingZone_Domain.mRID element (plant
    own-use, e.g. pumped-storage charging)."""
    # should never have duplicated timestamps when differing time resolution.
    # so simply concat all possibilities
    series = parse_timeseries_generic(soup, merge_series=True)

    _psrtype = soup.find("psrtype")
    if _psrtype is not None:
        psrtype = _psrtype.text
    else:
        psrtype = None

    if soup.find(CONSUMPTION_ELEMENT.lower()):
        metric = "Actual Consumption"
    else:
        metric = "Actual Aggregated"

    name = [metric]
    if psrtype:
        psrtype_name = PSRTYPE_MAPPINGS[psrtype]
        name.append(psrtype_name)

    if len(name) == 1:
        series.name = name[0]
    else:
        # tuple name -> becomes a MultiIndex column after concat
        name.reverse()
        series.name = tuple(name)
    return series


def _calc_nett_and_drop_redundant_columns(df: pd.DataFrame, nett: bool):
    """nett=True -> Agg.fillna(0) - Cons.fillna(0); a single column squeezes to a Series."""
    def _calc_nett(_df):
        try:
            if {"Actual Aggregated"}.issubset(_df):
                if {"Actual Consumption"}.issubset(_df):
                    _new = (_df["Actual Aggregated"].fillna(0)
                            - _df["Actual Consumption"].fillna(0))
                else:
                    _new = _df["Actual Aggregated"].fillna(0)
            else:
                _new = -_df["Actual Consumption"].fillna(0)
        except KeyError:
            _new = None  # the library prints and returns an undefined variable here (potential NameError) — hardened to None
        return _new

    if hasattr(df.columns, "levels"):
        if len(df.columns.levels[-1]) == 1:
            # Drop the extra header, if it is redundant
            df = df.droplevel(axis=1, level=-1)
        elif nett:
            frames = []
            for column in df.columns.levels[-2]:
                new = _calc_nett(df[column])
                if new is None:
                    continue
                new.name = column
                frames.append(new)
            df = pd.concat(frames, axis=1)
    else:
        if nett:
            df = _calc_nett(df)
        elif len(df.columns) == 1:
            df = df.squeeze()

    return df


def parse_generation(xml_text, nett: bool = False):
    """A68/A69/A71/A75 -> DataFrame (columns = psr or MultiIndex(psr, metric))
    or Series (single-column squeeze after nett; a single-column DataFrame also
    squeezes)."""
    all_series = dict()
    for soup in extract_timeseries(xml_text):
        ts = _parse_generation_timeseries(soup)
        series = all_series.get(ts.name)
        if series is None:
            all_series[ts.name] = ts
        else:
            series = pd.concat([series, ts])
            series.sort_index(inplace=True)
            all_series[series.name] = series

    # drop duplicates in all series
    for name in all_series:
        ts = all_series[name]
        all_series[name] = ts[~ts.index.duplicated(keep="first")]

    df = pd.DataFrame.from_dict(all_series)
    df.sort_index(inplace=True)

    df = _calc_nett_and_drop_redundant_columns(df, nett=nett)
    return df


# ============================================================
# A11/A09/A61/A31 cross-border single-value endpoints / A72 hydro
# ============================================================

def parse_water_hydro(xml_text) -> pd.Series:
    """A72 -> Series (native index, no timezone conversion — library behaviour)."""
    return parse_timeseries_generic_whole(xml_text)


def parse_crossborder_flows(xml_text) -> pd.Series:
    """A11/A09/A61/A31 → Series（generic whole）。"""
    return parse_timeseries_generic_whole(xml_text, to_float=True)


# ============================================================
# A84 activated balancing energy prices
# ============================================================

def _parse_activated_balancing_energy_prices_timeseries(soup) -> pd.DataFrame:
    direction_options = {"A01": "Up", "A02": "Down"}
    reserve_type_options = {"A95": "FCR", "A96": "aFRR",
                            "A97": "mFRR", "A98": "RR"}

    flow_direction = direction_options[soup.find("flowdirection.direction").text]
    reserve_type = reserve_type_options[soup.find("businesstype").text]
    # VERBATIM from the library: only the first <Period> is read (multi-Period revision segments are truncated)
    period = soup.find("period")
    start = pd.to_datetime(period.find("timeinterval").find("start").text)
    end = pd.to_datetime(period.find("timeinterval").find("end").text)
    resolution = resolution_to_timedelta_ext(period.find("resolution").text)

    tx = pd.date_range(start=start, end=end, freq=resolution, inclusive="left")

    df = pd.DataFrame(index=tx, columns=["Price", "Direction", "ReserveType"])

    for point in period.find_all("point"):
        idx = int(point.find("position").text)
        df.loc[tx[idx - 1], "Price"] = float(
            point.find("activation_price.amount").text)
        df.loc[tx[idx - 1], "Direction"] = flow_direction
        df.loc[tx[idx - 1], "ReserveType"] = reserve_type

    # sparse-position forward fill (Direction/ReserveType take the previous value)
    df = df.infer_objects().ffill()
    return df


def parse_activated_balancing_energy_prices(xml_text) -> pd.DataFrame:
    """A84 → DataFrame [Price, Direction, ReserveType]。"""
    timeseries_blocks = extract_timeseries(xml_text)
    frames = (_parse_activated_balancing_energy_prices_timeseries(soup)
              for soup in timeseries_blocks)
    df = pd.concat(frames)
    df.sort_index(inplace=True)
    return df


# ============================================================
# A85 imbalance prices (ZIP)
# ============================================================

def _parse_imbalance_prices_timeseries(soup) -> pd.DataFrame:
    df = pd.DataFrame({
        "price": parse_timeseries_generic(
            soup, label="imbalance_price.amount", merge_series=True),
        "category": parse_timeseries_generic(
            soup, label="imbalance_price.category", merge_series=True,
            to_float=False),
    }).pivot(columns="category", values="price")

    df.rename(columns={"A04": "Long", "A05": "Short",
                       "None": "Price for Consumption"}, inplace=True)
    df.columns.name = None
    return df


def parse_imbalance_prices(xml_text) -> pd.DataFrame:
    """A85 -> DataFrame with columns Long(A04) / Short(A05) / Price for Consumption(None)."""
    timeseries_blocks = extract_timeseries(xml_text)
    frames = (_parse_imbalance_prices_timeseries(soup)
              for soup in timeseries_blocks)
    df = pd.concat(frames, axis=1)
    df = df.stack().unstack()  # ad-hoc fix to prevent column splitting by NaNs
    df.sort_index(inplace=True)
    return df


def parse_imbalance_prices_zip(zip_contents: bytes) -> pd.DataFrame:
    def gen_frames(archive):
        with zipfile.ZipFile(BytesIO(archive), "r") as arc:
            for f in arc.infolist():
                if f.filename.endswith("xml"):
                    yield parse_imbalance_prices(xml_text=arc.read(f))

    frames = gen_frames(zip_contents)
    df = pd.concat(frames)
    df.sort_index(inplace=True)
    return df


# ============================================================
# A86 imbalance volumes (ZIP)
# ============================================================

def _parse_imbalance_volumes_timeseries(soup, include_resolution: bool) -> pd.DataFrame:
    flow_direction = soup.find("flowdirection.direction")
    if flow_direction:
        # time series uses flow direction codes
        flow_direction_factor = {
            "A01": 1,   # in
            "A02": -1,  # out
            "A03": 1,   # symmetric / in-balance
        }[flow_direction.text]
    else:
        # time series uses positive and negative values
        flow_direction_factor = 1

    df = pd.DataFrame({
        "Imbalance Volume": (
            parse_timeseries_generic(soup, label="quantity", merge_series=True)
            * flow_direction_factor),
    })

    if include_resolution:
        df["Resolution"] = resolution_to_timedelta_ext(
            soup.find("resolution").text)

    return df


def parse_imbalance_volumes(xml_text, include_resolution: bool = False):
    """A86 -> Series (NaN-column splicing guard: bfill(axis=1).iloc[:, 0])."""
    timeseries_blocks = extract_timeseries(xml_text)
    frames = (_parse_imbalance_volumes_timeseries(soup, include_resolution)
              for soup in timeseries_blocks)
    df = pd.concat(frames, axis=1)
    df = df.bfill(axis=1).iloc[:, 0]  # prevent column splitting by NaNs
    df.sort_index(inplace=True)
    return df


def parse_imbalance_volumes_zip(zip_contents: bytes,
                                include_resolution: bool = False):
    def gen_frames(archive):
        with zipfile.ZipFile(BytesIO(archive), "r") as arc:
            for f in arc.infolist():
                if f.filename.endswith("xml"):
                    yield parse_imbalance_volumes(
                        xml_text=arc.read(f), include_resolution=include_resolution)

    frames = gen_frames(zip_contents)
    df = pd.concat(frames)
    df.sort_index(inplace=True)
    return df


# ============================================================
# A81 procured balancing capacity (ZIP) — long-table output (deliberate difference 3)
# ============================================================

def parse_procured_capacity_long(zip_contents: bytes) -> pd.DataFrame:
    """A15/A81 -> long-table DataFrame [timestamp, direction, mrid, price, volume].

    direction: flowDirection A01 -> Up / A02 -> Down; mrid: int;
    price = procurement_price.amount, volume = quantity (each with A03 ffill
    semantics).
    """
    direction_options = {"A01": "Up", "A02": "Down"}
    records: List[dict] = []
    for xml_text in unzip_xmls(zip_contents):
        for soup in extract_timeseries(xml_text):
            direction = direction_options[soup.find("flowdirection.direction").text]
            mrid = int(soup.find("mrid").text)
            price = parse_timeseries_generic(
                soup, label="procurement_price.amount", merge_series=True)
            volume = parse_timeseries_generic(
                soup, label="quantity", merge_series=True)
            idx = price.index.union(volume.index)
            for ts in idx:
                records.append({
                    "timestamp": ts,
                    "direction": direction,
                    "mrid": mrid,
                    "price": price.get(ts),
                    "volume": volume.get(ts),
                })
    df = pd.DataFrame.from_records(
        records, columns=["timestamp", "direction", "mrid", "price", "volume"])
    if not df.empty:
        df.sort_values("timestamp", inplace=True, ignore_index=True)
    return df


# ============================================================
# A77/A78/A80 outages (ZIP)
# ============================================================

HEADERS_UNAVAIL_GEN = ["created_doc_time", "docstatus", "mrid", "revision",
                       "businesstype", "biddingzone_domain", "qty_uom",
                       "curvetype", "production_resource_id",
                       "production_resource_name",
                       "production_resource_psr_name",
                       "production_resource_location", "plant_type",
                       "nominal_power", "start", "end", "resolution", "pstn",
                       "avail_qty"]

HEADERS_UNAVAIL_TRANSM = ["created_doc_time", "docstatus", "mrid", "revision",
                          "businesstype", "in_domain", "out_domain", "qty_uom",
                          "curvetype", "start", "end", "resolution", "pstn",
                          "avail_qty"]


def _available_period(timeseries: bs4.BeautifulSoup) -> list:
    for period in timeseries.find_all("available_period"):
        start_p = pd.Timestamp(period.timeinterval.start.text)
        end_p = pd.Timestamp(period.timeinterval.end.text)
        res = period.resolution.text
        pts = period.find_all("point")
        for idx, pt in enumerate(pts):
            pstn, qty = pt.position.text, pt.quantity.text
            start = start_p + (pd.Timedelta(res) * (int(pstn) - 1))
            if idx < (len(pts) - 1):
                pstn_next = pts[idx + 1].position.text
                end = start_p + (pd.Timedelta(res) * (int(pstn_next) - 1))
            else:
                end = end_p
            yield [start, end, res, pstn, qty]


def _unavailability_gen_ts(soup: bs4.BeautifulSoup) -> list:
    get_attr = lambda attr: ("" if soup.find(attr) is None
                             else soup.find(attr).text)
    get_float = lambda val: (float("NaN") if val == "" else float(val))

    f = [BSNTYPE[get_attr("businesstype")],
         _INV_BIDDING_ZONE_DICO[get_attr("biddingzone_domain.mrid")],
         get_attr("quantity_measure_unit.name"),
         get_attr("curvetype"),
         get_attr("production_registeredresource.mrid"),
         get_attr("production_registeredresource.name"),
         get_attr("production_registeredresource.psrtype.powersystemresources.name"),
         get_attr("production_registeredresource.location.name"),
         PSRTYPE_MAPPINGS.get(get_attr(
             "production_registeredresource.psrtype.psrtype"), ""),
         get_float(get_attr(
             "production_registeredresource.psrtype.powersystemresources.nominalp"))]
    return [f + p for p in _available_period(soup)]


def _unavailability_tm_ts(soup: bs4.BeautifulSoup) -> list:
    get_attr = lambda attr: ("" if soup.find(attr) is None
                             else soup.find(attr).text)
    f = [BSNTYPE[get_attr("businesstype")],
         _INV_BIDDING_ZONE_DICO[get_attr("in_domain.mrid")],
         _INV_BIDDING_ZONE_DICO[get_attr("out_domain.mrid")],
         get_attr("quantity_measure_unit.name"),
         get_attr("curvetype")]
    return [f + p for p in _available_period(soup)]


_UNAVAIL_PARSE_CFG = {
    "A77": (HEADERS_UNAVAIL_GEN, _unavailability_gen_ts),
    "A78": (HEADERS_UNAVAIL_TRANSM, _unavailability_tm_ts),
    "A80": (HEADERS_UNAVAIL_GEN, _unavailability_gen_ts),
}


def _outage_parser(xml_file: bytes, headers, ts_func) -> pd.DataFrame:
    xml_text = xml_file.decode()

    soup = bs4.BeautifulSoup(xml_text, "html.parser")
    mrid = soup.find("mrid").text
    revision_number = int(soup.find("revisionnumber").text)
    try:
        creation_date = pd.Timestamp(soup.createddatetime.text)
    except AttributeError:
        creation_date = ""
    try:
        docstatus = DOCSTATUS[soup.docstatus.value.text]
    except AttributeError:
        docstatus = None
    d = list()
    series = extract_timeseries(xml_text)
    for ts in series:
        row = [creation_date, docstatus, mrid, revision_number]
        for t in ts_func(ts):
            d.append(row + t)
    df = pd.DataFrame.from_records(d, columns=headers)
    return df


def parse_unavailabilities(response: bytes, doctype: str) -> pd.DataFrame:
    """A77/A78/A80 ZIP -> DataFrame (index created_doc_time; columns in HEADERS_*)."""
    headers, ts_func = _UNAVAIL_PARSE_CFG[doctype]
    dfs = list()
    with zipfile.ZipFile(BytesIO(response), "r") as arc:
        for f in arc.infolist():
            if f.filename.endswith("xml"):
                frame = _outage_parser(arc.read(f), headers, ts_func)
                dfs.append(frame)
    if len(dfs) == 0:
        df = pd.DataFrame(columns=headers)
    else:
        df = pd.concat(dfs, axis=0)
    df.set_index("created_doc_time", inplace=True)
    df.sort_index(inplace=True)
    return df
