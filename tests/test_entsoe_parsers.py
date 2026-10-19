# -*- coding: utf-8 -*-
"""Offline unit tests for the self-built kernel (mappings/xmlcore/chunking/parsers).

Everything is based on constructed XML/ZIP fixtures; no network access.
Run: python -m unittest discover -s tests -v
"""
import unittest
import zipfile
from io import BytesIO

import pandas as pd

from entsoe_ext import chunking
from entsoe_ext.exceptions import NoMatchingDataError, PaginationError
from entsoe_ext.mappings import lookup_area
from entsoe_ext.parsers import (
    parse_activated_balancing_energy_prices,
    parse_crossborder_flows,
    parse_generation,
    parse_imbalance_prices,
    parse_imbalance_prices_zip,
    parse_imbalance_volumes,
    parse_imbalance_volumes_zip,
    parse_loads,
    parse_prices,
    parse_procured_capacity_long,
    parse_unavailabilities,
    parse_water_hydro,
)
from entsoe_ext.xmlcore import extract_timeseries, parse_timeseries_generic

RO_EIC = "10YRO-TEL------P"


def _ts(points_xml, start="2025-10-01T00:00Z", resolution="PT15M",
        n=None, curve="A03", value_tag="quantity", interval_end=None):
    """Build a single TimeSeries XML fragment. points_xml is the <Point> string."""
    if interval_end is None:
        n = n or 96
        interval_end = (pd.Timestamp(start)
                        + pd.Timedelta(resolution.replace("PT", "").replace("M", "min")) * n
                        ).isoformat()
    return f"""  <TimeSeries>
    <mRID>1</mRID>
    <curveType>{curve}</curveType>
    <Period>
      <timeInterval>
        <start>{start}</start>
        <end>{interval_end}</end>
      </timeInterval>
      <resolution>{resolution}</resolution>
{points_xml}
    </Period>
  </TimeSeries>"""


def _pt(pos, val, tag="quantity"):
    return (f"      <Point><position>{pos}</position>"
            f"<{tag}>{val}</{tag}></Point>")


def _doc(*blocks, root="Publication_MarketDocument"):
    return f"<{root}>\n" + "\n".join(blocks) + f"\n</{root}>"


def _zip_of(*xml_texts):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, t in enumerate(xml_texts):
            z.writestr(f"doc_{i:03d}.xml", t)
    return buf.getvalue()


class TestMappings(unittest.TestCase):
    def test_lookup_by_name_eic_and_invalid(self):
        for name in ("RO", "HU", "BG", "RS", "UA", "MD", "DE_LU", "NO_1",
                     "IT_NORD", "GR", "TR", "MK", "CH", "SE_4", "DK_2"):
            a = lookup_area(name)
            self.assertTrue(a.code.startswith("10Y") or len(a.code) >= 12, name)
            self.assertIsNotNone(a.tz, name)
        self.assertEqual(lookup_area(RO_EIC), lookup_area("RO"))
        with self.assertRaises(ValueError):
            lookup_area("XX_NOPE")

    def test_tz_values(self):
        self.assertEqual(lookup_area("RO").tz, "Europe/Bucharest")
        self.assertEqual(lookup_area("HU").tz, "Europe/Budapest")
        self.assertEqual(lookup_area("BG").tz, "Europe/Sofia")


class TestXmlCore(unittest.TestCase):
    def test_position_alignment_and_float(self):
        xml = _ts(_pt(1, "1,234.5") + "\n" + _pt(2, "2"), n=2)
        soup = next(extract_timeseries(xml))
        s = parse_timeseries_generic(soup, merge_series=True)
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s.iloc[0], 1234.5)
        self.assertAlmostEqual(s.iloc[1], 2.0)
        t0 = pd.Timestamp("2025-10-01T00:00Z")
        self.assertEqual(s.index[0], t0)
        self.assertEqual(s.index[1], t0 + pd.Timedelta(minutes=15))

    def test_a03_ffill_fills_missing_positions(self):
        # position 1 has a value, 2-4 missing (A03 repeat semantics) -> ffill
        xml = _ts(_pt(1, "7"), n=4)
        soup = next(extract_timeseries(xml))
        s = parse_timeseries_generic(soup, merge_series=True)
        self.assertEqual(len(s), 4)
        self.assertTrue((s == 7.0).all())

    def test_non_a03_keeps_only_explicit_points(self):
        xml = _ts(_pt(1, "7"), n=4, curve="A01")
        soup = next(extract_timeseries(xml))
        s = parse_timeseries_generic(soup, merge_series=True)
        self.assertEqual(len(s), 1)

    def test_multi_period_merge(self):
        p1 = _ts(_pt(1, "10"), start="2025-10-01T00:00Z", n=1,
                 interval_end="2025-10-01T00:15Z")
        # two Periods inside one TimeSeries: assembled by hand
        xml = f"""  <TimeSeries><curveType>A03</curveType>
    <Period><timeInterval><start>2025-10-01T00:00Z</start><end>2025-10-01T00:15Z</end></timeInterval>
      <resolution>PT15M</resolution>{_pt(1, '10')}</Period>
    <Period><timeInterval><start>2025-10-01T00:15Z</start><end>2025-10-01T00:30Z</end></timeInterval>
      <resolution>PT15M</resolution>{_pt(1, '20')}</Period>
  </TimeSeries>"""
        soup = next(extract_timeseries(xml))
        s = parse_timeseries_generic(soup, merge_series=True)
        self.assertEqual(len(s), 2)
        self.assertEqual(list(s.values), [10.0, 20.0])

    def test_empty_timeseries_defense(self):
        xml = "<TimeSeries><curveType>A03</curveType></TimeSeries>"
        soup = next(extract_timeseries(xml))
        s = parse_timeseries_generic(soup, merge_series=True)
        self.assertEqual(len(s), 0)  # the library raises ValueError here — hardened

    def test_dict_mode_buckets(self):
        xml = _ts(_pt(1, "5"), start="2025-10-01T00:00Z", n=1,
                  interval_end="2025-10-01T01:00Z", resolution="PT60M")
        soup = next(extract_timeseries(xml))
        d = parse_timeseries_generic(soup)
        self.assertIn("60min", d)
        self.assertEqual(len(d["60min"]), 1)
        self.assertIsNone(d["15min"])


class TestParsePrices(unittest.TestCase):
    def test_prices_buckets(self):
        xml = _doc(_ts(_pt(1, "50.5", tag="price.amount"), n=96))
        d = parse_prices(xml)
        self.assertEqual(len(d["15min"]), 96)
        self.assertAlmostEqual(float(d["15min"].iloc[0]), 50.5)


class TestParseLoads(unittest.TestCase):
    def test_actual_vs_forecast_columns(self):
        xml = _doc(_ts(_pt(1, "4000"), n=96), root="GL_MarketDocument")
        df_a = parse_loads(xml, process_type="A16")
        df_f = parse_loads(xml, process_type="A01")
        self.assertEqual(list(df_a.columns), ["Actual Load"])
        self.assertEqual(list(df_f.columns), ["Forecasted Load"])


class TestParseGeneration(unittest.TestCase):
    def _gen_ts(self, psr="B16", cons=False, vals="100"):
        dom = "<outBiddingZone_Domain.mRID>10YRO-TEL------P</outBiddingZone_Domain.mRID>" if cons else \
              "<inBiddingZone_Domain.mRID>10YRO-TEL------P</inBiddingZone_Domain.mRID>"
        return f"""  <TimeSeries>
    <mRID>1</mRID>{dom}
    <MktPSRType><psrType>{psr}</psrType></MktPSRType>
    <curveType>A03</curveType>
    <Period><timeInterval><start>2025-10-01T00:00Z</start><end>2025-10-01T00:15Z</end></timeInterval>
      <resolution>PT15M</resolution>{_pt(1, vals)}</Period>
  </TimeSeries>"""

    def test_single_psr_dataframe(self):
        xml = _doc(self._gen_ts(), root="GL_MarketDocument")
        df = parse_generation(xml)
        self.assertEqual(list(df.columns), ["Solar"])
        self.assertAlmostEqual(float(df["Solar"].iloc[0]), 100.0)

    def test_agg_cons_multiindex_and_nett(self):
        xml = _doc(self._gen_ts(psr="B25", cons=False, vals="300")
                   + self._gen_ts(psr="B25", cons=True, vals="150.5"),
                   root="GL_MarketDocument")
        df = parse_generation(xml)
        self.assertIn("Energy storage", df.columns.get_level_values(0))
        netted = parse_generation(xml, nett=True)
        self.assertAlmostEqual(float(netted["Energy storage"].iloc[0]), 149.5)


class TestImbalance(unittest.TestCase):
    def _pt85(self, pos, amount, category):
        return (f"      <Point><position>{pos}</position>"
                f"<imbalance_Price.amount>{amount}</imbalance_Price.amount>"
                f"<imbalance_Price.category>{category}</imbalance_Price.category></Point>")

    def test_prices_long_short_pivot(self):
        def ts85(category, vals):
            pts = "".join(
                f"<Point><position>{i + 1}</position>"
                f"<imbalance_Price.amount>{v}</imbalance_Price.amount>"
                f"<imbalance_Price.category>{category}</imbalance_Price.category></Point>"
                for i, v in enumerate(vals))
            return ("<TimeSeries><curveType>A03</curveType>"
                    "<Period><timeInterval><start>2025-10-01T00:00Z</start>"
                    "<end>2025-10-01T00:30Z</end></timeInterval>"
                    "<resolution>PT15M</resolution>" + pts + "</Period></TimeSeries>")
        # real A85 document shape: one TimeSeries per category
        xml = _doc(ts85("A04", ("10.5", "12.0")) + ts85("A05", ("11.5", "13.0")),
                   root="Balancing_MarketDocument")
        df = parse_imbalance_prices(xml)
        self.assertIn("Long", df.columns)
        self.assertIn("Short", df.columns)
        self.assertAlmostEqual(float(df["Long"].iloc[0]), 10.5)
        self.assertAlmostEqual(float(df["Long"].iloc[1]), 12.0)
        self.assertAlmostEqual(float(df["Short"].iloc[0]), 11.5)
        self.assertAlmostEqual(float(df["Short"].iloc[1]), 13.0)

    def test_prices_zip(self):
        xml = _doc(
            "<TimeSeries><curveType>A03</curveType>"
            "<Period><timeInterval><start>2025-10-01T00:00Z</start>"
            "<end>2025-10-01T00:15Z</end></timeInterval><resolution>PT15M</resolution>"
            + self._pt85(1, "9.5", "A04") + "</Period></TimeSeries>",
            root="Balancing_MarketDocument")
        df = parse_imbalance_prices_zip(_zip_of(xml))
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["Long"].iloc[0]), 9.5)

    def _pt86(self, pos, qty):
        return f"      <Point><position>{pos}</position><quantity>{qty}</quantity></Point>"

    def test_volumes_flow_direction_factor(self):
        def ts(direction):
            d = (f"<flowDirection.direction>{direction}</flowDirection.direction>"
                 if direction else "")
            return ("<TimeSeries>" + d + "<curveType>A03</curveType>"
                    "<Period><timeInterval><start>2025-10-01T00:00Z</start>"
                    "<end>2025-10-01T00:15Z</end></timeInterval>"
                    f"<resolution>PT15M</resolution>{self._pt86(1, '5')}</Period></TimeSeries>")
        for direction, expect in (("A01", 5.0), ("A02", -5.0), ("A03", 5.0)):
            s = parse_imbalance_volumes(_doc(ts(direction),
                                             root="Balancing_MarketDocument"))
            self.assertAlmostEqual(float(s.iloc[0]), expect, msg=direction)
        # no flowDirection (signed values sent directly) -> factor 1
        s = parse_imbalance_volumes(_doc(ts(None), root="Balancing_MarketDocument"))
        self.assertAlmostEqual(float(s.iloc[0]), 5.0)

    def test_volumes_zip(self):
        xml = _doc(
            "<TimeSeries><flowDirection.direction>A01</flowDirection.direction>"
            "<curveType>A03</curveType><Period>"
            "<timeInterval><start>2025-10-01T00:00Z</start>"
            "<end>2025-10-01T00:15Z</end></timeInterval>"
            f"<resolution>PT15M</resolution>{self._pt86(1, '3.25')}</Period></TimeSeries>",
            root="Balancing_MarketDocument")
        s = parse_imbalance_volumes_zip(_zip_of(xml))
        self.assertAlmostEqual(float(s.iloc[0]), 3.25)


class TestActivatedBalancingPrices(unittest.TestCase):
    def test_direction_reserve_and_ffill(self):
        pts = "".join([
            f"      <Point><position>{p}</position>"
            f"<activation_price.amount>{v}</activation_price.amount></Point>"
            for p, v in ((1, "200"), (3, "300"))])  # position 2 missing -> ffill
        xml = _doc(
            "<TimeSeries><flowDirection.direction>A01</flowDirection.direction>"
            "<businessType>A96</businessType><curveType>A03</curveType>"
            "<Period><timeInterval><start>2025-10-01T00:00Z</start>"
            "<end>2025-10-01T00:45Z</end></timeInterval>"
            f"<resolution>PT15M</resolution>{pts}</Period></TimeSeries>",
            root="Balancing_MarketDocument")
        df = parse_activated_balancing_energy_prices(xml)
        self.assertEqual(len(df), 3)
        self.assertEqual(df["Direction"].iloc[0], "Up")
        self.assertEqual(df["ReserveType"].iloc[0], "aFRR")
        self.assertAlmostEqual(float(df["Price"].iloc[1]), 200.0)  # ffill
        self.assertAlmostEqual(float(df["Price"].iloc[2]), 300.0)


class TestProcuredCapacityLong(unittest.TestCase):
    def test_long_table(self):
        def ts(direction, mrid, p1, v1):
            return (f"<TimeSeries><mRID>{mrid}</mRID>"
                    f"<flowDirection.direction>{direction}</flowDirection.direction>"
                    "<curveType>A03</curveType><Period>"
                    "<timeInterval><start>2025-10-01T00:00Z</start>"
                    "<end>2025-10-01T00:15Z</end></timeInterval>"
                    "<resolution>PT15M</resolution>"
                    f"<Point><position>1</position><procurement_price.amount>{p1}</procurement_price.amount>"
                    f"<quantity>{v1}</quantity></Point></Period></TimeSeries>")
        df = parse_procured_capacity_long(
            _zip_of(_doc(ts("A01", 1, "12.5", "100"), ts("A02", 2, "8", "50"),
                         root="Balancing_MarketDocument")))
        self.assertEqual(list(df.columns),
                         ["timestamp", "direction", "mrid", "price", "volume"])
        self.assertEqual(len(df), 2)
        up = df[df["direction"] == "Up"].iloc[0]
        self.assertEqual(int(up["mrid"]), 1)
        self.assertAlmostEqual(float(up["price"]), 12.5)
        self.assertAlmostEqual(float(up["volume"]), 100.0)


class TestUnavailabilities(unittest.TestCase):
    def _gen_doc(self):
        return """<Unavailability_MarketDocument>
  <mRID>DOC-1</mRID>
  <revisionNumber>2</revisionNumber>
  <createdDateTime>2025-09-01T10:00Z</createdDateTime>
  <docStatus><value>A02</value></docStatus>
  <TimeSeries>
    <businessType>A53</businessType>
    <biddingZone_Domain.mRID>10YRO-TEL------P</biddingZone_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <production_RegisteredResource.mRID>RES-1</production_RegisteredResource.mRID>
    <production_RegisteredResource.name>Plant Alpha</production_RegisteredResource.name>
    <production_RegisteredResource.pSRType.powerSystemResources.name>Unit 1</production_RegisteredResource.pSRType.powerSystemResources.name>
    <production_RegisteredResource.location.name>RO</production_RegisteredResource.location.name>
    <production_RegisteredResource.pSRType.pSRType>B04</production_RegisteredResource.pSRType.pSRType>
    <production_RegisteredResource.pSRType.powerSystemResources.nominalP>400</production_RegisteredResource.pSRType.powerSystemResources.nominalP>
    <available_Period>
      <timeInterval><start>2025-10-05T00:00Z</start><end>2025-10-05T02:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><quantity>150</quantity></Point>
      <Point><position>2</position><quantity>0</quantity></Point>
    </available_Period>
  </TimeSeries>
</Unavailability_MarketDocument>"""

    def test_gen_19_columns(self):
        df = parse_unavailabilities(_zip_of(self._gen_doc()), "A80")
        # of 19 headers, created_doc_time becomes the index -> 18 columns + index
        self.assertEqual(len(df.columns), 18)
        self.assertEqual(df.index.name, "created_doc_time")
        row = df.iloc[0]
        self.assertEqual(row["mrid"], "DOC-1")
        self.assertEqual(int(row["revision"]), 2)
        self.assertEqual(row["docstatus"], "Final")
        self.assertEqual(row["businesstype"], "Planned maintenance")
        self.assertEqual(row["biddingzone_domain"], "RO")
        self.assertEqual(row["plant_type"], "Fossil Gas")
        self.assertAlmostEqual(float(row["nominal_power"]), 400.0)
        # two Points -> two rows; the second row's end = period end
        self.assertEqual(len(df), 2)
        self.assertEqual(pd.Timestamp(row["end"]),
                         pd.Timestamp("2025-10-05T01:00Z"))
        self.assertEqual(pd.Timestamp(df.iloc[1]["end"]),
                         pd.Timestamp("2025-10-05T02:00Z"))
        self.assertEqual(df.iloc[1]["pstn"], "2")

    def test_transmission_14_columns(self):
        xml = """<Unavailability_MarketDocument>
  <mRID>DOC-T</mRID><revisionNumber>1</revisionNumber>
  <createdDateTime>2025-09-02T08:00Z</createdDateTime>
  <docStatus><value>A02</value></docStatus>
  <TimeSeries>
    <businessType>A54</businessType>
    <in_Domain.mRID>10YRO-TEL------P</in_Domain.mRID>
    <out_Domain.mRID>10YHU-MAVIR----U</out_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <available_Period>
      <timeInterval><start>2025-10-06T00:00Z</start><end>2025-10-06T01:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><quantity>100</quantity></Point>
    </available_Period>
  </TimeSeries>
</Unavailability_MarketDocument>"""
        df = parse_unavailabilities(_zip_of(xml), "A78")
        # of 14 headers, created_doc_time becomes the index -> 13 columns + index
        self.assertEqual(len(df.columns), 13)
        self.assertEqual(df.index.name, "created_doc_time")
        self.assertEqual(df.iloc[0]["in_domain"], "RO")
        self.assertEqual(df.iloc[0]["out_domain"], "HU")


class TestCrossborderAndHydro(unittest.TestCase):
    def test_crossborder_series(self):
        xml = _doc(_ts(_pt(1, "253.865"), n=1,
                       interval_end="2025-10-01T00:15Z"))
        s = parse_crossborder_flows(xml)
        self.assertEqual(len(s), 1)
        self.assertAlmostEqual(float(s.iloc[0]), 253.865)

    def test_water_hydro_series(self):
        xml = _doc(_ts(_pt(1, "1610300"), start="2025-09-13T21:00Z",
                       resolution="P7D", n=1,
                       interval_end="2025-09-20T21:00Z"),
                   root="GL_MarketDocument")
        s = parse_water_hydro(xml)
        self.assertEqual(len(s), 1)
        self.assertAlmostEqual(float(s.iloc[0]), 1610300.0)
        self.assertEqual(s.index[0], pd.Timestamp("2025-09-13T21:00Z"))


class TestChunking(unittest.TestCase):
    def test_year_blocks_365_cap(self):
        s = pd.Timestamp("2024-06-01", tz="UTC")
        e = pd.Timestamp("2026-01-01", tz="UTC")
        blocks = chunking.iter_year_blocks(s, e)
        self.assertEqual(len(blocks), 2)
        for bs, be in blocks:
            self.assertLessEqual((be - bs).days, 365)
        self.assertEqual(blocks[0][0], s)
        self.assertEqual(blocks[-1][1], e)
        # adjacent blocks share the boundary instant
        self.assertEqual(blocks[0][1], blocks[1][0])

    def test_month_blocks(self):
        s = pd.Timestamp("2026-01-15", tz="UTC")
        e = pd.Timestamp("2026-03-20", tz="UTC")
        blocks = chunking.iter_month_blocks(s, e)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0], (s, pd.Timestamp("2026-02-15", tz="UTC")))

    def test_fetch_in_blocks_skips_empty(self):
        def fetch(bs, be):
            if be <= pd.Timestamp("2026-01-05", tz="UTC"):
                raise NoMatchingDataError
            return pd.DataFrame({"v": [1, 2]}, index=[bs, be])
        df = chunking.fetch_in_blocks(
            fetch, chunking.iter_month_blocks(
                pd.Timestamp("2026-01-01", tz="UTC"),
                pd.Timestamp("2026-03-01", tz="UTC")))
        self.assertEqual(len(df), 4)
        with self.assertRaises(NoMatchingDataError):
            chunking.fetch_in_blocks(
                lambda bs, be: (_ for _ in ()).throw(NoMatchingDataError),
                [(pd.Timestamp("2026-01-01", tz="UTC"),
                  pd.Timestamp("2026-01-02", tz="UTC"))])

    def test_fetch_documents_offset_and_dedupe(self):
        calls = []

        def fetch(offset=0, **kw):
            calls.append(offset)
            if offset == 0:
                idx = pd.DatetimeIndex([pd.Timestamp("2026-01-01", tz="UTC")] * 2)
                return pd.DataFrame({"Price": [None, 5.0]}, index=idx)
            raise NoMatchingDataError

        df = chunking.fetch_documents(fetch, 100)
        self.assertEqual(calls, [0, 100])  # next page empty -> stop
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["Price"].iloc[0]), 5.0)  # ffill+last

    def test_paginate_binary_split(self):
        def fetch(start, end, **kw):
            if end - start > pd.Timedelta(days=1):
                raise PaginationError
            return pd.DataFrame({"v": [1]}, index=[start])
        df = chunking.paginate(
            fetch, pd.Timestamp("2026-01-01", tz="UTC"),
            pd.Timestamp("2026-01-04", tz="UTC"))
        # 3-day window -> binary split down to <=1 day: Jan1 / Jan1.75 / Jan2.5 / Jan3.25 -> 4 leaf blocks
        self.assertEqual(len(df), 4)


if __name__ == "__main__":
    unittest.main()
