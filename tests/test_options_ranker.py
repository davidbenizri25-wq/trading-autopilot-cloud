from __future__ import annotations

import copy
import math
import unittest
from datetime import datetime, timezone

from options_ranker import rank_option_contracts


NOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


def context(**overrides):
    value = {
        "verdict": "ENTER",
        "setup_state": "ENTER",
        "direction": "bullish",
        "underlying_ticker": "XYZ",
        "underlying_price": 102.0,
        "expected_holding_days": 5,
        "target_price": 115.0,
        "earnings_date": None,
        "earnings_policy": "avoid",
        "chain_complete": True,
    }
    value.update(overrides)
    return value


def contract(**overrides):
    value = {
        "underlying_ticker": "XYZ",
        "contract_symbol": "XYZ260821C00100000",
        "option_type": "call",
        "expiration": "2026-08-21",
        "strike": 100.0,
        "bid": 4.0,
        "ask": 4.2,
        "volume": 1_500,
        "open_interest": 5_000,
        "implied_volatility": 0.35,
        "delta": 0.58,
        "gamma": 0.04,
        "theta": -0.05,
        "vega": 0.13,
        "snapshot_timestamp": "2026-07-13T14:55:00Z",
    }
    value.update(overrides)
    return value


class OptionRankerTests(unittest.TestCase):
    def test_enter_recommends_best_complete_contract_and_calculates_outputs(self):
        less_aligned = contract(
            contract_symbol="XYZ260821C00105000",
            strike=105.0,
            bid=2.6,
            ask=2.9,
            volume=600,
            open_interest=2_100,
            delta=0.39,
            theta=-0.04,
        )
        result = rank_option_contracts([less_aligned, contract()], context(), now=NOW)

        self.assertEqual(result["status"], "RECOMMEND")
        self.assertEqual(result["recommendation"]["contract_symbol"], "XYZ260821C00100000")
        best = result["ranked_contracts"][0]
        self.assertEqual(best["call_put"], "call")
        self.assertEqual(best["expiration"], "2026-08-21")
        self.assertEqual(best["dte"], 39)
        self.assertEqual(best["mid"], 4.1)
        self.assertEqual(best["mid_source"], "calculated_from_bid_ask")
        self.assertEqual(best["spread_dollars"], 0.2)
        self.assertAlmostEqual(best["spread_pct"], 0.2 / 4.1, places=6)
        self.assertEqual(best["breakeven"], 104.1)
        self.assertEqual(best["liquidity_quality"], "excellent")
        self.assertTrue(best["recommendation_eligible"])
        self.assertFalse(best["rejection_reasons"])
        self.assertNotIn("iv_rank", best)

        expected_amount = 102.0 * 0.35 * math.sqrt(39 / 365.0)
        self.assertTrue(best["expected_move"]["estimated"])
        self.assertEqual(best["expected_move"]["amount"], round(expected_amount, 4))
        self.assertIn("implied_volatility", best["expected_move"]["method"])
        self.assertGreater(best["ranked_score"], result["ranked_contracts"][1]["ranked_score"])
        self.assertTrue(result["ranked_contracts"][1]["why_ranked_lower"])

    def test_iv_rank_is_only_present_when_it_is_actually_supplied(self):
        without_rank = rank_option_contracts([contract()], context(), now=NOW)["ranked_contracts"][0]
        with_rank = rank_option_contracts(
            [contract(iv_rank=64.0)], context(), now=NOW
        )["ranked_contracts"][0]

        self.assertNotIn("iv_rank", without_rank)
        self.assertEqual(with_rank["iv_rank"], 64.0)
        self.assertEqual(with_rank["iv_rank_source"], "snapshot")

    def test_armed_returns_provisional_ranking_but_never_a_recommendation(self):
        result = rank_option_contracts(
            [contract()],
            context(verdict="WAIT FOR CONFIRMATION", setup_state="ARMED"),
            now=NOW,
        )

        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["recommendation"])
        self.assertTrue(result["provisional_rankings"])
        self.assertEqual(result["provisional_leader"]["label"], "provisional only; not a recommendation")
        ranked = result["ranked_contracts"][0]
        self.assertTrue(ranked["contract_quality_passes"])
        self.assertTrue(ranked["provisional"])
        self.assertFalse(ranked["recommendation_eligible"])
        self.assertIn(
            "underlying setup is ARMED; wait for ENTER confirmation",
            ranked["rejection_reasons"],
        )

    def test_non_enter_non_armed_is_a_hard_pass(self):
        result = rank_option_contracts(
            [contract()],
            context(verdict="PASS", setup_state="BLOCKED"),
            now=NOW,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["recommendation"])
        self.assertIsNone(result["provisional_leader"])
        self.assertIn("underlying verdict is not ENTER", result["rejection_reasons"])

    def test_each_required_safety_gate_blocks_the_contract(self):
        cases = {
            "excessive spread": contract(bid=2.0, ask=4.0),
            "thin volume": contract(volume=99),
            "thin open interest": contract(open_interest=499),
            "unreasonable theta": contract(theta=-0.30),
            "stale": contract(snapshot_timestamp="2026-07-13T14:00:00Z"),
        }
        for expected_reason, snapshot in cases.items():
            with self.subTest(expected_reason=expected_reason):
                result = rank_option_contracts([snapshot], context(), now=NOW)
                ranked = result["ranked_contracts"][0]
                self.assertEqual(result["status"], "PASS")
                self.assertFalse(ranked["contract_quality_passes"])
                self.assertFalse(ranked["recommendation_eligible"])
                self.assertTrue(
                    any(expected_reason in reason for reason in ranked["rejection_reasons"]),
                    ranked["rejection_reasons"],
                )

    def test_incompatible_earnings_and_incomplete_chain_are_hard_blocks(self):
        earnings_result = rank_option_contracts(
            [contract()],
            context(earnings_date="2026-08-03", earnings_policy="avoid"),
            now=NOW,
        )
        earnings_ranked = earnings_result["ranked_contracts"][0]
        self.assertTrue(earnings_ranked["earnings_exposure"]["exposed"])
        self.assertFalse(earnings_ranked["earnings_exposure"]["compatible"])
        self.assertIn("contract crosses earnings under avoid policy", earnings_ranked["rejection_reasons"])

        incomplete = contract()
        del incomplete["gamma"]
        incomplete_result = rank_option_contracts([incomplete], context(), now=NOW)
        incomplete_ranked = incomplete_result["ranked_contracts"][0]
        self.assertTrue(
            any(
                reason == "chain data incomplete: missing gamma"
                for reason in incomplete_ranked["rejection_reasons"]
            )
        )
        self.assertEqual(incomplete_result["status"], "PASS")

    def test_mismatched_or_missing_underlying_can_never_be_recommended(self):
        mismatched = rank_option_contracts(
            [contract(underlying_ticker="MSFT")], context(), now=NOW
        )
        self.assertEqual(mismatched["status"], "PASS")
        self.assertIn(
            "contract underlying does not match the analyzed security",
            mismatched["ranked_contracts"][0]["rejection_reasons"],
        )

        missing = contract()
        del missing["underlying_ticker"]
        missing_result = rank_option_contracts([missing], context(), now=NOW)
        self.assertEqual(missing_result["status"], "PASS")
        self.assertTrue(
            any(
                "missing underlying_ticker" in reason
                for reason in missing_result["ranked_contracts"][0]["rejection_reasons"]
            )
        )

    def test_missing_earnings_key_can_never_be_recommended(self):
        missing_earnings = context()
        del missing_earnings["earnings_date"]
        result = rank_option_contracts([contract()], missing_earnings, now=NOW)
        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["recommendation"])
        self.assertIn("earnings date unavailable", result["rejection_reasons"])

    def test_put_has_put_breakeven_and_bearish_fit(self):
        put = contract(
            contract_symbol="XYZ260821P00105000",
            option_type="put",
            strike=105.0,
            delta=-0.57,
        )
        result = rank_option_contracts(
            [put],
            context(direction="bearish", target_price=93.0),
            now=NOW,
        )

        ranked = result["ranked_contracts"][0]
        self.assertEqual(result["status"], "RECOMMEND")
        self.assertEqual(ranked["call_put"], "put")
        self.assertEqual(ranked["breakeven"], 100.9)
        self.assertIn("put matches the bearish underlying thesis", ranked["fit_rationale"])

    def test_inputs_are_not_mutated_and_ties_are_deterministic(self):
        later_symbol = contract(contract_symbol="ZZZ")
        earlier_symbol = contract(contract_symbol="AAA")
        snapshots = [later_symbol, earlier_symbol]
        original_snapshots = copy.deepcopy(snapshots)
        original_context = context()
        copied_context = copy.deepcopy(original_context)

        first = rank_option_contracts(snapshots, original_context, now=NOW)
        second = rank_option_contracts(snapshots, original_context, now=NOW)

        self.assertEqual(first, second)
        self.assertEqual(snapshots, original_snapshots)
        self.assertEqual(original_context, copied_context)
        self.assertEqual(
            [item["contract_symbol"] for item in first["ranked_contracts"]],
            ["AAA", "ZZZ"],
        )

    def test_no_contracts_and_invalid_policy_fail_closed(self):
        empty = rank_option_contracts([], context(), now=NOW)
        self.assertEqual(empty["status"], "PASS")
        self.assertIn("options chain contains no contracts", empty["rejection_reasons"])

        with self.assertRaises(ValueError):
            rank_option_contracts(
                [contract()],
                context(),
                now=NOW,
                policy={"max_spread_pct": 0},
            )


if __name__ == "__main__":
    unittest.main()
