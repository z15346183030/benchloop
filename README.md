# benchloop

**两条命令交替跑，用配对数据算加速比。一个文件，零依赖。**

```bash
python benchloop.py -n 10 "python examples/dedupe_list.py 20000" "python examples/dedupe_set.py 20000"
```

输出长这样（这是这台机器上的真实结果）：

```
benchloop  runs=10  warmup=2  交错  shell=auto

#1  python examples/dedupe_list.py 20000
    median 1.71 s   mean 1.73 s ± 92.8 ms (5.4%)   min 1.62 s   max 1.92 s

#2  python examples/dedupe_set.py 20000
    median 511 ms   mean 519 ms ± 34.3 ms (6.6%)   min 479 ms   max 601 ms
    outliers: 第 1 次 601 ms（只标记，不剔除）

comparison  (baseline #1, 10 轮配对)
    #2 vs #1   3.34× 更快   95% CI [3.21, 3.46]   10/10 轮领先 (p=0.0020)

spawn floor 181 ms（空命令的中位数）—— 最快的命令只有它的 2.8 倍，量到的主要是进程创建，不是你的程序
```

那行 `spawn floor` 不是抱怨，是结论：`#2` 的 511 ms 里有相当一部分是 Python 启动，
真正属于代码的时间只有几十毫秒。两个脚本做的事一模一样，只是一个用 list 查重、一个用 set，
真实差距远不止 3.34 倍——工具把能确定的部分告诉你，把不能确定的部分也说出来。

## 为什么又造一个

基准测试工具最怕的不是测不准，是**测出一个错的数字还让你相信它**。

机器会随时间变慢：温度上来、后台进程醒来、杀毒软件扫到你的目录。传统做法是把 A 跑完 10 次，
再跑 B 跑 10 次。如果 B 那一轮正好赶上机器变慢，你会得到「B 慢 3.9 倍」的结论——而这个结论完全来自时间，
不是来自代码。同一个命令跟它自己比，也能测出 3.90× 的差距。

benchloop 只做三件事，但这三件事是连在一起的：

1. **交错执行**：第 1 轮跑 A、B，第 2 轮跑 B、A，如此往复。机器变慢时两条命令一起变慢，比值不受影响。
2. **配对统计**：每轮的比值是一个样本，对 10 个比值做 bootstrap，得到 95% 置信区间和符号检验的 p 值。
   区间跨过 1.0 就说明「这点差异还不够下结论」，工具会如实这么说。
3. **进程创建地板**：先量一次空命令要多久。如果你的命令只比它快两三倍，工具会直接告诉你：
   你量到的主要是进程创建，不是你的程序。

## 安装

不需要安装。下载一个文件，Python 3.9+：

```bash
curl -O https://raw.githubusercontent.com/z15346183030/benchloop/main/benchloop.py
python benchloop.py --version
```

## 三种用法

**量一条命令**——只看分布，不做比较：

```bash
python benchloop.py -n 20 "python manage.py migrate"
```

**比较两条命令**——默认交错，给出比值、区间、p 值：

```bash
python benchloop.py -n 10 "python a.py" "python b.py"
```

**量一次真实开销**——先看看进程启动占了多少：

```bash
python benchloop.py -n 5 "python -c pass"
```

## 参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `-n, --runs` | 10 | 每条命令测几次 |
| `-w, --warmup` | 2 | 预热几次，不计入结果 |
| `--shell` | auto | `auto` / `none` / `cmd` / `bash` / `sh`。`none` 直接 exec，不经过 shell |
| `--sequential` | 关 | 一条测完再测下一条。**只在你需要顺序执行时用**，此时不给区间和 p 值 |
| `--json` | 关 | 输出结构化结果，方便塞进 CI |
| `--seed` | 0 | bootstrap 随机种子，同一组数据结果可复现 |
| `--timeout` | 无 | 单次执行超时秒数 |
| `--no-floor` | 关 | 不测进程创建地板 |
| `--show-output` | 关 | 让命令的 stdout/stderr 打出来（默认丢掉，避免输出拖慢计时） |
| `-q, --quiet` | 关 | 只打一行结论 |

## JSON 输出

```bash
python benchloop.py -n 5 --json "python a.py" "python b.py" > result.json
```

```json
{
  "tool": "benchloop",
  "runs": 5,
  "interleaved": true,
  "overhead_floor_ns": 173000000,
  "commands": [
    {
      "index": 1,
      "command": "python a.py",
      "samples_ns": [2200000000, 2380000000],
      "summary": {"median_ns": 2200000000, "mean_ns": 2380000000, "outliers": []}
    }
  ],
  "comparisons": [
    {"other": "#2", "speedup": 3.0, "ci_low": 2.8, "ci_high": 3.23, "wins": 5, "p_value": 0.0625}
  ]
}
```

## 它不做什么

- **不测 CPU 时间 / 内存**。跨平台拿子进程树的 CPU 时间，标准库给不了；宁可只给一个准的墙钟时间，
  也不给一个在某些系统上悄悄是错的数字。
- **不自动决定跑几次**。10 次是个能覆盖大多数场景的默认值，跑多久你自己定。
- **不做统计检验的花活**。只有 bootstrap 区间和符号检验，两个都能手算验证。
- **不清理缓存、不改 CPU 频率**。这些是操作系统的事，工具越权去做只会让结果更难解释。

## 什么时候你会得到「测不准」

命令比进程创建地板还短的时候。这时候唯一的出路是**把循环写进被测的命令里**：

```bash
# 别这样：量到的是 python 启动
python benchloop.py -n 10 "python -c 'sum(range(1000))'"

# 这样：把 1000 次循环塞进一次进程
python benchloop.py -n 10 "python -c 'for _ in range(1000): sum(range(1000))'"
```

`spawn floor` 那一行就是提醒你这个的。

## 测试

```bash
python -m unittest discover -s tests
```

54 个用例，覆盖分位数、离群判定、执行顺序、bootstrap 区间、符号检验、命令行行为和失败处理。
