from __future__ import annotations

import unittest
from pathlib import Path

from scoring import grade_for_score, load_candidates_csv, rank_candidates


ROOT = Path(__file__).resolve().parents[1]


class ScoringFreezeTests(unittest.TestCase):
    def test_grade_thresholds_are_frozen(self) -> None:
        cases = {
            85: "A+",
            80: "A",
            75: "A-",
            70: "B+",
            65: "B",
            50: "C",
            49: "F",
        }
        for score, grade in cases.items():
            with self.subTest(score=score):
                self.assertEqual(grade_for_score(score), grade)

    def test_sample_ranking_output_is_unchanged(self) -> None:
        ranked = rank_candidates(load_candidates_csv(ROOT / "data" / "sample_candidates.csv"))
        actual = [(row["ticker"], row["bias"], row["score"], row["grade"], row["state"]) for row in ranked]
        expected = [
            ("BEAR", "bearish", 100, "A+", "priority_watch"),
            ("MSFT", "bearish", 100, "A+", "priority_watch"),
            ("ES1!", "context", 95, "A+", "context_watch"),
            ("CONFLICT", "bullish", 90, "A+", "alert"),
            ("SPCE", "bullish", 90, "A+", "priority_watch"),
            ("BTCUSD", "context", 87, "A+", "context_watch"),
            ("AI", "bullish", 82, "A", "priority_watch"),
        ]
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()

