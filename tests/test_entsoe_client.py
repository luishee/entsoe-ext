# -*- coding: utf-8 -*-
"""entsoe_ext.EntsoeClient unit tests (offline; FakeClient intercepts _base_request).

Coverage: periodStart/End round('h') formatting, HTTP error body -> exception
mapping (four classes), 200+XML no-data path, connection-error retry, params
key casing / DE_LU sequence, day_ahead_prices SDAC golive semantics, A25
net_position 365-day chunking.
Run: python -m unittest discover -s tests -v
"""
import unittest

import pandas as pd
import requests

from entsoe_ext.client import EntsoeClient
from entsoe_ext.exceptions import (InvalidBusinessParameterError,
                                   InvalidPSRTypeError, NoMatchingDataError,
                                   PaginationError)


class _Resp:
    def __init__(self, text="", content=b"", status=200,
                 content_type="application/xml"):
        self.text = text
        self.content = content
        self.status_code = status
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def _err_xml(msg):
    return (f"<Acknowledgement_MarketDocument><Reason><code>999</code>"
            f"<text>{msg}</text></Reason></Acknowledgement_MarketDocument>")


class _FakeSession:
    """Transport-layer fake: captures request params, returns a canned _Resp.

    Intercepts session.get (not _base_request) so that error mapping / retry /
    token injection / periodStart formatting all execute for real.
    """

    def __init__(self, responder):
        self.responder = responder  # responder(params) -> _Resp | raise
        self.calls = []             # [(url, params_dict), ...]
        self.headers = {}

    def get(self, url, params=None, proxies=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responder(dict(params or {}))


class FakeClient(EntsoeClient):
    def __init__(self, responder):
        self.api_key = "test-key"
        self.session = _FakeSession(responder)
        self.proxies = None
        self.timeout = 1
        self.retry_count = 1
        self.retry_delay = 0

    @property
    def calls(self):
        return self.session.calls


class TestDatetimeToStr(unittest.TestCase):
    def test_round_to_nearest_hour(self):
        f = EntsoeClient._datetime_to_str
        # 02:10 -> 02:00; 02:50 -> 03:00 (round, not floor)
        self.assertEqual(f(pd.Timestamp("2025-10-01T02:10:00Z")), "202510010200")
        self.assertEqual(f(pd.Timestamp("2025-10-01T02:50:00Z")), "202510010300")
        # tz-aware non-UTC -> convert to UTC first: RO 05:10+03:00 = UTC 02:10
        self.assertEqual(
            f(pd.Timestamp("2025-10-01T05:10:00+03:00")), "202510010200")
        # naive treated as UTC
        self.assertEqual(f(pd.Timestamp("2025-10-01T07:45")), "202510010800")


class TestErrorMapping(unittest.TestCase):
    def _client(self, status, body):
        return FakeClient(lambda p: _Resp(body, status=status))

    def test_no_matching_data_400(self):
        c = self._client(400, _err_xml("No matching data found for the "))
        with self.assertRaises(NoMatchingDataError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))

    def test_invalid_business_parameter(self):
        c = self._client(400, _err_xml(
            "Please check you request against dependency tables"))
        with self.assertRaises(InvalidBusinessParameterError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))

    def test_invalid_psr_type(self):
        c = self._client(400, _err_xml("psr X is not valid for this area"))
        with self.assertRaises(InvalidPSRTypeError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))

    def test_pagination_error(self):
        c = self._client(400, _err_xml(
            "The amount of requested data exceeds allowed limit"))
        with self.assertRaises(PaginationError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))

    def test_unmatched_http_error_reraised(self):
        c = self._client(401, "Unauthorized")
        with self.assertRaises(requests.HTTPError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))

    def test_200_xml_no_matching_data(self):
        c = FakeClient(lambda p: _Resp(_err_xml("No matching data found")))
        with self.assertRaises(NoMatchingDataError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))


class TestRetry(unittest.TestCase):
    def test_connection_error_retried_then_success(self):
        state = {"n": 0}

        def responder(params):
            state["n"] += 1
            if state["n"] == 1:
                raise requests.ConnectionError("boom")
            return _Resp("<ok/>")

        c = FakeClient(responder)
        c.retry_count = 2
        c.retry_delay = 0
        text = c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                                  pd.Timestamp("2025-10-02", tz="UTC"))
        self.assertEqual(text, "<ok/>")
        self.assertEqual(state["n"], 2)

    def test_retry_exhausted_raises(self):
        c = FakeClient(lambda p: (_ for _ in ()).throw(
            requests.ConnectionError("boom")))
        c.retry_count = 2
        c.retry_delay = 0
        with self.assertRaises(requests.ConnectionError):
            c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))


class TestParams(unittest.TestCase):
    def test_a44_params_casing_and_offset(self):
        c = FakeClient(lambda p: _Resp(""))
        c.day_ahead_prices_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"))
        p = c.calls[0][1]
        self.assertEqual(p["documentType"], "A44")
        self.assertEqual(p["contract_MarketAgreement.type"], "A01")  # lowercase t
        self.assertIn("offset", p)  # always present
        self.assertNotIn("classificationSequence_AttributeInstanceComponent.position", p)

    def test_a44_de_lu_sequence(self):
        c = FakeClient(lambda p: _Resp(""))
        # sequence is set to 1 by day_ahead_prices() for DE_LU / AT; the raw method takes it explicitly
        c.day_ahead_prices_xml("DE_LU", pd.Timestamp("2025-10-01", tz="UTC"),
                               pd.Timestamp("2025-10-02", tz="UTC"),
                               sequence=1)
        p = c.calls[0][1]
        self.assertEqual(
            p["classificationSequence_AttributeInstanceComponent.position"], 1)

    def test_a25_contract_key_uppercase_t(self):
        c = FakeClient(lambda p: _Resp(""))
        c.net_position_xml("RO", pd.Timestamp("2025-10-01", tz="UTC"),
                           pd.Timestamp("2025-10-02", tz="UTC"), dayahead=False)
        p = c.calls[0][1]
        self.assertEqual(p["Contract_MarketAgreement.Type"], "A07")  # uppercase T

    def test_a24_whitelist_and_no_offset(self):
        c = FakeClient(lambda p: _Resp(""))
        c.aggregated_bids_xml("RO", "A51",
                              pd.Timestamp("2025-10-01", tz="UTC"),
                              pd.Timestamp("2025-10-02", tz="UTC"))
        p = c.calls[0][1]
        self.assertEqual(p["documentType"], "A24")
        self.assertNotIn("offset", p)
        with self.assertRaises(ValueError):
            c.aggregated_bids_xml("RO", "A99",
                                  pd.Timestamp("2025-10-01", tz="UTC"),
                                  pd.Timestamp("2025-10-02", tz="UTC"))

    def test_security_token_and_periods(self):
        c = FakeClient(lambda p: _Resp(""))
        c.net_position_xml("RO", pd.Timestamp("2025-10-01T02:10:00Z"),
                           pd.Timestamp("2025-10-02T02:50:00Z"))
        p = c.calls[0][1]
        self.assertEqual(p["securityToken"], "test-key")
        self.assertEqual(p["periodStart"], "202510010200")
        self.assertEqual(p["periodEnd"], "202510020300")


def _a44_xml(start, resolution, values):
    pts = "\n".join(
        f"<Point><position>{i + 1}</position>"
        f"<price.amount>{v}</price.amount></Point>"
        for i, v in enumerate(values))
    n = len(values)
    end = (pd.Timestamp(start)
           + (pd.Timedelta(minutes=15) if resolution == "PT15M"
              else pd.Timedelta(hours=1)) * n).isoformat()
    return f"""<Publication_MarketDocument>
  <TimeSeries><curveType>A03</curveType>
    <Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>
    <resolution>{resolution}</resolution>
{pts}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


class TestDayAheadPrices(unittest.TestCase):
    def _client(self, xml_by_offset):
        def responder(params):
            off = params.get("offset", 0)
            if off in xml_by_offset:
                return _Resp(xml_by_offset[off])
            raise NoMatchingDataError
        return FakeClient(responder)

    def test_pre_golive_hourly_first(self):
        # pre-golive (2025-09-30): native PT60M -> resample('h').first() (i.e. the original values)
        xml = _a44_xml("2025-09-29T21:00Z", "PT60M", [10.0, 11.0, 12.0])
        c = self._client({0: xml})
        s = c.day_ahead_prices(
            "RO", pd.Timestamp("2025-09-30 00:00", tz="Europe/Bucharest"),
            pd.Timestamp("2025-10-01 00:00", tz="Europe/Bucharest"))
        self.assertEqual(len(s), 3)
        self.assertAlmostEqual(float(s.iloc[0]), 10.0)
        self.assertEqual(str(s.index.tz), "Europe/Bucharest")

    def test_post_golive_native_15min(self):
        xml = _a44_xml("2025-10-04T21:00Z", "PT15M",
                       [1.0, 2.0, 3.0, 4.0])
        c = self._client({0: xml})
        s = c.day_ahead_prices(
            "RO", pd.Timestamp("2025-10-05 00:00", tz="Europe/Bucharest"),
            pd.Timestamp("2025-10-06 00:00", tz="Europe/Bucharest"))
        self.assertEqual(len(s), 4)
        self.assertEqual(list(s.values), [1.0, 2.0, 3.0, 4.0])


class TestNetPositionAbsorbed(unittest.TestCase):
    """A25 net_position absorbs the query_net_position_corrected chunking semantics."""

    def test_365d_blocks_and_seam_kept(self):
        def responder(params):
            # each block returns one value at its first instant (periodStart)
            start = pd.Timestamp(params["periodStart"], tz="UTC")
            return _Resp(_a25_xml(start.isoformat(), [42.0]))

        c = FakeClient(responder)
        s = pd.Timestamp("2024-01-01", tz="Europe/Bucharest")
        e = pd.Timestamp("2026-01-01", tz="Europe/Bucharest")  # 731 days -> 3 blocks
        series = c.net_position("RO", start=s, end=e)
        self.assertEqual(len(c.calls), 3)
        # adjacent blocks share the boundary instant (periodEnd[i] == periodStart[i+1])
        for i in range(2):
            self.assertEqual(c.calls[i][1]["periodEnd"],
                             c.calls[i + 1][1]["periodStart"])
        self.assertGreater(len(series), 0)

    def test_all_blocks_empty_raises(self):
        c = FakeClient(lambda p: (_ for _ in ()).throw(
            NoMatchingDataError))
        with self.assertRaises(NoMatchingDataError):
            c.net_position("RO", pd.Timestamp("2024-01-01", tz="UTC"),
                           pd.Timestamp("2025-01-01", tz="UTC"))


def _a25_xml(start, values):
    pts = "\n".join(
        f"<Point><position>{i + 1}</position><quantity>{v}</quantity></Point>"
        for i, v in enumerate(values))
    end = (pd.Timestamp(start) + pd.Timedelta(hours=len(values))).isoformat()
    return f"""<Publication_MarketDocument>
  <TimeSeries>
    <in_Domain.mRID codingScheme="A01">10YRO-TEL------P</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">10YHU-MAVIR----U</out_Domain.mRID>
    <curveType>A03</curveType>
    <Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>
      <resolution>PT60M</resolution>
{pts}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


if __name__ == "__main__":
    unittest.main()
