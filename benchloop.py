#!/usr/bin/env python3
"""benchloop - 交错跑 A/B，用配对数据算加速比。

一条命令量自己，多条命令交替跑。所有命令每一轮轮流执行一次，
所以机器变慢（温度、后台进程、内存回收）时，A 和 B 一起变慢，
比较结果不受影响。

只用标准库，Python 3.9+。
"""

import argparse
import json
import math
import os
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import time

__version__ = "1.0.0"

# 修改 Z 分数超过这个值就标记为离群。3.5 是 Iglewicz & Hoaglin (1993) 建议的阈值。
OUTLIER_Z = 3.5

# 离群标记不参与计算，只用于展示，所以阈值可以收紧一点。


# ---------------------------------------------------------------------------
# 统计：全部是纯函数，输入输出都是数字，方便单独测
# ---------------------------------------------------------------------------


def percentile(values, p):
    """线性插值分位数。values 必须已排序，p 取 0..1。"""
    if not values:
        raise ValueError("percentile() 需要至少一个值")
    if len(values) == 1:
        return float(values[0])
    if p <= 0:
        return float(values[0])
    if p >= 1:
        return float(values[-1])
    pos = (len(values) - 1) * p
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return float(values[lo]) + (float(values[hi]) - float(values[lo])) * frac


def median(values):
    return percentile(sorted(values), 0.5)


def geomean(values):
    """几何平均。比率类数据用它，因为 2× 和 0.5× 应该互相抵消。"""
    if not values:
        raise ValueError("geomean() 需要至少一个值")
    total = 0.0
    for v in values:
        if v <= 0:
            raise ValueError("geomean() 只接受正数")
        total += math.log(v)
    return math.exp(total / len(values))


def summarize(samples):
    """把一组纳秒样本压成描述性统计。

    samples 是整数纳秒，返回值里除了常规统计，还带离群点下标。
    """
    if not samples:
        raise ValueError("summarize() 需要至少一个样本")
    n = len(samples)
    ordered = sorted(samples)
    mean = sum(samples) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in samples) / (n - 1)
        stdev = math.sqrt(var)
    else:
        stdev = 0.0
    return {
        "n": n,
        "mean_ns": mean,
        "median_ns": median(samples),
        "stdev_ns": stdev,
        "min_ns": ordered[0],
        "max_ns": ordered[-1],
        "p05_ns": percentile(ordered, 0.05),
        "p95_ns": percentile(ordered, 0.95),
        "cv": (stdev / mean) if mean else 0.0,
        "outliers": outlier_indices(samples),
    }


def modified_zscores(samples):
    """修改 Z 分数：用中位数和 MAD，抗离群点，不会被一个尖峰带跑。"""
    n = len(samples)
    if n < 3:
        return [0.0] * n
    med = median(samples)
    devs = [abs(x - med) for x in samples]
    mad = median(devs)
    if mad == 0:
        # 一半以上的样本完全一样时 MAD 会退化成 0，改用平均绝对偏差。
        mad = sum(devs) / n
        if mad == 0:
            return [0.0] * n
    return [0.6745 * (x - med) / mad for x in samples]


def outlier_indices(samples):
    """返回疑似离群样本的下标。只做标记，不剔除，也不改均值。"""
    scores = modified_zscores(samples)
    return [i for i, s in enumerate(scores) if abs(s) > OUTLIER_Z]


def sign_test_p(wins, n):
    """符号检验的双侧 p 值：n 轮里至少 wins 轮同一个方向，纯靠运气的概率。

    精确二项分布，不用正态近似——样本量本来就小。
    """
    if n <= 0:
        return 1.0
    k = max(wins, n - wins)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def bootstrap_ci(ratios, iterations=2000, seed=0, alpha=0.05):
    """对几何平均做百分位 bootstrap，返回 (下界, 上界)。

    重采样比率而不是时间，因为比率的分布本来就偏，几何平均更合适。
    固定种子，同一组数据每次跑出同一个区间。
    """
    if not ratios:
        raise ValueError("bootstrap_ci() 需要至少一个比率")
    if len(ratios) == 1:
        return (float(ratios[0]), float(ratios[0]))
    rng = random.Random(seed)
    n = len(ratios)
    estimates = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += math.log(ratios[rng.randrange(n)])
        estimates.append(math.exp(total / n))
    estimates.sort()
    return (percentile(estimates, alpha / 2), percentile(estimates, 1 - alpha / 2))


# ---------------------------------------------------------------------------
# 执行顺序：交错是核心，独立成函数才测得住
# ---------------------------------------------------------------------------


def plan_order(n_commands, rounds, interleave=True):
    """返回 [(轮次, 命令下标), ...]，就是这个进程要按顺序跑的全部单元。

    交错模式下每轮把每条命令各跑一次，并且单数轮反序（A,B,B,A），
    这样「排在前面的那次」不会总是同一条命令。
    """
    if n_commands < 1:
        raise ValueError("至少要有一条命令")
    if not interleave:
        # 顺序模式：一条命令的全部轮次跑完，再跑下一条。
        return [(r, idx) for idx in range(n_commands) for r in range(rounds)]
    order = []
    for r in range(rounds):
        indices = list(range(n_commands))
        if r % 2 == 1:
            indices.reverse()
        for idx in indices:
            order.append((r, idx))
    return order


# ---------------------------------------------------------------------------
# shell 解析与执行
# ---------------------------------------------------------------------------


class ShellError(Exception):
    pass


def resolve_shell(name):
    """把 --shell 的值变成一个可执行文件路径；none 返回 None 表示直接 exec。"""
    if name == "none":
        return None
    if name == "auto":
        if os.name == "nt":
            return os.environ.get("COMSPEC", "cmd.exe")
        return "/bin/sh"
    if name == "cmd":
        if os.name != "nt":
            raise ShellError("--shell cmd 只在 Windows 上可用，这台机器是 " + os.name)
        return os.environ.get("COMSPEC", "cmd.exe")
    if name in ("bash", "sh"):
        found = shutil.which(name)
        if not found:
            raise ShellError(
                "找不到 %s，换个 shell 或者用 --shell none 直接执行命令" % name
            )
        return found
    raise ShellError("不认识的 shell：%s（可选 auto / none / cmd / bash / sh）" % name)


def is_cmd_exe(shell_path):
    return os.path.basename(shell_path).lower() in ("cmd.exe", "cmd")


def build_argv(command, shell_path):
    """把一条命令行变成 argv。不经过 shell=True，引号行为可预期。"""
    if shell_path is None:
        tokens = shlex.split(command, posix=(os.name != "nt"))
        if os.name == "nt":
            # Windows 下 posix=False 会把引号留在 token 里，自己剥掉。
            tokens = [
                t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t
                for t in tokens
            ]
        return tokens
    if is_cmd_exe(shell_path):
        return [shell_path, "/c", command]
    return [shell_path, "-c", command]


def run_once(argv, timeout=None, capture=True):
    """跑一次，返回纳秒。stdout/stderr 默认丢掉，免得终端输出拖慢计时。"""
    devnull = subprocess.DEVNULL if capture else None
    start = time.perf_counter_ns()
    try:
        proc = subprocess.run(
            argv,
            stdout=devnull,
            stderr=devnull,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ShellError("命令找不到：" + " ".join(argv))
    elapsed = time.perf_counter_ns() - start
    return elapsed, proc.returncode


def measure_floor(shell_path, rounds=5):
    """量一下「什么都不做」要多久：这是这台机器上任何计时的地板。

    命令本身比地板还短的时候，量到的主要是进程创建开销。
    """
    if shell_path is None:
        return None
    probe = "exit 0" if is_cmd_exe(shell_path) else "true"
    argv = build_argv(probe, shell_path)
    samples = []
    for _ in range(rounds):
        elapsed, code = run_once(argv)
        if code == 0:
            samples.append(elapsed)
    if not samples:
        return None
    return median(samples)


# ---------------------------------------------------------------------------
# 基准执行
# ---------------------------------------------------------------------------


def run_benchmark(commands, runs, warmup, shell_path, interleave=True, timeout=None,
                  capture=True, on_run=None):
    """按 plan_order 跑完所有单元，返回每条命令的样本列表（纳秒）。"""
    samples = [[] for _ in commands]

    if warmup > 0:
        for _ in range(warmup):
            for idx in range(len(commands)):
                argv = build_argv(commands[idx], shell_path)
                _, code = run_once(argv, timeout=timeout, capture=capture)
                if code != 0:
                    raise CommandFailed(idx, commands[idx], code)

    for round_no, idx in plan_order(len(commands), runs, interleave):
        argv = build_argv(commands[idx], shell_path)
        elapsed, code = run_once(argv, timeout=timeout, capture=capture)
        if code != 0:
            raise CommandFailed(idx, commands[idx], code)
        samples[idx].append(elapsed)
        if on_run:
            on_run(round_no, idx, elapsed)
    return samples


class CommandFailed(Exception):
    def __init__(self, index, command, code):
        self.index = index
        self.command = command
        self.code = code
        super().__init__("命令 #%d 退出码 %d：%s" % (index + 1, code, command))


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------


def compare_paired(baseline_samples, other_samples, baseline_label, other_label,
                   iterations=2000, seed=0):
    """配对比较：同一轮里两条命令各跑一次，比值就是那一轮的比值。

    比值 = 基线耗时 / 对方耗时，大于 1 表示对方更快。
    """
    if len(baseline_samples) != len(other_samples):
        raise ValueError("配对比较要求两组样本一样多")
    ratios = []
    for base, other in zip(baseline_samples, other_samples):
        if other <= 0:
            raise ValueError("样本里有非正数，计时出错了")
        ratios.append(base / other)
    lo, hi = bootstrap_ci(ratios, iterations=iterations, seed=seed)
    wins = sum(1 for r in ratios if r > 1.0)
    return {
        "baseline": baseline_label,
        "other": other_label,
        "rounds": len(ratios),
        "speedup": geomean(ratios),
        "ci_low": lo,
        "ci_high": hi,
        "wins": wins,
        "p_value": sign_test_p(wins, len(ratios)),
        "ratios": ratios,
    }


def compare_unpaired(baseline_samples, other_samples, baseline_label, other_label):
    """非交错时只比中位数，不给置信区间——数据不是配对的，算了也不可信。"""
    base = median(baseline_samples)
    other = median(other_samples)
    return {
        "baseline": baseline_label,
        "other": other_label,
        "rounds": min(len(baseline_samples), len(other_samples)),
        "speedup": (base / other) if other else float("inf"),
        "ci_low": None,
        "ci_high": None,
        "wins": None,
        "p_value": None,
        "ratios": [],
    }


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------


def format_duration(ns):
    """纳秒转人话。保留三位有效数字，不堆小数。"""
    if ns is None:
        return "-"
    value = float(ns)
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if abs(value) >= scale:
            return _sig(value / scale) + " " + unit
    return _sig(value) + " ns"


def _sig(x, digits=3):
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return "%.0f" % x
    if abs(x) >= 10:
        return "%.1f" % x
    return "%.2f" % x


def format_ratio(r):
    if r >= 1:
        return "%.2f× 更快" % r
    return "%.2f× 更慢" % (1.0 / r) if r > 0 else "-"


def format_pct(fraction):
    return "%.1f%%" % (fraction * 100)


def render_text(commands, labels, summaries, comparisons, floor_ns, interleaved,
                runs, warmup, shell_name, runs_detail):
    lines = []
    mode = "交错" if interleaved and len(commands) > 1 else "顺序"
    lines.append(
        "benchloop  runs=%d  warmup=%d  %s  shell=%s" % (runs, warmup, mode, shell_name)
    )
    lines.append("")

    for i, command in enumerate(commands):
        s = summaries[i]
        lines.append("#%d  %s" % (i + 1, command))
        lines.append(
            "    median %s   mean %s ± %s (%s)   min %s   max %s"
            % (
                format_duration(s["median_ns"]),
                format_duration(s["mean_ns"]),
                format_duration(s["stdev_ns"]),
                format_pct(s["cv"]),
                format_duration(s["min_ns"]),
                format_duration(s["max_ns"]),
            )
        )
        if s["outliers"]:
            marks = ", ".join(
                "第 %d 次 %s" % (i2 + 1, format_duration(runs_detail[i][i2]))
                for i2 in s["outliers"]
            )
            lines.append("    outliers: " + marks + "（只标记，不剔除）")
        lines.append("")

    if comparisons:
        lines.append(
            "comparison  (baseline #1, %d 轮配对)" % comparisons[0]["rounds"]
            if interleaved
            else "comparison  (非配对，只比中位数)"
        )
        for c in comparisons:
            if c["p_value"] is None:
                lines.append(
                    "    #%s vs #1  %s   median %s → %s"
                    % (
                        c["other"].lstrip("#"),
                        format_ratio(c["speedup"]),
                        format_duration(summaries[0]["median_ns"]),
                        format_duration(summaries[int(c["other"].lstrip("#")) - 1]["median_ns"]),
                    )
                )
                continue
            lines.append(
                "    %s vs #1   %s   95%% CI [%.2f, %.2f]   %d/%d 轮领先 (p=%.4f)"
                % (
                    c["other"],
                    format_ratio(c["speedup"]),
                    c["ci_low"],
                    c["ci_high"],
                    c["wins"],
                    c["rounds"],
                    c["p_value"],
                )
            )
        lines.append("")

    if floor_ns:
        floor_txt = format_duration(floor_ns)
        fastest = min(s["median_ns"] for s in summaries)
        if fastest < floor_ns * 3:
            lines.append(
                "spawn floor %s（空命令的中位数）—— 最快的命令只有它的 %.1f 倍，"
                "量到的主要是进程创建，不是你的程序" % (floor_txt, fastest / floor_ns)
            )
        else:
            lines.append(
                "spawn floor %s（空命令的中位数），最快的命令是它的 %.0f 倍，"
                "进程创建只占小头" % (floor_txt, fastest / floor_ns)
            )
    return "\n".join(lines)


def render_json(commands, summaries, comparisons, floor_ns, interleaved, runs,
                warmup, shell_name, runs_detail):
    return {
        "tool": "benchloop",
        "version": __version__,
        "runs": runs,
        "warmup": warmup,
        "interleaved": bool(interleaved),
        "shell": shell_name,
        "overhead_floor_ns": floor_ns,
        "commands": [
            {
                "index": i + 1,
                "command": cmd,
                "samples_ns": list(runs_detail[i]),
                "summary": summaries[i],
            }
            for i, cmd in enumerate(commands)
        ],
        "comparisons": comparisons,
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="benchloop",
        description="交错跑多条命令，用配对数据算加速比。",
        epilog="例子：python benchloop.py -n 20 'python a.py' 'python b.py'",
    )
    p.add_argument("commands", nargs="+", metavar="COMMAND", help="要测的命令，多条则交替执行")
    p.add_argument("-n", "--runs", type=int, default=10, help="每条命令测几次（默认 10）")
    p.add_argument("-w", "--warmup", type=int, default=2, help="预热几次（默认 2）")
    p.add_argument(
        "-s",
        "--sequential",
        action="store_true",
        help="一条命令测完再测下一条（默认交错，不推荐）",
    )
    p.add_argument(
        "--shell",
        default="auto",
        choices=["auto", "none", "cmd", "bash", "sh"],
        help="怎么执行命令：auto 用系统默认 shell，none 直接 exec（默认 auto）",
    )
    p.add_argument("--timeout", type=float, default=None, help="单次超时秒数")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--seed", type=int, default=0, help="bootstrap 随机种子（默认 0，结果可复现）")
    p.add_argument("--boot", type=int, default=2000, help="bootstrap 重采样次数（默认 2000）")
    p.add_argument("--no-floor", action="store_true", help="不测进程创建地板")
    p.add_argument("--show-output", action="store_true", help="让命令的 stdout/stderr 打出来")
    p.add_argument("-q", "--quiet", action="store_true", help="只输出一行结论")
    p.add_argument("--version", action="version", version="benchloop " + __version__)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs 至少是 1")
    if args.warmup < 0:
        parser.error("--warmup 不能是负数")
    if len(args.commands) > 1 and args.sequential:
        interleaved = False
    else:
        interleaved = len(args.commands) > 1

    try:
        shell_path = resolve_shell(args.shell)
    except ShellError as exc:
        print("benchloop: " + str(exc), file=sys.stderr)
        return 2

    try:
        floor_ns = None if args.no_floor else measure_floor(shell_path)
    except ShellError as exc:
        print("benchloop: " + str(exc), file=sys.stderr)
        return 2

    try:
        samples = run_benchmark(
            args.commands,
            args.runs,
            args.warmup,
            shell_path,
            interleave=interleaved,
            timeout=args.timeout,
            capture=not args.show_output,
        )
    except CommandFailed as exc:
        print(
            "benchloop: 命令 #%d 退出码 %d，基准中断。\n  命令：%s\n"
            "  如果非零退出是预期的，先在命令后面加 `|| exit 0` 再测。"
            % (exc.index + 1, exc.code, exc.command),
            file=sys.stderr,
        )
        return 3
    except ShellError as exc:
        print("benchloop: " + str(exc), file=sys.stderr)
        return 2

    summaries = [summarize(s) for s in samples]
    labels = ["#%d" % (i + 1) for i in range(len(samples))]

    comparisons = []
    if len(samples) > 1:
        for idx in range(1, len(samples)):
            if interleaved:
                comparisons.append(
                    compare_paired(
                        samples[0],
                        samples[idx],
                        labels[0],
                        labels[idx],
                        iterations=args.boot,
                        seed=args.seed,
                    )
                )
            else:
                comparisons.append(
                    compare_unpaired(samples[0], samples[idx], labels[0], labels[idx])
                )

    if args.json:
        payload = render_json(
            args.commands, summaries, comparisons, floor_ns, interleaved, args.runs,
            args.warmup, args.shell, samples,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.quiet:
        for c in comparisons:
            print(
                "%s vs %s: %s" % (c["other"], c["baseline"], format_ratio(c["speedup"]))
            )
        if not comparisons:
            print("median " + format_duration(summaries[0]["median_ns"]))
        return 0

    print(
        render_text(
            args.commands, labels, summaries, comparisons, floor_ns, interleaved,
            args.runs, args.warmup, args.shell, samples,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
