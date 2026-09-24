from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from photon_fab.analytics import (
    fingerprint_measurements,
    responsivity_statistics,
    t_critical,
)
from photon_fab.api import Handler
from photon_fab.service import PhotonService


class ResponsivityStatisticsTests(unittest.TestCase):
    def test_single_sample_is_marked_insufficient(self) -> None:
        stats = responsivity_statistics([0.8])
        self.assertFalse(stats.sufficient)
        self.assertEqual(stats.sample_count, 1)
        self.assertIsNone(stats.mean)
        self.assertIsNone(stats.ci_lower)
        self.assertIsNone(stats.ci_upper)
        self.assertIn("at least 2 samples", stats.insufficient_reason)

    def test_empty_sample_is_marked_insufficient(self) -> None:
        stats = responsivity_statistics([])
        self.assertFalse(stats.sufficient)
        self.assertEqual(stats.sample_count, 0)
        self.assertIsNone(stats.ci_lower)

    def test_two_samples_honest_wide_interval(self) -> None:
        stats = responsivity_statistics([0.8, 1.0])
        self.assertTrue(stats.sufficient)
        self.assertAlmostEqual(stats.mean, 0.9)
        # 自由度 1 的 t=12.7062，区间必须明显宽于 z=1.96 的近似。
        expected_half = t_critical(1, 0.95) * (0.2 / 2 ** 0.5) / 2 ** 0.5
        self.assertAlmostEqual(stats.ci_upper - stats.mean, expected_half, places=4)
        self.assertGreater(stats.ci_upper - stats.ci_lower, 0.1)

    def test_zero_variance_does_not_fake_precision(self) -> None:
        stats = responsivity_statistics([0.9, 0.9, 0.9])
        self.assertFalse(stats.sufficient)
        self.assertEqual(stats.mean, 0.9)
        self.assertIsNone(stats.ci_lower)
        self.assertIsNone(stats.ci_upper)
        self.assertIn("zero sample variance", stats.insufficient_reason)

    def test_result_is_deterministic(self) -> None:
        values = [0.71, 0.93, 0.84, 0.88]
        self.assertEqual(responsivity_statistics(values), responsivity_statistics(values))

    def test_fingerprint_is_order_sensitive_and_stable(self) -> None:
        records = [
            {"measurement_id": "m1", "wavelength_nm": 450.0, "response": 0.71},
            {"measurement_id": "m2", "wavelength_nm": 520.0, "response": 0.93},
        ]
        digest = fingerprint_measurements(records)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, fingerprint_measurements(list(records)))
        self.assertNotEqual(digest, fingerprint_measurements(list(reversed(records))))


class ReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = PhotonService(str(Path(self.temporary.name) / "photon.sqlite3"))
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.create_lot(self.token, "LOT-R1", "CMOS image sensor", "P3.2", 10)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _add(self, response: float, wavelength: float = 550.0) -> None:
        self.service.add_measurement(
            self.token, "LOT-R1", wavelength, response, 0.01, "spectrometer-1"
        )

    def test_report_without_samples_is_insufficient(self) -> None:
        report = self.service.create_responsivity_report(self.token, "LOT-R1")
        self.assertFalse(report["statistics"]["sufficient"])
        self.assertEqual(report["statistics"]["sample_count"], 0)
        self.assertIsNone(report["statistics"]["mean"])
        self.assertIsNone(report["statistics"]["ci_lower"])
        self.assertIsNotNone(report["statistics"]["insufficient_reason"])
        self.assertEqual(report["process_rev"], "P3.2")
        self.assertEqual(report["created_by"], "admin")
        self.assertEqual(report["parameters"]["algorithm_version"], "photon-responsivity-report/1")
        self.assertEqual(report["parameters"]["confidence"], 0.95)
        self.assertEqual(len(report["data_fingerprint"]["sha256"]), 64)
        self.assertEqual(report["samples"], [])

    def test_report_with_two_samples_carries_confidence_interval(self) -> None:
        self._add(0.8)
        self._add(1.0)
        report = self.service.create_responsivity_report(self.token, "LOT-R1")
        stats = report["statistics"]
        self.assertTrue(stats["sufficient"])
        self.assertEqual(stats["sample_count"], 2)
        self.assertAlmostEqual(stats["mean"], 0.9)
        self.assertLess(stats["ci_lower"], 0.9)
        self.assertLess(0.9, stats["ci_upper"])

    def test_historical_report_ignores_later_measurements(self) -> None:
        self._add(0.8)
        self._add(1.0)
        first = self.service.create_responsivity_report(self.token, "LOT-R1")
        first_fingerprint = first["data_fingerprint"]["sha256"]

        # 工艺版本升级并追加测量；旧报告必须保持原样。
        from photon_fab.storage import transaction as db_transaction
        with db_transaction(self.service.db):
            self.service.db.execute(
                "UPDATE chip_lots SET process_rev='P4.0' WHERE lot_id='LOT-R1'"
            )
        self._add(0.6, wavelength=650.0)

        reread = self.service.get_report(self.token, first["report_id"])
        self.assertEqual(reread["statistics"]["sample_count"], 2)
        self.assertEqual(len(reread["samples"]), 2)
        self.assertEqual(reread["data_fingerprint"]["sha256"], first_fingerprint)
        self.assertEqual(reread["process_rev"], "P3.2")
        self.assertAlmostEqual(reread["statistics"]["mean"], 0.9)

        second = self.service.create_responsivity_report(self.token, "LOT-R1")
        self.assertEqual(second["process_rev"], "P4.0")
        self.assertEqual(second["statistics"]["sample_count"], 3)
        self.assertNotEqual(
            second["data_fingerprint"]["sha256"], first_fingerprint
        )

        listing = self.service.list_reports(self.token, "LOT-R1")
        self.assertEqual([r["report_id"] for r in listing], [first["report_id"], second["report_id"]])

    def test_unknown_report_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.service.get_report(self.token, "missing")


class ReportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = str(Path(self.temporary.name) / "photon.sqlite3")
        ready = threading.Event()

        def serve() -> None:
            # SQLite 连接有线程亲和性，服务必须在处理请求的线程内构建。
            Handler.service = PhotonService(database)
            Handler.service.bootstrap_admin()
            self.server = HTTPServer(("127.0.0.1", 0), Handler)
            self.port = self.server.server_address[1]
            ready.set()
            self.server.serve_forever()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(timeout=5))
        token_response = self._post("/login", {"user_id": "admin", "password": "photon-admin"})
        self.token = token_response["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else b"{}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        token = getattr(self, "token", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_report_round_trip_over_http(self) -> None:
        self._post("/lots", {"lot_id": "LOT-HTTP", "product": "sensor", "process_rev": "P3.2", "wafer_count": 4})
        for response in (0.8, 1.0):
            self._request("POST", "/lots/LOT-HTTP/measurements",
                          {"wavelength_nm": 550.0, "response": response, "noise": 0.01,
                           "instrument": "spec-1"})

        status, created = self._request("POST", "/lots/LOT-HTTP/responsivity-reports")
        self.assertEqual(status, 201)
        self.assertTrue(created["statistics"]["sufficient"])
        self.assertEqual(created["process_rev"], "P3.2")

        status, fetched = self._request("GET", f"/reports/{created['report_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["report_id"], created["report_id"])
        self.assertEqual(fetched["statistics"]["sample_count"], 2)

        status, listing = self._request("GET", "/lots/LOT-HTTP/responsivity-reports")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["reports"]), 1)

        status, _ = self._request("GET", "/reports/does-not-exist")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
