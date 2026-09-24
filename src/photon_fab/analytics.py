"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SpectrumSummary:
    count: int
    peak_wavelength_nm: float
    peak_response: float
    mean_response: float
    noise_rms: float
    pass_band_nm: tuple[float, float]


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


RESPONSIVITY_REPORT_VERSION = "responsivity-report/1"
MIN_RESPONSIVITY_SAMPLES = 3


def _z_for(confidence: float) -> float:
    if not 0 < confidence < 1:
        raise ValueError("confidence must lie between 0 and 1")
    return 1.96 if confidence >= 0.95 else 1.645


def confidence_interval(values: Iterable[float], confidence: float = 0.95) -> tuple[float, float]:
    data = [float(v) for v in values]
    if not data:
        raise ValueError("values are invalid")
    mean = statistics.fmean(data)
    if len(data) == 1:
        return mean, mean
    margin = _z_for(confidence) * statistics.stdev(data) / math.sqrt(len(data))
    return mean - margin, mean + margin


@dataclass(frozen=True)
class ResponsivityStatistics:
    count: int
    mean: float | None
    ci_lower: float | None
    ci_upper: float | None
    sufficient: bool
    confidence: float
    z: float


def responsivity_statistics(
    values: Iterable[float],
    *,
    confidence: float = 0.95,
    min_sample_size: int = MIN_RESPONSIVITY_SAMPLES,
) -> ResponsivityStatistics:
    """计算响应度均值与 95% 置信区间；样本不足时只标记、不伪造区间精度。"""
    if min_sample_size < 2:
        raise ValueError("min_sample_size must be at least 2")
    z = _z_for(confidence)
    data = [float(v) for v in values]
    if any(not math.isfinite(v) for v in data):
        raise ValueError("measurements must be finite")
    count = len(data)
    mean = statistics.fmean(data) if data else None
    sufficient = count >= min_sample_size
    if sufficient:
        lower, upper = confidence_interval(data, confidence)
    else:
        lower = upper = None
    return ResponsivityStatistics(count, mean, lower, upper, sufficient, confidence, z)


def yield_rate(total: int, passed: int, rejected: int = 0) -> dict[str, float]:
    if total <= 0 or passed < 0 or rejected < 0 or passed + rejected > total:
        raise ValueError("inconsistent lot counts")
    return {"yield": passed / total, "reject_rate": rejected / total, "unknown_rate": (total - passed - rejected) / total}


def responsivity(current_ma: float, optical_power_mw: float) -> float:
    if optical_power_mw <= 0:
        raise ValueError("optical power must be positive")
    return current_ma / optical_power_mw
