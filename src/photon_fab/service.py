"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import uuid
from typing import Sequence

from .analytics import (
    RESPONSIVITY_ALGORITHM_VERSION,
    confidence_interval,
    fingerprint_measurements,
    responsivity_statistics,
    summarize_spectrum,
    yield_rate,
)
from .auth import Auth
from .storage import connect, event, transaction, utcnow


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

    def create_responsivity_report(self, token: str, lot_id: str) -> dict:
        """基于当前测量生成响应度统计报告并持久化完整快照。

        快照保存当时的测量数据副本、计算参数、数据指纹和生成者；之后新增
        测量或工艺版本变化都不会影响已保存的报告。
        """

        actor = self.auth.require(token, "analyze")
        lot = self.get_lot(token, lot_id)
        rows = self.db.execute(
            "SELECT measurement_id,wavelength_nm,response FROM measurements "
            "WHERE lot_id=? ORDER BY measured_at,measurement_id",
            (lot_id,),
        ).fetchall()
        sample_records = [
            {
                "measurement_id": r["measurement_id"],
                "wavelength_nm": r["wavelength_nm"],
                "response": r["response"],
            }
            for r in rows
        ]
        stats = responsivity_statistics(r["response"] for r in rows)
        data_sha = fingerprint_measurements(sample_records)
        report_id = uuid.uuid4().hex
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO responsivity_reports("
                "report_id,lot_id,algorithm_version,confidence,min_samples,sample_count,"
                "sufficient,mean_response,ci_lower,ci_upper,insufficient_reason,process_rev,"
                "data_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id, lot_id, RESPONSIVITY_ALGORITHM_VERSION, stats.confidence_level, 2,
                    stats.sample_count, int(stats.sufficient), stats.mean, stats.ci_lower,
                    stats.ci_upper, stats.insufficient_reason, lot["process_rev"], data_sha,
                    actor.user_id, now,
                ),
            )
            self.db.executemany(
                "INSERT INTO report_measurements(report_id,measurement_id,wavelength_nm,response,ordinal)"
                " VALUES(?,?,?,?,?)",
                [
                    (report_id, rec["measurement_id"], rec["wavelength_nm"], rec["response"], index)
                    for index, rec in enumerate(sample_records)
                ],
            )
            event(
                self.db, lot_id, "responsivity_report", actor.user_id,
                {"report_id": report_id, "sample_count": stats.sample_count,
                 "sufficient": stats.sufficient, "data_sha256": data_sha},
            )
        return self.get_report(token, report_id)

    def get_report(self, token: str, report_id: str) -> dict:
        """读取已保存的报告；仅返回快照内容，不重新引用 measurements 表。"""

        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM responsivity_reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if not row:
            raise KeyError(report_id)
        samples = [
            {"measurement_id": r["measurement_id"], "wavelength_nm": r["wavelength_nm"],
             "response": r["response"]}
            for r in self.db.execute(
                "SELECT measurement_id,wavelength_nm,response FROM report_measurements "
                "WHERE report_id=? ORDER BY ordinal", (report_id,)
            ).fetchall()
        ]
        return {
            "report_id": report_id,
            "lot_id": row["lot_id"],
            "process_rev": row["process_rev"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "parameters": {
                "algorithm_version": row["algorithm_version"],
                "confidence": row["confidence"],
                "min_samples": row["min_samples"],
            },
            "data_fingerprint": {"sha256": row["data_sha256"], "sample_count": row["sample_count"]},
            "statistics": {
                "sample_count": row["sample_count"],
                "sufficient": bool(row["sufficient"]),
                "mean": row["mean_response"],
                "confidence_level": row["confidence"],
                "ci_lower": row["ci_lower"],
                "ci_upper": row["ci_upper"],
                "insufficient_reason": row["insufficient_reason"],
            },
            "samples": samples,
        }

    def list_reports(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT report_id FROM responsivity_reports WHERE lot_id=? ORDER BY created_at",
            (lot_id,),
        ).fetchall()
        return [self.get_report(token, r["report_id"]) for r in rows]

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
