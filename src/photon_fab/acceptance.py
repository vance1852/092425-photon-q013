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
    for wavelength, response in ((450, .71), (520, .93)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    thin = service.create_responsivity_report(token, "LOT-DEMO")
    service.add_measurement(token, "LOT-DEMO", 650, .84, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    report = service.create_responsivity_report(token, "LOT-DEMO")
    reread = service.get_report(token, "LOT-DEMO", report["report_id"])
    service.add_measurement(token, "LOT-DEMO", 700, .90, .01, "spectrometer-2")
    frozen = service.get_report(token, "LOT-DEMO", report["report_id"])
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "thin_report": thin["status"],
        "report": report["status"],
        "report_samples": reread["sample_count"],
        "frozen_samples": frozen["sample_count"],
        "reports": len(service.list_reports(token, "LOT-DEMO")),
        "events": len(service.audit(token, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
