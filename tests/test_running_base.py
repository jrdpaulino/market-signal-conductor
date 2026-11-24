import os
import unittest
from unittest import mock

from msc import version
from msc.backfill import signal_errors
from msc.execution.gate import running_base_brier, track_record
from msc.store import Store


class RunningBaseRateTests(unittest.TestCase):
    def test_uses_only_outcomes_already_known(self):
        # forecast on day 1 can't see its own outcome (exit day 5) -> says 0.5
        rows = [("d01", "d05", 1), ("d02", "d06", 1), ("d07", "d11", 0)]
        # d1: 0.5 -> .25; d2: 0.5 -> .25; d7: known outcomes d5,d6 = both 1 -> base 1.0 vs 0 -> 1.0
        self.assertAlmostEqual(running_base_brier(rows), (0.25 + 0.25 + 1.0) / 3)

    def test_track_record_reports_it_and_gate_benchmark_unchanged(self):
        fc = [(f"2026-01-{i:02d}", 20, 0.6, "x") for i in range(1, 11)]
        t = track_record(fc, lambda d, h: 1, exit_of=lambda d, h: "2026-02-01")
        self.assertAlmostEqual(t["brier_base"], 0.0)                 # hindsight base rate is perfect when all ups
        self.assertAlmostEqual(t["brier_running"], 0.25)             # nothing known yet at forecast time
        self.assertIn("brier_running", track_record(fc, lambda d, h: None))

    def test_without_exit_days_no_running_figure(self):
        self.assertIsNone(track_record([("d", 20, 0.5, "x")], lambda d, h: 1)["brier_running"])


class VersionAndErrorsTests(unittest.TestCase):
    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"MSC_COMMIT": "abc1234"}):
            self.assertEqual(version.commit_id(), "abc1234")

    def test_never_raises(self):
        with mock.patch.dict(os.environ, {"MSC_COMMIT": ""}), mock.patch("subprocess.run", side_effect=OSError):
            self.assertEqual(version.commit_id(), "unknown")

    def test_signal_errors_grouped(self):
        s = Store(":memory:")
        s.set_health("2026-01-01", "sig", "error", 0, "RuntimeError: need 250 observations, have 10")
        s.set_health("2026-01-02", "sig", "error", 0, "RuntimeError: need 250 observations, have 10")
        s.set_health("2026-01-03", "sig", "ok", 1)
        out = signal_errors(s)
        self.assertEqual(out["sig"][0]["days"], 2)
        self.assertEqual((out["sig"][0]["first"], out["sig"][0]["last"]), ("2026-01-01", "2026-01-02"))


if __name__ == "__main__":
    unittest.main()
