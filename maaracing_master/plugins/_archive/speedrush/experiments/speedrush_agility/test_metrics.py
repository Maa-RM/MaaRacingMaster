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


    def test_pin_stat_reports_missing_column(self):
        """旧 trace 没有 pin 列：贴边拍照数，有列数为 0，不猜分布。"""
        rows = [{"ts_ns": i * 50_000_000, "sel_why": "pass",
                 "sel_dbg": {"target": 0.1 * i}} for i in range(3)]
        result = M.pin_stat(rows)
        self.assertEqual(result["ticks"], 3)
        self.assertEqual(result["with_pin"], 0)
        self.assertEqual(result["notes"], {})

    def test_pin_stat_counts_same_object_jumps_only(self):
        """同车（命中/保持 id 相同）大跳按当拍锁定结果归类；换车不算。"""
        def row(i, target, hit, pin):
            return {"ts_ns": i * 50_000_000, "sel_why": "pass",
                    "sel_dbg": {"target": target, "hit_oid": hit,
                                "graze_hold": [hit, 1.0], "pin": pin}}
        rows = [row(0, 0.0, 7, "new"), row(1, 0.1, 7, "held"),
                row(2, 0.9, 7, "same"), row(3, 0.0, 8, "held")]
        result = M.pin_stat(rows)
        self.assertEqual(result["ticks"], 4)
        self.assertEqual(result["with_pin"], 4)
        self.assertEqual(result["notes"], {"new": 1, "held": 2, "same": 1})
        self.assertEqual(result["jumps"], 1)
        self.assertEqual(result["jump_notes"], {"same": 1})


    def test_pin_stat_counts_side_break_at_jump(self):
        """输出侧与锁定侧不一致时记违背；同车大跳处单独计数。"""
        rows = [
            {"ts_ns": 0, "sel_why": "pass",
             "sel_dbg": {"target": 0.0, "hit_oid": 7, "graze_hold": [7, 1.0],
                         "pin": "new", "side": 1, "lock": 1}},
            {"ts_ns": 50_000_000, "sel_why": "pass",
             "sel_dbg": {"target": 0.9, "hit_oid": 7, "graze_hold": [7, 1.0],
                         "pin": "na", "side": -1, "lock": 1}},
        ]
        result = M.pin_stat(rows)
        self.assertEqual(result["side_ticks"], 2)
        self.assertEqual(result["side_break"], 1)
        self.assertEqual(result["jumps"], 1)
        self.assertEqual(result["jump_side_break"], 1)

    def test_repeated_round_stage_labels_get_suffix(self):
        """同一会话跑多场：round/stage 重复时标签加出现序号，不产生同名段。"""
        def row(ts):
            return {"round": 1, "stage_id": 1, "ts_ns": ts}
        segs = M.split_segments([row(0), row(50_000_000),
                                 row(2_000_000_000), row(2_050_000_000)])
        self.assertEqual([M.seg_label(s) for s in segs], ["r1s1", "r1s1#2"])


if __name__ == "__main__":
    unittest.main()
