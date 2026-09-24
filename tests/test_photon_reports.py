from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from photon_fab.service import PhotonService


class ReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.create_lot(self.token, "LOT-A", "CMOS image sensor", "P3.2", 10)
        self.service.auth.create_user("op1", "operator-pw", "operator")
        self.operator = self.service.auth.login("op1", "operator-pw")

    def _measure(self, values) -> list[str]:
        ids = []
        for wavelength, response in values:
            ids.append(
                self.service.add_measurement(
                    self.token, "LOT-A", wavelength, response, 0.01, "spectrometer-1"
                )["measurement_id"]
            )
        return ids

    def test_insufficient_report_is_marked_without_confidence_interval(self) -> None:
        self._measure([(450, 0.71), (520, 0.93)])
        report = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertEqual(report["status"], "insufficient_sample")
        self.assertFalse(report["statistics"]["sufficient"])
        self.assertEqual(report["sample_count"], 2)
        self.assertAlmostEqual(report["statistics"]["mean"], 0.82)
        self.assertIsNone(report["statistics"]["ci_lower"])
        self.assertIsNone(report["statistics"]["ci_upper"])
        self.assertIn("2", report["note"])
        self.assertEqual(len(report["sample_fingerprint"]), 64)

    def test_sufficient_report_records_parameters_fingerprint_and_generator(self) -> None:
        self._measure([(450, 0.71), (520, 0.93), (650, 0.84)])
        report = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["generated_by"], "admin")
        self.assertEqual(report["process_rev"], "P3.2")
        self.assertEqual(report["metric"], "responsivity")
        params = report["parameters"]
        self.assertEqual(params["algorithm_version"], "responsivity-report/1")
        self.assertEqual(params["confidence"], 0.95)
        self.assertEqual(params["min_sample_size"], 3)
        self.assertEqual(params["z_value"], 1.96)
        stats = report["statistics"]
        self.assertTrue(stats["sufficient"])
        self.assertLess(stats["ci_lower"], stats["mean"])
        self.assertLess(stats["mean"], stats["ci_upper"])

    def test_history_report_does_not_pick_up_later_measurements(self) -> None:
        first_ids = self._measure([(450, 0.71), (520, 0.93)])
        first = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertEqual(first["status"], "insufficient_sample")
        first_fingerprint = first["sample_fingerprint"]

        self._measure([(650, 0.84), (700, 0.90)])
        reread = self.service.get_report(self.token, "LOT-A", first["report_id"])
        self.assertEqual(reread["sample_count"], 2)
        self.assertEqual(reread["sample_fingerprint"], first_fingerprint)
        self.assertEqual(sorted(s["measurement_id"] for s in reread["samples"]), sorted(first_ids))
        self.assertEqual(reread["status"], "insufficient_sample")
        self.assertIsNone(reread["statistics"]["ci_lower"])

        second = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertEqual(second["sample_count"], 4)
        self.assertNotEqual(second["sample_fingerprint"], first_fingerprint)
        self.assertEqual(second["status"], "ok")

        listing = self.service.list_reports(self.token, "LOT-A")
        self.assertEqual([item["report_id"] for item in listing], [first["report_id"], second["report_id"]])
        self.assertEqual([item["sample_count"] for item in listing], [2, 4])
        self.assertEqual([item["status"] for item in listing], ["insufficient_sample", "ok"])
        self.assertTrue(all("samples" not in item for item in listing))

    def test_same_dataset_reuses_the_snapshot(self) -> None:
        self._measure([(450, 0.71), (520, 0.93), (650, 0.84)])
        first = self.service.create_responsivity_report(self.token, "LOT-A")
        second = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(len(self.service.list_reports(self.token, "LOT-A")), 1)

    def test_changed_value_changes_fingerprint_and_creates_new_report(self) -> None:
        ids = self._measure([(450, 0.71), (520, 0.93), (650, 0.84)])
        first = self.service.create_responsivity_report(self.token, "LOT-A")
        self.service.db.execute(
            "UPDATE measurements SET response=? WHERE measurement_id=?", (0.99, ids[0])
        )
        self.service.db.commit()
        second = self.service.create_responsivity_report(self.token, "LOT-A")
        self.assertNotEqual(second["report_id"], first["report_id"])
        self.assertNotEqual(second["sample_fingerprint"], first["sample_fingerprint"])
        # 旧报告中的快照数值保持不变
        frozen = self.service.get_report(self.token, "LOT-A", first["report_id"])
        frozen_value = next(s["response"] for s in frozen["samples"] if s["measurement_id"] == ids[0])
        self.assertEqual(frozen_value, 0.71)

    def test_report_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "photon.sqlite3")
            service = PhotonService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "photon-admin")
            service.create_lot(token, "LOT-P", "PD array", "P4.0", 4)
            service.add_measurement(token, "LOT-P", 450, 0.71, 0.01, "spec-1")
            service.add_measurement(token, "LOT-P", 520, 0.93, 0.01, "spec-1")
            report = service.create_responsivity_report(token, "LOT-P")
            service.db.close()

            reopened = PhotonService(path)
            token = reopened.auth.login("admin", "photon-admin")
            stored = reopened.get_report(token, "LOT-P", report["report_id"])
            self.assertEqual(stored, report)
            reopened.add_measurement(token, "LOT-P", 650, 0.84, 0.01, "spec-1")
            still_frozen = reopened.get_report(token, "LOT-P", report["report_id"])
            self.assertEqual(still_frozen["sample_count"], 2)
            self.assertEqual(len(reopened.list_reports(token, "LOT-P")), 1)

    def test_unknown_report_and_lot(self) -> None:
        with self.assertRaises(KeyError):
            self.service.get_report(self.token, "LOT-A", "missing")
        with self.assertRaises(KeyError):
            self.service.create_responsivity_report(self.token, "NOPE")

    def test_operator_cannot_create_report_but_can_read(self) -> None:
        self._measure([(450, 0.71), (520, 0.93), (650, 0.84)])
        with self.assertRaises(PermissionError):
            self.service.create_responsivity_report(self.operator, "LOT-A")
        report = self.service.create_responsivity_report(self.token, "LOT-A")
        got = self.service.get_report(self.operator, "LOT-A", report["report_id"])
        self.assertEqual(got["report_id"], report["report_id"])

    def test_report_creation_is_audited(self) -> None:
        self._measure([(450, 0.71), (520, 0.93), (650, 0.84)])
        report = self.service.create_responsivity_report(self.token, "LOT-A")
        events = [e for e in self.service.audit(self.token, "LOT-A") if e["event_type"] == "stat_report"]
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["report_id"], report["report_id"])
        self.assertEqual(payload["sample_fingerprint"], report["sample_fingerprint"])
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
