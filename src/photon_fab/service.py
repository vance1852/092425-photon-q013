"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid

from .analytics import (
    MIN_RESPONSIVITY_SAMPLES,
    RESPONSIVITY_REPORT_VERSION,
    confidence_interval,
    responsivity_statistics,
    summarize_spectrum,
    yield_rate,
)
from .auth import Auth
from .storage import canonical_json, connect, event, fingerprint, transaction, utcnow


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def create_responsivity_report(
        self,
        token: str,
        lot_id: str,
        *,
        confidence: float = 0.95,
        min_sample_size: int = MIN_RESPONSIVITY_SAMPLES,
    ) -> dict:
        actor = self.auth.require(token, "analyze")
        lot = self.db.execute("SELECT process_rev FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        rows = self.db.execute(
            "SELECT measurement_id,wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY measurement_id",
            (lot_id,),
        ).fetchall()
        samples = [
            {"measurement_id": r["measurement_id"], "wavelength_nm": r["wavelength_nm"], "response": r["response"]}
            for r in rows
        ]
        sample_fingerprint = fingerprint(samples)
        stats = responsivity_statistics(
            [item["response"] for item in samples],
            confidence=confidence,
            min_sample_size=min_sample_size,
        )
        report_id = uuid.uuid4().hex
        statistics_block = {
            "count": stats.count,
            "mean": stats.mean,
            "ci_lower": stats.ci_lower,
            "ci_upper": stats.ci_upper,
            "sufficient": stats.sufficient,
            "confidence": stats.confidence,
        }
        report = {
            "report_id": report_id,
            "lot_id": lot_id,
            "metric": "responsivity",
            "generated_at": utcnow(),
            "generated_by": actor.user_id,
            "parameters": {
                "algorithm_version": RESPONSIVITY_REPORT_VERSION,
                "min_sample_size": min_sample_size,
                "confidence": confidence,
                "z_value": stats.z,
            },
            "process_rev": lot["process_rev"],
            "sample_count": stats.count,
            "sample_fingerprint": sample_fingerprint,
            "samples": samples,
            "statistics": statistics_block,
            "status": "ok" if stats.sufficient else "insufficient_sample",
            "note": (
                None
                if stats.sufficient
                else f"样本数 {stats.count} 小于最小要求 {min_sample_size}，均值仅供参考，未计算置信区间"
            ),
        }
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO stat_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report_id, lot_id, "responsivity", RESPONSIVITY_REPORT_VERSION,
                        min_sample_size, confidence, stats.z, stats.count, sample_fingerprint,
                        canonical_json(report), actor.user_id, report["generated_at"],
                    ),
                )
                event(self.db, lot_id, "stat_report", actor.user_id, {
                    "report_id": report_id, "sample_fingerprint": sample_fingerprint,
                    "sample_count": stats.count, "status": report["status"],
                })
        except sqlite3.IntegrityError:
            row = self.db.execute(
                "SELECT report_json FROM stat_reports WHERE lot_id=? AND metric=? "
                "AND sample_fingerprint=? AND confidence=? AND min_sample_size=?",
                (lot_id, "responsivity", sample_fingerprint, confidence, min_sample_size),
            ).fetchone()
            if row:
                return json.loads(row["report_json"])
            raise
        return report

    def list_reports(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        rows = self.db.execute(
            "SELECT report_json FROM stat_reports WHERE lot_id=? ORDER BY created_at,report_id", (lot_id,)
        ).fetchall()
        return [
            {key: doc[key] for key in ("report_id", "lot_id", "metric", "generated_by", "generated_at", "sample_count", "sample_fingerprint", "status")}
            for doc in (json.loads(r["report_json"]) for r in rows)
        ]

    def get_report(self, token: str, lot_id: str, report_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT report_json FROM stat_reports WHERE report_id=? AND lot_id=?", (report_id, lot_id)
        ).fetchone()
        if not row:
            raise KeyError(report_id)
        return json.loads(row["report_json"])

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
