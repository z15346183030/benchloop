"""benchloop 的测试。跑法：

    python -m unittest discover -s tests

统计函数都是纯函数，直接喂数字；命令行部分跑真的子进程，但只跑最轻的那种。
"""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import benchloop  # noqa: E402


class TestPercentile(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(benchloop.percentile([5], 0.3), 5.0)

    def test_edges(self):
        vals = [1, 2, 3, 4]
        self.assertEqual(benchloop.percentile(vals, 0.0), 1.0)
        self.assertEqual(benchloop.percentile(vals, 1.0), 4.0)

    def test_interpolates_between_neighbours(self):
        vals = [1, 2, 3, 4]
        self.assertAlmostEqual(benchloop.percentile(vals, 0.5), 2.5)
        self.assertAlmostEqual(benchloop.percentile(vals, 0.25), 1.75)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            benchloop.percentile([], 0.5)


class TestGeomean(unittest.TestCase):
    def test_reciprocal_ratios_cancel(self):
        self.assertAlmostEqual(benchloop.geomean([2.0, 0.5]), 1.0)

    def test_constant(self):
        self.assertAlmostEqual(benchloop.geomean([4.0, 4.0, 4.0]), 4.0)

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            benchloop.geomean([1.0, 0.0])


class TestSummarize(unittest.TestCase):
    def test_basic_numbers(self):
        s = benchloop.summarize([100, 200, 300, 400])
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["mean_ns"], 250.0)
        self.assertAlmostEqual(s["median_ns"], 250.0)
        self.assertAlmostEqual(s["min_ns"], 100)
        self.assertAlmostEqual(s["max_ns"], 400)
        # 样本标准差（n-1），不是总体标准差
        self.assertAlmostEqual(s["stdev_ns"], 129.09944487358058)

    def test_single_sample_has_zero_spread(self):
        s = benchloop.summarize([42])
        self.assertEqual(s["stdev_ns"], 0.0)
        self.assertEqual(s["median_ns"], 42)
        self.assertEqual(s["outliers"], [])

    def test_cv_is_relative_spread(self):
        s = benchloop.summarize([100, 100, 100, 100])
        self.assertEqual(s["cv"], 0.0)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            benchloop.summarize([])


class TestOutliers(unittest.TestCase):
    def test_clean_sample_has_none(self):
        self.assertEqual(benchloop.outlier_indices([100, 101, 99, 102, 98]), [])

    def test_single_spike_is_flagged(self):
        samples = [100, 101, 99, 102, 98, 900]
        self.assertEqual(benchloop.outlier_indices(samples), [5])

    def test_zero_mad_falls_back_to_mean_deviation(self):
        samples = [10, 10, 10, 10, 10, 10, 10, 100]
        self.assertEqual(benchloop.outlier_indices(samples), [7])

    def test_all_identical_has_no_outliers(self):
        self.assertEqual(benchloop.outlier_indices([7, 7, 7, 7]), [])

    def test_two_samples_cannot_be_judged(self):
        self.assertEqual(benchloop.outlier_indices([1, 100]), [])


class TestSignTest(unittest.TestCase):
    def test_all_wins(self):
        self.assertAlmostEqual(benchloop.sign_test_p(10, 10), 2 / 1024, places=6)

    def test_split_is_one(self):
        self.assertEqual(benchloop.sign_test_p(5, 10), 1.0)

    def test_nine_of_ten(self):
        self.assertAlmostEqual(benchloop.sign_test_p(9, 10), 22 / 1024, places=6)

    def test_symmetric(self):
        self.assertAlmostEqual(benchloop.sign_test_p(2, 10), benchloop.sign_test_p(8, 10))

    def test_empty_sample(self):
        self.assertEqual(benchloop.sign_test_p(0, 0), 1.0)


class TestPlanOrder(unittest.TestCase):
    def test_interleaved_alternates_direction(self):
        plan = benchloop.plan_order(2, 3)
        self.assertEqual(
            plan,
            [(0, 0), (0, 1), (1, 1), (1, 0), (2, 0), (2, 1)],
        )

    def test_each_round_runs_every_command_once(self):
        plan = benchloop.plan_order(4, 5)
        self.assertEqual(len(plan), 20)
        for r in range(5):
            self.assertEqual(sorted(i for rr, i in plan if rr == r), [0, 1, 2, 3])

    def test_sequential_groups_by_command(self):
        plan = benchloop.plan_order(2, 3, interleave=False)
        self.assertEqual([i for _, i in plan], [0, 0, 0, 1, 1, 1])

    def test_single_command(self):
        self.assertEqual(benchloop.plan_order(1, 2), [(0, 0), (1, 0)])

    def test_zero_rounds(self):
        self.assertEqual(benchloop.plan_order(3, 0), [])

    def test_rejects_no_commands(self):
        with self.assertRaises(ValueError):
            benchloop.plan_order(0, 3)


class TestFormatting(unittest.TestCase):
    def test_duration_units(self):
        self.assertEqual(benchloop.format_duration(0), "0 ns")
        self.assertEqual(benchloop.format_duration(900), "900 ns")
        self.assertEqual(benchloop.format_duration(1500), "1.50 µs")
        self.assertEqual(benchloop.format_duration(1_500_000), "1.50 ms")
        self.assertEqual(benchloop.format_duration(2_500_000_000), "2.50 s")

    def test_none(self):
        self.assertEqual(benchloop.format_duration(None), "-")

    def test_ratio_speedup(self):
        self.assertEqual(benchloop.format_ratio(2.0), "2.00× 更快")

    def test_ratio_slowdown(self):
        self.assertEqual(benchloop.format_ratio(0.5), "2.00× 更慢")


class TestBootstrap(unittest.TestCase):
    def test_constant_ratios_give_zero_width(self):
        lo, hi = benchloop.bootstrap_ci([2.0] * 8, iterations=200)
        self.assertAlmostEqual(lo, 2.0)
        self.assertAlmostEqual(hi, 2.0)

    def test_ci_brackets_the_estimate(self):
        ratios = [1.9, 2.0, 2.1, 2.05, 1.95, 2.02, 1.98, 2.03]
        lo, hi = benchloop.bootstrap_ci(ratios, iterations=500, seed=1)
        self.assertLess(lo, benchloop.geomean(ratios))
        self.assertGreater(hi, benchloop.geomean(ratios))

    def test_seed_is_reproducible(self):
        ratios = [1.0, 2.0, 4.0, 8.0]
        self.assertEqual(
            benchloop.bootstrap_ci(ratios, iterations=300, seed=7),
            benchloop.bootstrap_ci(ratios, iterations=300, seed=7),
        )

    def test_wider_spread_gives_wider_interval(self):
        tight = benchloop.bootstrap_ci([2.0, 2.02, 1.98, 2.01], iterations=500, seed=3)
        loose = benchloop.bootstrap_ci([1.0, 3.0, 1.2, 2.8], iterations=500, seed=3)
        self.assertGreater(loose[1] - loose[0], tight[1] - tight[0])

    def test_single_ratio(self):
        self.assertEqual(benchloop.bootstrap_ci([1.5]), (1.5, 1.5))


class TestArgv(unittest.TestCase):
    def test_direct_exec_splits_words(self):
        argv = benchloop.build_argv("python -c pass", None)
        self.assertEqual(argv[0], "python")
        self.assertEqual(argv[-1], "pass")

    def test_cmd_uses_slash_c(self):
        argv = benchloop.build_argv("echo hi", r"C:\Windows\system32\cmd.exe")
        self.assertEqual(argv[1], "/c")
        self.assertEqual(argv[2], "echo hi")

    def test_posix_shell_uses_dash_c(self):
        argv = benchloop.build_argv("echo hi", "/bin/sh")
        self.assertEqual(argv, ["/bin/sh", "-c", "echo hi"])

    @unittest.skipUnless(os.name == "nt", "只在 Windows 上有引号保留的问题")
    def test_windows_quoted_path_loses_its_quotes(self):
        # posix=False 会把引号留在 token 里，直接传给 CreateProcess 会变成转义后的字面引号。
        argv = benchloop.build_argv(r'"C:\Program Files\Python\python.exe" -c pass', None)
        self.assertEqual(argv[0], r"C:\Program Files\Python\python.exe")
        self.assertEqual(argv[1:], ["-c", "pass"])


class TestResolveShell(unittest.TestCase):
    def test_none_means_direct(self):
        self.assertIsNone(benchloop.resolve_shell("none"))

    def test_auto_picks_something(self):
        self.assertTrue(benchloop.resolve_shell("auto"))

    def test_unknown_name(self):
        with self.assertRaises(benchloop.ShellError):
            benchloop.resolve_shell("fish")


class TestComparePaired(unittest.TestCase):
    def test_two_times_faster(self):
        c = benchloop.compare_paired([200] * 6, [100] * 6, "#1", "#2", iterations=200)
        self.assertAlmostEqual(c["speedup"], 2.0)
        self.assertEqual(c["wins"], 6)
        self.assertEqual(c["rounds"], 6)
        self.assertLess(c["p_value"], 0.05)

    def test_slower_direction(self):
        c = benchloop.compare_paired([100] * 5, [200] * 5, "#1", "#2", iterations=200)
        self.assertLess(c["speedup"], 1.0)
        self.assertEqual(c["wins"], 0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            benchloop.compare_paired([1, 2], [1], "#1", "#2")


class TestRenderText(unittest.TestCase):
    def render(self, samples, floor_ns):
        summaries = [benchloop.summarize(samples)]
        return benchloop.render_text(
            ["demo"], ["#1"], summaries, [], floor_ns, False, len(samples), 0,
            "auto", [list(samples)],
        )

    def test_warns_when_command_is_close_to_the_floor(self):
        text = self.render([200], 100)
        self.assertIn("spawn floor", text)
        self.assertIn("量到的主要是进程创建", text)

    def test_says_floor_is_minor_when_command_is_far_above(self):
        text = self.render([5000], 100)
        self.assertIn("进程创建只占小头", text)

    def test_no_floor_line_when_probe_skipped(self):
        self.assertNotIn("spawn floor", self.render([5000], None))


class TestCli(unittest.TestCase):
    """真跑子进程，但只跑最便宜的。用 --no-floor 省掉地板探测。"""

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = benchloop.main(argv)
        return code, out.getvalue(), err.getvalue()

    def quick(self, *extra):
        return ["-n", "2", "-w", "0", "--no-floor"] + list(extra)

    def test_single_command_json(self):
        code, out, _ = self.run_cli(
            self.quick("--json", "--shell", "none", sys.executable + " -c pass")
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["tool"], "benchloop")
        self.assertEqual(len(data["commands"]), 1)
        self.assertEqual(len(data["commands"][0]["samples_ns"]), 2)
        self.assertGreater(data["commands"][0]["summary"]["median_ns"], 0)
        self.assertEqual(data["comparisons"], [])

    def test_two_commands_interleaved(self):
        code, out, _ = self.run_cli(
            self.quick(
                "--json",
                "--shell",
                "none",
                sys.executable + " -c pass",
                sys.executable + " -c 0",
            )
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["interleaved"])
        self.assertEqual(len(data["comparisons"]), 1)
        self.assertEqual(data["comparisons"][0]["rounds"], 2)

    def test_sequential_flag(self):
        code, out, _ = self.run_cli(
            self.quick(
                "--json",
                "--sequential",
                "--shell",
                "none",
                sys.executable + " -c pass",
                sys.executable + " -c 0",
            )
        )
        data = json.loads(out)
        self.assertFalse(data["interleaved"])
        self.assertIsNone(data["comparisons"][0]["p_value"])

    def test_failing_command_stops_with_hint(self):
        code, out, err = self.run_cli(
            self.quick("--shell", "none", sys.executable + " -c \"raise SystemExit(4)\"")
        )
        self.assertEqual(code, 3)
        self.assertIn("退出码 4", err)
        self.assertIn("exit 0", err)

    def test_text_output_mentions_median(self):
        code, out, _ = self.run_cli(
            self.quick("--shell", "none", sys.executable + " -c pass")
        )
        self.assertEqual(code, 0)
        self.assertIn("median", out)
        self.assertIn("#1", out)

    def test_quiet_prints_one_line(self):
        code, out, _ = self.run_cli(
            self.quick(
                "--quiet",
                "--shell",
                "none",
                sys.executable + " -c pass",
                sys.executable + " -c 0",
            )
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_zero_runs_rejected(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_cli(["-n", "0", "true"])


class TestRunOnce(unittest.TestCase):
    def test_measures_something_positive(self):
        elapsed, code = benchloop.run_once([sys.executable, "-c", "pass"])
        self.assertEqual(code, 0)
        self.assertGreater(elapsed, 0)

    def test_missing_binary(self):
        with self.assertRaises(benchloop.ShellError):
            benchloop.run_once(["definitely-not-a-real-binary-xyz"])


if __name__ == "__main__":
    unittest.main()
