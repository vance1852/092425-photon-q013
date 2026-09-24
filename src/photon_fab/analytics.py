"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

RESPONSIVITY_ALGORITHM_VERSION = "photon-responsivity-report/1"

# 双侧 95% 学生化 t 临界值（自由度 1..30）；自由度大于 30 时退回 z=1.96 近似。
T_CRITICAL_095 = {
    1: 12.7062, 2: 4.3027, 3: 3.1824, 4: 2.7764, 5: 2.5706,
    6: 2.4469, 7: 2.3646, 8: 2.3060, 9: 2.2622, 10: 2.2281,
    11: 2.2010, 12: 2.1788, 13: 2.1604, 14: 2.1448, 15: 2.1314,
    16: 2.1199, 17: 2.1098, 18: 2.1009, 19: 2.0930, 20: 2.0860,
    21: 2.0796, 22: 2.0739, 23: 2.0687, 24: 2.0639, 25: 2.0595,
    26: 2.0555, 27: 2.0518, 28: 2.0484, 29: 2.0452, 30: 2.0423,
}
Z_CRITICAL_095 = 1.96


@dataclass(frozen=True)
class SpectrumSummary:
    count: int
    peak_wavelength_nm: float
    peak_response: float
    mean_response: float
    noise_rms: float
    pass_band_nm: tuple[float, float]


@dataclass(frozen=True)
class ResponsivityStatistics:
    """响应度统计结果；样本不足时置信区间为 None，绝不伪造精度。"""

    sample_count: int
    sufficient: bool
    mean: float | None
    confidence_level: float
    ci_lower: float | None
    ci_upper: float | None
    insufficient_reason: str | None

    def as_dict(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "sufficient": self.sufficient,
            "mean": self.mean,
            "confidence_level": self.confidence_level,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "insufficient_reason": self.insufficient_reason,
        }


def _pairs(wavelengths: Sequence[float], response: Sequence[float]) -> list[tuple[float, float]]:
    if len(wavelengths) != len(response) or len(wavelengths) < 3:
        raise ValueError("at least three wavelength/response pairs are required")
    pairs = sorted((float(w), float(r)) for w, r in zip(wavelengths, response))
    if any(not math.isfinite(w) or not math.isfinite(r) for w, r in pairs):
        raise ValueError("measurements must be finite")
    return pairs


def summarize_spectrum(wavelengths: Sequence[float], response: Sequence[float], threshold: float = 0.8) -> SpectrumSummary:
    pairs = _pairs(wavelengths, response)
    peak_w, peak_r = max(pairs, key=lambda p: p[1])
    values = [r for _, r in pairs]
    mean = statistics.fmean(values)
    noise = math.sqrt(statistics.fmean((r - mean) ** 2 for r in values))
    band = [w for w, r in pairs if r >= peak_r * threshold]
    return SpectrumSummary(len(pairs), peak_w, peak_r, mean, noise, (min(band), max(band)))


def confidence_interval(values: Iterable[float], confidence: float = 0.95) -> tuple[float, float]:
    data = [float(v) for v in values]
    if not data or not 0 < confidence < 1:
        raise ValueError("values and confidence are invalid")
    mean = statistics.fmean(data)
    if len(data) == 1:
        return mean, mean
    z = 1.96 if confidence >= 0.95 else 1.645
    margin = z * statistics.stdev(data) / math.sqrt(len(data))
    return mean - margin, mean + margin


def yield_rate(total: int, passed: int, rejected: int = 0) -> dict[str, float]:
    if total <= 0 or passed < 0 or rejected < 0 or passed + rejected > total:
        raise ValueError("inconsistent lot counts")
    return {"yield": passed / total, "reject_rate": rejected / total, "unknown_rate": (total - passed - rejected) / total}


def responsivity(current_ma: float, optical_power_mw: float) -> float:
    if optical_power_mw <= 0:
        raise ValueError("optical power must be positive")
    return current_ma / optical_power_mw


def t_critical(degrees_of_freedom: int, confidence: float) -> float:
    if confidence != 0.95:
        raise ValueError("only 0.95 confidence is tabulated")
    if degrees_of_freedom < 1:
        raise ValueError("degrees of freedom must be positive")
    return T_CRITICAL_095.get(degrees_of_freedom, Z_CRITICAL_095)


def responsivity_statistics(
    values: Iterable[float], confidence: float = 0.95, min_samples: int = 2
) -> ResponsivityStatistics:
    """计算响应度均值与置信区间。

    样本数低于 ``min_samples`` 或无法估计离散度（零方差）时，明确标记为
    insufficient 且不返回置信区间，避免用零宽区间伪造精度。
    """

    data = [float(v) for v in values]
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    if len(data) < min_samples:
        return ResponsivityStatistics(
            len(data),
            False,
            None,
            confidence,
            None,
            None,
            f"at least {min_samples} samples are required",
        )
    mean = statistics.fmean(data)
    if not math.isfinite(mean):
        raise ValueError("measurements must be finite")
    stddev = statistics.stdev(data)
    if stddev <= 0:
        # 方差为零时经典 t 区间退化成点值，这同样不是真实精度：标记为不足。
        return ResponsivityStatistics(
            len(data),
            False,
            mean,
            confidence,
            None,
            None,
            "zero sample variance: confidence interval is undefined",
        )
    margin = t_critical(len(data) - 1, confidence) * stddev / math.sqrt(len(data))
    return ResponsivityStatistics(
        len(data), True, mean, confidence, mean - margin, mean + margin, None
    )


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本，用于数据指纹。"""

    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def fingerprint_measurements(records: Iterable[Mapping[str, object]]) -> str:
    """对规范化后的测量记录（含编号与响应度）逐行计算 SHA-256 指纹。"""

    digest = hashlib.sha256()
    for record in records:
        digest.update(canonical_json(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
