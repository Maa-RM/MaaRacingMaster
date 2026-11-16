"""验证层的手算边界样本；只在实验区运行，不由生产测试加载。"""
import math
import unittest

import metrics as M


class MetricsChecks(unittest.TestCase):
    def test_even_median(self):
        self.assertEqual(M.RevStat(theta=.1, runs=[2, 4]).median_run_ticks, 3)

    def test_nonfinite_is_missing(self):
        self.assertIsNone(M.num(math.nan))
        self.assertIsNone(M.num(math.inf))

    def test_car_identity_does_not_match_coin_group(self):
        coin = ["coin", 0, 1, 0, 0, 0, "pixel", 0, 0, 0, 0, 0, 0, 0, 7, True]
        car = ["car", 2, 1, 0, 0, 0, "metric", 0, 0, 0, 0, 0, 0, 0, 7, True]
        self.assertEqual(M.scene_map({"scene": {"objs": [coin, car]}})[7], car)

    def test_rebase_does_not_create_reversal(self):
        rows = [{"ts_ns": i * 50_000_000, "x": x,
                 "lat_ctrl": {"rebase": "jump_confirm" if i == 2 else None}}
                for i, x in enumerate([0, 1, -1, -2])]
        stat = M.rev_stat(rows, lambda r: r["x"], .2)
        self.assertEqual(stat.count, 0)
        self.assertEqual(stat.breaks, 1)
        self.assertAlmostEqual(stat.span_s, .1)

    def test_hysteresis_retains_small_steps_and_flat_samples(self):
        self.assertEqual(M.reversals([0, .1, .2, .2, .1, 0], .2)[0], 1)
        self.assertEqual(M.reversals([0, .1, .2, .3], .2)[0], 0)

    def test_reference_epoch_does_not_create_reversal(self):
        rows = [{"ts_ns": i * 50_000_000, "x": x, "route": {"ep": i // 2}}
                for i, x in enumerate([0, 1, -1, -2])]
        self.assertEqual(M.rev_stat(rows, lambda r: r["x"], .2).count, 0)

    def test_hold_ends_at_next_zero_for_both_sampling_rates(self):
        self.assertTrue(callable(getattr(M, "stick_stat", None)), "缺少统一杆值时间算法")
        slow = [(0, 3000), (100_000_000, 3000), (200_000_000, 0)]
        fast = [(0, 3000), (50_000_000, 3000), (100_000_000, 3000),
                (150_000_000, 3000), (200_000_000, 0)]
        for samples in (slow, fast):
            self.assertEqual(M.stick_stat(samples)["hold_med_ms"], 200)
            self.assertEqual(M.stick_stat(samples)["censored_boundaries"], 1)

    def test_missing_and_gap_do_not_extend_hold(self):
        self.assertTrue(callable(getattr(M, "stick_stat", None)), "缺少统一杆值时间算法")
        samples = [(0, 3000), (50_000_000, None), (100_000_000, 3000),
                   (150_000_000, 0), (1_000_000_000, 3000)]
        result = M.stick_stat(samples)
        self.assertEqual(result["hold_med_ms"], 50)
        self.assertAlmostEqual(result["span_s"], .05)

    def test_request_never_uses_execution_coordinates(self):
        self.assertTrue(callable(getattr(M, "request_stat", None)), "缺少请求统计定义")
        rows = [{"executed_lane": 0, "sel_dbg": {"rw": [[2, 2]]}}]
        self.assertEqual(M.request_stat(rows)["ticks"], 0)
        self.assertEqual(M.request_stat(rows)["unobserved"], 1)

    def test_geometry_accounts_for_compensation_and_missing_components(self):
        def row(i, cd):
            obj = ["car", 1 - (cd or 0), 1, 0, 0, 0, "metric",
                   0, 0, 0, 0, 0, 0, 0, 7, True]
            return {"round": 1, "stage_id": 1, "fid": i, "ts_ns": i * 50_000_000,
                    "sel_dbg": {"hit_oid": 7, "origin": 0, "fshift": 0},
                    "scene": {"objs": [obj]},
                    "carsrc": [{"id": 7, "xr": 1, "cd": cd, "origin": 0, "x": obj[1]}]}
        result = M.geometry_stat([row(0, 0), row(1, 2), row(2, None), row(3, 4)])
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["groups"], {"cd_only": 1})
        self.assertEqual(result["err_max"], 0)


if __name__ == "__main__":
    unittest.main()
