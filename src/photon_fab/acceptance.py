"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    report = service.create_responsivity_report(token, "LOT-DEMO")
    # 报告保存后追加测量，历史报告内容必须保持不变。
    service.add_measurement(token, "LOT-DEMO", 720, .62, .01, "spectrometer-1")
    frozen = service.get_report(token, report["report_id"])
    if frozen["data_fingerprint"]["sha256"] != report["data_fingerprint"]["sha256"] or frozen["statistics"]["sample_count"] != 3:
        raise RuntimeError("历史响应度报告被后续测量污染")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "report_id": report["report_id"],
        "responsivity_mean": report["statistics"]["mean"],
        "responsivity_ci": [report["statistics"]["ci_lower"], report["statistics"]["ci_upper"]],
        "report_samples": frozen["statistics"]["sample_count"],
        "events": len(service.audit(token, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
