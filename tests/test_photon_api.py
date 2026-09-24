from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from photon_fab.api import Handler
from photon_fab.service import PhotonService


class ReportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = str(Path(self.directory.name) / "photon.sqlite3")
        service = PhotonService(database)
        service.bootstrap_admin()
        Handler.service = service
        Handler.log_message = lambda *args, **kwargs: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self._call("POST", "/login", {"user_id": "admin", "password": "photon-admin"}, auth=None)["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        Handler.service.db.close()
        self.directory.cleanup()

    def _call(self, method: str, path: str, body: dict | None = None, auth: str | None = "default"):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if auth == "default":
            headers["Authorization"] = f"Bearer {self.token}"
        elif auth is not None:
            headers["Authorization"] = f"Bearer {auth}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            self.fail(f"{method} {path} failed: {exc.code} {exc.read().decode()}")

    def test_report_lifecycle_over_http(self) -> None:
        self.assertEqual(self._call("GET", "/health")["service"], "photon-fab")
        self._call("POST", "/lots", {
            "lot_id": "LOT-HTTP", "product": "PD array", "process_rev": "P5.0", "wafer_count": 6,
        })
        for wavelength, response in ((450, 0.71), (520, 0.93)):
            self._call("POST", "/lots/LOT-HTTP/measurements", {
                "wavelength_nm": wavelength, "response": response, "noise": 0.01, "instrument": "spec-1",
            })
        thin = self._call("POST", "/lots/LOT-HTTP/responsivity-reports", {})
        self.assertEqual(thin["status"], "insufficient_sample")
        self.assertIsNone(thin["statistics"]["ci_lower"])

        self._call("POST", "/lots/LOT-HTTP/measurements", {
            "wavelength_nm": 650, "response": 0.84, "noise": 0.01, "instrument": "spec-1",
        })
        report = self._call("POST", "/lots/LOT-HTTP/responsivity-reports", {})
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["generated_by"], "admin")
        self.assertEqual(report["parameters"]["confidence"], 0.95)
        self.assertLess(report["statistics"]["ci_lower"], report["statistics"]["mean"])

        listing = self._call("GET", "/lots/LOT-HTTP/responsivity-reports")["reports"]
        self.assertEqual([item["status"] for item in listing], ["insufficient_sample", "ok"])

        # 复算返回同一快照
        again = self._call("POST", "/lots/LOT-HTTP/responsivity-reports", {})
        self.assertEqual(again["report_id"], report["report_id"])

        # 新增测量后，历史报告保持当时的数据
        self._call("POST", "/lots/LOT-HTTP/measurements", {
            "wavelength_nm": 700, "response": 0.90, "noise": 0.01, "instrument": "spec-2",
        })
        frozen = self._call("GET", f"/lots/LOT-HTTP/responsivity-reports/{report['report_id']}")
        self.assertEqual(frozen["sample_count"], 3)
        self.assertEqual(frozen["sample_fingerprint"], report["sample_fingerprint"])


if __name__ == "__main__":
    unittest.main()
