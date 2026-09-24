from __future__ import annotations

import math
import statistics
import unittest

from photon_fab.analytics import (
    MIN_RESPONSIVITY_SAMPLES,
    confidence_interval,
    responsivity_statistics,
)


class ResponsivityStatisticsTests(unittest.TestCase):
    def test_empty_sample_is_marked_insufficient_without_fabrication(self) -> None:
        result = responsivity_statistics([])
        self.assertEqual(result.count, 0)
        self.assertIsNone(result.mean)
        self.assertIsNone(result.ci_lower)
        self.assertIsNone(result.ci_upper)
        self.assertFalse(result.sufficient)

    def test_too_few_samples_get_marker_not_fake_interval(self) -> None:
        for size, values in ((1, [0.82]), (2, [0.82, 0.84])):
            with self.subTest(size=size):
                result = responsivity_statistics(values)
                self.assertEqual(result.count, size)
                self.assertAlmostEqual(result.mean, statistics.fmean(values))
                self.assertIsNone(result.ci_lower)
                self.assertIsNone(result.ci_upper)
                self.assertFalse(result.sufficient)

    def test_three_samples_produce_95_percent_interval(self) -> None:
        values = [0.71, 0.93, 0.84]
        result = responsivity_statistics(values)
        self.assertEqual(result.count, 3)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.confidence, 0.95)
        self.assertEqual(result.z, 1.96)
        mean = statistics.fmean(values)
        self.assertAlmostEqual(result.mean, mean)
        margin = 1.96 * statistics.stdev(values) / math.sqrt(3)
        self.assertAlmostEqual(result.ci_lower, mean - margin)
        self.assertAlmostEqual(result.ci_upper, mean + margin)
        self.assertLess(result.ci_lower, mean)
        self.assertLess(mean, result.ci_upper)

    def test_custom_min_sample_size_and_confidence(self) -> None:
        values = [0.8, 0.82]
        result = responsivity_statistics(values, min_sample_size=2, confidence=0.90)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.z, 1.645)
        self.assertIsNotNone(result.ci_lower)

    def test_non_finite_values_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_statistics([0.8, float("nan"), 0.9])

    def test_min_sample_size_must_be_at_least_two(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_statistics([0.8], min_sample_size=1)

    def test_default_threshold_is_three(self) -> None:
        self.assertEqual(MIN_RESPONSIVITY_SAMPLES, 3)

    def test_confidence_interval_needs_data(self) -> None:
        with self.assertRaises(ValueError):
            confidence_interval([])


if __name__ == "__main__":
    unittest.main()
