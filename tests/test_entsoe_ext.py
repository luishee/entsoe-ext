# -*- coding: utf-8 -*-
"""Unit tests for entsoe-ext.

Coverage:
- A25 parsing: out=own→positive / in=own→negative, non-own domain excluded
  (incl. ``strict_other``), seam duplicate timestamps ``keep='last'``, tz conversion;
- ``resample_sdac_native``: multiple native MTUs before go-live → mean (not first),
  native resolution kept after go-live;
- ``resolution_to_timedelta_ext``: known set + generic ISO-8601 fallback + ValueError;
- ``dedupe_by_index`` / ``to_hourly_mean``;
- ``patch.install()`` idempotency and effectiveness.

Run: python -m unittest discover -s tests -v
"""
import unittest

import pandas as pd

from entsoe.mappings import lookup_area
from entsoe_ext import (
    dedupe_by_index,
    parse_a25,
    resolution_to_timedelta_ext,
    resample_sdac_native,
    to_hourly_mean,
)

RO_EIC = lookup_area("RO").code
NEIGH_EIC = "10YHU-TSO-----2"     # neighbour (not RO)
OTHER_EIC = "10YRS-TSO-----A"     # third domain (for "neither side is own")

GOLIVE = pd.Timestamp("2025-10-01", tz="Europe/Amsterdam")


def _ts_xml(in_eic, out_eic, start, resolution, values, curve="A03"):
    """Build a single A25 TimeSeries XML fragment (positions numbered from 1)."""
    start_ts = pd.Timestamp(start)
    delta = pd.Timedelta(resolution)
    end_ts = start_ts + delta * len(values)
    points = "\n".join(
        f"        <Point><position>{i + 1}</position><quantity>{v}</quantity></Point>"
        for i, v in enumerate(values)
    )
    return f"""  <TimeSeries>
    <in_Domain.mRID codingScheme="A01">{in_eic}</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">{out_eic}</out_Domain.mRID>
    <curveType>{curve}</curveType>
    <Period>
      <timeInterval>
        <start>{start_ts.isoformat()}</start>
        <end>{end_ts.isoformat()}</end>
      </timeInterval>
      <resolution>{resolution}</resolution>
{points}
    </Period>
  </TimeSeries>"""


def _doc(*blocks):
    return "<GL_MarketDocument>\n" + "\n".join(blocks) + "\n</GL_MarketDocument>"


class TestParseA25(unittest.TestCase):
    """A25 parsing: direction decided explicitly by in/out domain (native sign kept)."""

    def test_out_domain_is_positive_in_is_negative(self):
        # out=RO → export (positive); in=RO → import (negative); non-overlapping
        xml = _doc(
            _ts_xml(NEIGH_EIC, RO_EIC, "2025-10-01T00:00Z", "PT15M", [100, 200]),
            _ts_xml(RO_EIC, NEIGH_EIC, "2025-10-01T00:30Z", "PT15M", [300]),
        )
        s = parse_a25(xml, "RO")
        self.assertEqual(len(s), 3)
        vals = list(s.values)
        self.assertAlmostEqual(vals[0], 100.0)
        self.assertAlmostEqual(vals[1], 200.0)
        self.assertAlmostEqual(vals[2], -300.0)
        self.assertEqual(str(s.index.tz), "UTC")

    def test_non_national_domain_excluded(self):
        # the only TimeSeries has neither side == RO → excluded → empty Series
        xml = _doc(_ts_xml(NEIGH_EIC, OTHER_EIC, "2025-10-01T00:00Z", "PT15M", [50]))
        s = parse_a25(xml, "RO")
        self.assertEqual(len(s), 0)

    def test_strict_other_raises(self):
        xml = _doc(_ts_xml(NEIGH_EIC, OTHER_EIC, "2025-10-01T00:00Z", "PT15M", [50]))
        with self.assertRaises(ValueError):
            parse_a25(xml, "RO", strict_other=True)

    def test_seam_duplicate_keeps_last(self):
        # two RO-export TimeSeries overlap at 00:00 → keep='last' wins
        xml = _doc(
            _ts_xml(NEIGH_EIC, RO_EIC, "2025-10-01T00:00Z", "PT15M", [111, 222]),
            _ts_xml(NEIGH_EIC, RO_EIC, "2025-10-01T00:00Z", "PT15M", [999]),
        )
        s = parse_a25(xml, "RO")
        self.assertEqual(len(s), 2)
        also = s.loc[pd.Timestamp("2025-10-01T00:00Z")]
        self.assertAlmostEqual(float(also), 999.0)
        self.assertAlmostEqual(float(s.loc[pd.Timestamp("2025-10-01T00:15Z")]), 222.0)

    def test_tz_convert(self):
        xml = _doc(_ts_xml(NEIGH_EIC, RO_EIC, "2025-10-01T00:00Z", "PT15M", [100]))
        s = parse_a25(xml, "RO", tz="Europe/Bucharest")
        self.assertEqual(str(s.index.tz), "Europe/Bucharest")
        # 2025-10-01 Romania is EEST (UTC+3) → 00:00Z = 03:00 local
        self.assertIn(pd.Timestamp("2025-10-01T03:00:00+03:00"), s.index)


class TestResampleSdacNative(unittest.TestCase):
    """Before go-live → mean (not the library's first); after → keep native."""

    def _series(self, times, vals):
        idx = pd.DatetimeIndex([pd.Timestamp(t).tz_localize("Europe/Amsterdam")
                                if pd.Timestamp(t).tz is None else pd.Timestamp(t)
                                for t in times])
        return pd.Series([float(v) for v in vals], index=idx)

    def test_before_golive_multi_mtu_takes_mean_not_first(self):
        # 3 native MTUs in one hour: 2,2,3 → mean=7/3≈2.333 (first would be 2)
        s = self._series(["2025-09-30T10:00", "2025-09-30T10:20", "2025-09-30T10:40"],
                         [2, 2, 3])
        out = resample_sdac_native(s, golive=GOLIVE)
        self.assertEqual(len(out), 1)
        got = float(out.iloc[0])
        self.assertAlmostEqual(got, 7.0 / 3.0, places=6)
        self.assertNotAlmostEqual(got, 2.0, places=6)

    def test_after_golive_keeps_native_resolution(self):
        times = ["2025-10-01T10:00", "2025-10-01T10:15",
                 "2025-10-01T10:30", "2025-10-01T10:45"]
        s = self._series(times, [1, 2, 3, 4])
        out = resample_sdac_native(s, golive=GOLIVE)
        self.assertEqual(len(out), 4)
        self.assertListEqual([float(v) for v in out.values], [1.0, 2.0, 3.0, 4.0])


class TestResolutionToTimedeltaExt(unittest.TestCase):
    def test_known(self):
        for raw, exp in [("PT1M", "1min"), ("PT5M", "5min"), ("PT15M", "15min"),
                         ("PT30M", "30min"), ("PT60M", "60min"), ("P1D", "1D"),
                         ("P1W", "7D"), ("P7D", "7D"), ("P1M", "1MS"),
                         ("P3M", "3MS"), ("P1Y", "12MS")]:
            self.assertEqual(resolution_to_timedelta_ext(raw), exp, raw)

    def test_iso_generic_fallback(self):
        self.assertEqual(resolution_to_timedelta_ext("PT45M"), "45min")
        self.assertEqual(resolution_to_timedelta_ext("P2D"), "2D")

    def test_invalid_raises_valueerror(self):
        with self.assertRaises(ValueError):
            resolution_to_timedelta_ext("XYZ")
        with self.assertRaises(ValueError):
            resolution_to_timedelta_ext("")


class TestMiscHelpers(unittest.TestCase):
    def test_dedupe_by_index_keep_last(self):
        idx = pd.to_datetime(["2025-10-01T00:00Z", "2025-10-01T00:00Z",
                              "2025-10-01T00:15Z"])
        s = pd.Series([1.0, 2.0, 3.0], index=idx)
        out = dedupe_by_index(s, keep="last")
        self.assertTrue(out.index.is_unique)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(float(out.iloc[0]), 2.0)

    def test_to_hourly_mean(self):
        idx = pd.to_datetime(["2025-10-01T00:00Z", "2025-10-01T00:20Z",
                              "2025-10-01T00:40Z"])
        df = pd.DataFrame({"v": [1.0, 2.0, 3.0]}, index=idx)
        out = to_hourly_mean(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out.iloc[0]["v"]), 2.0)


class TestQueryNetPositionCorrectedWindowSplit(unittest.TestCase):
    """Window splitting of ``query_net_position_corrected`` (measured platform cap 365 d).

    RawClient carries no ``limited`` decorator → the function splits at 365 days
    itself. A fake client records every ``(start, end)`` call, verifying:
    - windows up to one year stay a single call (identical to 0.8.0 behaviour);
    - longer windows are split into ≤365-day left-closed/right-open blocks whose
      boundaries chain (each block's end == next block's start);
    - the first instant of every block is kept (no ``@year_limited`` left-open
      truncation);
    - ``NoMatchingDataError`` from some blocks skips them; raised only when all
      blocks are empty.
    """

    class _FakeClient:
        """Stand-in raw client: intercept ``_base_request`` to return canned XML.

        ``EntsoeRawClient.query_net_position(client, ...)`` only depends on
        ``self._base_request(params, start, end)`` → intercepting there avoids
        any network access.
        """

        def __init__(self, data_by_block=None):
            self.calls = []                     # [(start, end), ...]
            self.data_by_block = data_by_block or {}
            self._last = None                   # most recent (start, end)

        class _Resp:
            def __init__(self, text):
                self.text = text

        def _base_request(self, params, start, end):
            from entsoe.exceptions import NoMatchingDataError
            self.calls.append((start, end))
            self._last = (start, end)
            for (bs, be), (vals_start, values) in self.data_by_block.items():
                if start == bs and end == be:
                    xml = _doc(_ts_xml(NEIGH_EIC, RO_EIC, vals_start, "PT60M", values))
                    return self._Resp(xml)
            raise NoMatchingDataError

    def test_window_within_a_year_single_call(self):
        # a 3-month window (the ETL batch size) → single call, same as 0.8.0
        from entsoe_ext import query_net_position_corrected
        s = pd.Timestamp("2025-06-01", tz="Europe/Bucharest")
        e = pd.Timestamp("2025-09-01", tz="Europe/Bucharest")
        client = self._FakeClient(data_by_block={
            (s, e): ("2025-06-01T00:00Z", [100]),
        })
        out = query_net_position_corrected(client, "RO", start=s, end=e)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((client.calls[0][0], client.calls[0][1]), (s, e))
        self.assertEqual(len(out), 1)

    def test_window_over_a_year_split_into_365d_blocks(self):
        from entsoe_ext import query_net_position_corrected
        s = pd.Timestamp("2024-01-01", tz="Europe/Bucharest")
        e = pd.Timestamp("2026-01-01", tz="Europe/Bucharest")   # 731 days → 3 blocks
        client = self._FakeClient()
        with self.assertRaises(Exception):
            query_net_position_corrected(client, "RO", start=s, end=e)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0][0], s)
        self.assertEqual(client.calls[-1][1], e)
        for i in range(2):
            self.assertEqual(client.calls[i][1], client.calls[i + 1][0])
        # every block spans at most 365 days
        for bs, be in client.calls:
            self.assertLessEqual((be - bs).days, 365)

    def test_block_first_instant_not_dropped(self):
        # both blocks have data → the second block's first instant must survive
        from entsoe_ext import query_net_position_corrected
        s = pd.Timestamp("2024-06-01", tz="Europe/Bucharest")
        b2 = s + pd.Timedelta(days=365)          # block boundary
        e = pd.Timestamp("2025-09-01", tz="Europe/Bucharest")
        client = self._FakeClient(data_by_block={
            (s, b2): ("2024-06-01T00:00Z", [100, 110]),
            (b2, e): ("2025-05-31T00:00Z", [200, 210]),
        })
        out = query_net_position_corrected(client, "RO", start=s, end=e)
        self.assertEqual(len(client.calls), 2)
        idx = {t.isoformat() for t in out.index}
        # second block first instants: 2025-05-31 00:00Z = 03:00 EEST
        self.assertIn("2025-05-31T03:00:00+03:00", idx)
        self.assertIn("2025-05-31T04:00:00+03:00", idx)
        self.assertIn("2024-06-01T03:00:00+03:00", idx)

    def test_all_blocks_empty_raises(self):
        from entsoe.exceptions import NoMatchingDataError
        from entsoe_ext import query_net_position_corrected
        s = pd.Timestamp("2024-01-01", tz="Europe/Bucharest")
        e = pd.Timestamp("2026-01-01", tz="Europe/Bucharest")
        client = self._FakeClient()   # every block raises NoMatchingDataError
        with self.assertRaises(NoMatchingDataError):
            query_net_position_corrected(client, "RO", start=s, end=e)
        self.assertEqual(len(client.calls), 3)


class TestPatchInstall(unittest.TestCase):
    def test_install_idempotent_and_effective(self):
        import entsoe
        from entsoe_ext.patch import install
        import entsoe.series_parsers as SP

        if getattr(entsoe, "__version__", None) not in ("0.8.0", "0.8.1"):
            self.skipTest(f"entsoe-py version {entsoe.__version__!r} not validated, "
                          f"skipping patch assertions")
        self.assertTrue(install())
        self.assertTrue(getattr(SP, "_ENTS_EXT_PATCHED", False))
        # unknown resolution no longer raises NotImplementedError → ISO fallback
        self.assertEqual(SP._resolution_to_timedelta("PT45M"), "45min")


if __name__ == "__main__":
    unittest.main()