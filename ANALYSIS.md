# 灵感来源：[sharkdp/hyperfine](https://github.com/sharkdp/hyperfine)

- 星标：29,156
- 许可证：Apache-2.0
- 语言：Rust
- 它承诺解决：把「A 和 B 哪个快」变成一个可重复、有统计支撑的命令行问题——跑 N 次、算均值和标准差、顺手比较。

## 它做对了什么

**1. 把 shell 启动时间单独量出来，再从结果里扣掉。**
`src/benchmark/executor.rs` 里有个 `calibrate()` 方法，注释写得很直白：走 shell 执行时，必须先量出
shell 的启动开销，之后从每次结果里减掉。这不是可有可无的优化——Windows 上 `cmd /c` 启动一次就是几十到
一百多毫秒，不减掉的话，所有亚秒级的命令都在量同一个东西：进程创建。

**2. 每个平台一套计时器，而不是一套糊弄所有平台。**
`src/timer/` 下有 `linux_counters.rs`、`macos_counters.rs`、`windows_timer.rs`、`unix_timer.rs`、
`wall_clock_timer.rs` 五个实现。要拿到 user / system 时间，Windows 得用 `GetProcessTimes`，
Linux 得读 `wait4` 的 rusage，macOS 还有自己的 API。作者没有为了省事只留墙钟时间。

**3. 离群检测用中位数和 MAD，不用均值。**
`src/outlier_detection.rs` 用的是 Iglewicz & Hoaglin (1993) 的修改 Z 分数，阈值 `1.4826 * 10.0`。
选 MAD 是对的：一个 8 秒的尖峰会把均值和标准差一起带跑，但带不动中位数。

**4. 参数扫描（`-P`）是这个工具最被低估的功能。**
`hyperfine -P threads 1 8 'make -j {threads}'` 一条命令跑完 8 个配置，还能自动画出趋势。
把「扫参数」和「测时间」放进同一个工具，省掉了写外层脚本的麻烦。

**5. 输出格式的生态做得完整。**
`src/export/` 里有 json / csv / markdown / asciidoc / orgmode 五种导出，加上 `--export-*` 三个独立开关。
CI 里存 JSON、README 里贴 Markdown 表格，都是一条命令的事。

## 它在哪里让人痛苦

### 交错执行：2018 年提的，2026 年还开着

> "This would be useful when comparing two similar commands, as interleaving them makes it less likely that e.g.
> a load spike will unfavorably affect only one of them, or due to e.g. thermal throttling negatively affecting
> the last command."
>
> — [#21 Interleaved benchmark execution (multiple commands)](https://github.com/sharkdp/hyperfine/issues/21)
> （42 条评论，2018-01-21 提出，至今 open）

作者当年的回复是：

> "Also, simplicity is a desirable goal for me. If there are command line options that we don't necessarily need,
> I would tend to leave them out."

这条理由站得住，但它把成本转嫁给了用户：**默认行为就是有偏的**。看 `src/benchmark/scheduler.rs`：

```rust
for (number, cmd) in self.commands.iter().enumerate() {
    self.results.push(
        Benchmark::new(number, cmd, self.options, &*executor).run(self.results.first())?,
    );
}
```

一条命令的全部轮次跑完，再跑下一条。2020 年 comex 在同一个 issue 里把问题讲得更清楚：

> "Instantaneous load spikes are one thing, since they can be removed by outlier detection or at least detected
> by a high variance. But there are also many ways in which the state of the system can vary *slowly* over time.
> For example: Thermal throttling..."

离群检测治的是尖峰，治不了漂移。一个慢慢变热的过程不会产生任何一个「离群点」，它让整段数据整体抬高——
而那段数据恰好属于第二条命令。

### 想要区间和 p 值，请去 scripts 目录

> "I use this in academic research, where I'll be computing p-values to describe the confidence with which the
> sample data suggests that one program is faster than another."
>
> — [#523 Confidence intervals, p-values](https://github.com/sharkdp/hyperfine/issues/523)

作者的答复是把 `scripts/advanced_statistics.py` 和 `scripts/welch_t_test.py` 指给用户。脚本能用，
但它们是旁路：你得先把 JSON 导出来，再自己喂进去，而且脚本不认识「配对」这件事——
它的输入是两组独立样本，交错跑出来的结构信息在导出那一刻就丢了。

`1.40 ± 0.03 times faster` 里的 ± 是一个标准差，不是置信区间。提问的人专门追问过这一点，
说明这个歧义对读者是真实存在的。

### `--subtract`：一个被讨论了两轮、最后靠 shell 脚本绕过需求

> "My usecase for this is that I want to benchmark page loads, for which I'm using headless chromium, but want to
> subtract the time that chromium takes to start up."
>
> — [#265 Add --subtract option](https://github.com/sharkdp/hyperfine/issues/265)

四年后有人在 issue 里贴了自己的解法：写一个假 shell，在 `-S` 参数里给除目标程序外的每条命令都垫一个
固定开销，把差异抹平。这已经不是「用工具」，是在跟工具搏斗。

### 一条无法关闭的警告

> "I keep consistently getting that warning when absolutely nothing else on my computer is running... I've already
> tried `--shell=none` and as many as 1000 warmup runs and it still shows up. ... could there please be a way to
> suppress it?"
>
> — [#528 Suppress "statistical outliers detected" warning](https://github.com/sharkdp/hyperfine/issues/528)

警告本身没错，错在它只说「检测到离群点」，不说「哪几次、差多少」。用户没法判断该重跑还是该忽略，
只能去翻 JSON。

## 我们的取舍

**砍掉的**

| 砍掉 | 为什么 |
|---|---|
| `--prepare` / `--setup` / `--cleanup` | 三条生命周期命令，各自还要处理失败策略、输出策略、是否计时。`--prepare` 甚至会改变结果（#715），因为它在两次测量之间插入了一段未知长度的执行 |
| 参数扫描 `-P` | 好用，但它是另一个功能（批量实验），不该和「比较两条命令」混在一个命令里 |
| 五种导出格式 | JSON 一种就够。Markdown 表格三行代码就能从 JSON 生成，不需要工具内置 |
| CPU / 内存指标 | 标准库拿不到跨平台的子进程 CPU 时间，宁可不给 |
| 直方图、进度条、配色主题 | 输出是给人看的一段文字，不需要终端 UI |
| shell 启动时间的扣减 | 见下 |

**保留的**

| 保留 | 为什么 |
|---|---|
| 墙钟时间 + 预热 | 这是所有基准的地基 |
| 中位数、均值、标准差、最小最大 | 少一个都看不懂分布 |
| 修改 Z 分数标离群 | 抗尖峰，实现只要二十行 |
| JSON 输出 | CI 要用 |

**加的**

| 加 | 为什么 |
|---|---|
| 默认交错，ABBA 顺序 | 直接对着 #21 去。单数轮反序，让「排在前面的那次」不总是同一条命令 |
| 配对 bootstrap 区间 + 符号检验 p 值 | 交错跑出来的数据天生是配对的，那就用配对的方法算。对应 #523 |
| 进程创建地板 | 不扣减，而是**量出来告诉你**。扣减会让结果出现负数的可能，而且扣多了等于骗人；报一个地板，让用户自己判断这次测量有没有意义 |
| 离群点标出具体轮次和耗时 | 对应 #528：不说「有离群点」，说「第 4 次 1.36 s」 |

**为什么不扣 shell 启动时间。**
hyperfine 的做法是先量、再减。减法的前提是「开销是常数」，可它不是——同一个空命令在这台机器上
5 次测量是 155 / 159 / 169 / 234 / 323 ms。拿中位数去减，每次测量都会引入一个新的误差项，
而且减完之后你无法知道误差有多大。benchloop 改成：量一次地板，如果被测命令离地板太近就直接警告。
数字保持原样，判断权交回用户。

## 与原版的差异

| 维度 | hyperfine | benchloop |
|---|---|---|
| 执行顺序 | 一条命令跑完再跑下一条 | 默认交错，单数轮反序 |
| 比较统计 | 均值比值 ± 一个标准差 | 每轮比值的几何平均 + bootstrap 95% 区间 + 符号检验 p 值 |
| 进程开销 | 量出来并从结果里扣掉 | 量出来作为地板提示，不扣 |
| 离群点 | 只报数量，无法关闭警告 | 报具体轮次和耗时，不改变任何计算结果 |
| 计时指标 | 墙钟 + user/system + 内存（部分平台） | 只有墙钟 |
| 输出 | 文本 + 5 种导出格式 + 直方图 | 文本 + JSON |
| 生命周期钩子 | `--prepare` / `--setup` / `--cleanup` | 无 |
| 参数扫描 | `-P` | 无 |
| 代码规模 | 41 个 Rust 文件，`src/` 共 233 KB | 1 个 Python 文件，625 行 / 21 KB |
| 依赖 | Rust 工具链 + 若干 crate | Python 3.9+ 标准库 |

## 许可证说明

上游是 Apache-2.0，允许参考与复用。这个仓库**没有复用任何代码**：`benchloop.py` 全部自己写，
统计部分用的是教科书公式（分位数线性插值、几何平均、百分位 bootstrap、二项分布符号检验），
离群检测用的是 Iglewicz & Hoaglin 的公开方法，与本项目的实现无关。

读源码时参考的是它的**设计取舍**——比如「为什么要单独量 shell 启动」「为什么用 MAD 而不是标准差」，
这些是思路，不是代码。
