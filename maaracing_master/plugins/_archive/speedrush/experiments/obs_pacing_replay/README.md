# obs_pacing_replay — 观测链消费时序：trace 分解 + 完整生产链离线回放

status: active

> **本文是什么**：回答「YOLO/线观测全链路能否进 50ms、深度全链路能否进 100ms」的
> 实验记录——先用生产 trace 把读取龄拆成段（哪一段在等），再用真实生产组件
> 离线回放验证候选时序。起点是 2026-11-15 对三局 trace 的复核（独立复核者先行
> 统计，本实验复算一致）。
> **本文不是什么**：不回答模型选型（见 `speedrush_depthart_prod`）与 provider
> 取值（见 `lane_obs_dml`）。
> **信源等级**：L4（过程与证据）。**继承**：实验规范见
> [`tools/experiments/README.md`](../README.md)。

## 背景与口径

- 口径一=**新结果首次消费龄**：每个新观测结果第一次被规划器读到的龄；
  口径二=**每拍实际使用龄**：每一拍控制链实际吃到的内容龄（含复用）。
  两者不可混用：深度 ~10Hz、控制 20Hz，复用拍必然比首次消费多一拍。
- 达标线：YOLO/线 ≤50ms（一拍），深度 ≤100ms（两拍）。缺席拍不计入达标
  分母（比例≠供数覆盖率）。
- 事实基线（2026-11-15 三局六阶段，`probe_trace_decomp.py` 复算与独立复核
  一致）：YOLO 口径一 p50 21-25 / p95 41-43ms（≤50ms 99.3-99.7%）；线观测
  p50 72-76 / p95 92-94ms（**≤50ms 0%**）；深度口径一 p50 73-86 / p95
  124-139ms（≤100ms 70.2-88.5%）；口径二中位 115-122ms。
- 线观测现役已是 DML FP16（`decision.json` provider=dml，FP16 权重随包），
  处理本体 p50 仅 11-13ms——**模型不是瓶颈**。

## 段账（trace 分解，`probe_trace_decomp.py`）

线观测读取龄 p50 72-76ms 的构成（六阶段一致）：

| 段 | p50 | 说明 |
|---|---:|---|
| cap（采集→提交） | 19-23ms | 控制拍在检测后 ~20ms 才把线帧交给后台 |
| queue（排队→开工） | ~2ms | |
| ms（处理本体） | 11-13ms | DML FP16 前向+提取 |
| **发布龄**（前三段和） | **33-39ms** | 结果 ~34ms 就绪 |
| **发布→读取** | **38-39ms** | **读取点在下一拍 ~22ms 处，结果空等一整拍** |

→ 线观测「多等一拍」全在消费时序：读取点（检测后 ~22ms）早于发布点（~34ms），
本帧结果永远错过本拍读取点，下一拍才被读到。

深度读取龄的构成：cap 18-21 + queue ~2 + **本体墙钟 p50 46-47ms**（points
~13 + edges ~16 + forward ~5.6 + plane ~6.2，栅格与找边并行不计入墙钟）+
发布段 ~7ms。本体比离线口径（38ms）贵 ~8ms=生产 CPU 争抢。**注意**：分段
求和会把并行的栅格算进去，虚增 ~10ms；本体墙钟用「读取龄−cap−queue−pub」
残差口径。

## 回放（`probe_full_chain_replay.py`，真实生产组件）

StreetPerception / LaneObsWorker / AsyncDepthRoadObserver + 真实配置与标定，
真实帧循环喂，20Hz 拍步，消费龄逐拍实测。**标尺**：V0 复刻先对 trace——
线 70.3ms（trace 72-76，差=回放无生产负载）、深口径一 70.6 / 口径二 95-120
（trace 73-86 / 115-122），结构逐项复现；绝对尾部偏轻是已知边界。

| 变体 | 线口径一 p50/p95 | ≤50ms | 深口径一 | 说明 |
|---|---|---|---|---|
| V0 复刻（提交 ~20ms、单次早读） | 70.3 / 70.9 | 0% | 70.6 | 复现生产结构 |
| V1 拍首提交（生产 worker） | — | — | — | **不可用**：槽窗契约假设提交在节拍后，238/240 超窗弃帧 |
| V2 提交不动 + 拍内 ~40ms 晚读 | 40.4 / 41.4 | **100%** | 70.6 | 改动最小：加一个晚读取点并消费新结果 |
| V3 拍首提交 + worker 等窗 + 早/晚双读 | **20.3 / 20.5** | **100%** | 70.6 | 早读取点就吃到同帧结果；需子类验证的等窗语义 |
| V4 深度 take 挪晚读点 | — | — | 70.6 | 回放无尾部，take 位置不改变龄（见下） |

V1 的教训即设计约束：**拍首提交必须配 worker「等本拍节拍开窗」语义**
（探针内 `EarlySubmitWorker` 子类已验证可行：等 note ~9ms 后正常进槽）。

## 深度的结构下限（回放揭示）

- 单次 take 在拍内 ~+21ms；任何 >21ms 才就绪的结果最早也要到**下一拍 +21**
  才被读到 → **首次消费龄下限 ≈ 71ms**（回放四变体全部 70.6，与链路快慢、
  take 位置无关——错峰让 worker 处理「上一拍待帧」，publish 恒落在下一
  take 点之前）。
- 复用拍比首次消费恒多一拍（50ms）→ **每拍使用龄（口径二）下限 ≈ 121ms**。
  「深度每拍 ≤100ms」在 20Hz 控制 + 单次 take + 现链体量下**结构性不可达**，
  不是优化问题。
- 可达的是：口径一 p50 ~71（已在线上）+ **治 p95**。生产 p95 124-139 的
  缺口=发布尾部越过 take 点罚一拍（本体 p95 61 + cap p95 38 > 71）：压
  points/edges 的生产膨胀与 YOLO 尾部（cap p90 37ms）即可收回，结构不拦。

## 结论

1. **线观测 ≤50ms 可达，瓶颈在读取点不在模型**：V2（加晚读取点）回放
   100% 达标、p50 40ms，改动最小；V3（拍首提交+等窗）回放 p50 20ms，对
   生产发布尾部（p90 52ms）更稳健，但要改 worker 槽窗语义并验证锚配对
   差一拍的语义（V1 证明不改 worker 拍首提交不可用）。
2. **深度首次消费 ≤100ms 可达且 p50 已在下限（~71ms）**；p95 达标需压
   发布尾部（本体生产膨胀 + 提交尾巴），是优化问题。
3. **深度每拍使用龄 ≤100ms 不可达**（下限 121ms）：复用必加一拍。若要
   收紧只能改消费语义（复用拍不吃旧深度/改控制频率），属控制设计问题。
4. 模拟口径的教训沉淀：`lane_obs_dml` 的固定排班模拟以固定睡眠建模深度
   后处理、消费龄按拍边界取整推算、偶数拍顺序与生产相反——其「零丢帧、
   帧龄 100ms」不能当生产结论；本实验以真实组件+逐拍实测取代。
5. **生产实现交叉验证（2026-11-16，L2 落地后）**：回放探针改用生产
   LaneObsWorker（pre_note 目标节拍制，探针内 EarlySubmitWorker 子类删除）
   复跑——v3 口径一/二 p50 20.3 / p95 20.5ms、100% ≤50ms，与子类行为
   逐位一致；v0 复刻基线不变（70.4ms / 0%）。生产实现与回放标尺吻合。

## 边界

- 回放无游戏/采集/调试落盘负载：V0 对 trace 的差（线 −2~6ms、深 p95 明显
  偏轻）即负载差；**V2 在生产负载下晚读点 ~40ms 会漏掉发布 p90（52ms）
  的 ~10-15% 拍**（退回 ~90ms 龄），V3 的拍首提交对尾部免疫（发布 p95
  ≈ 21ms）——两变体的生产取舍要实机一局对账。
- 锚语义：拍首提交时深度锚取上一拍驻留值（生产在 ~20ms 提交时取的是本拍
  已发布结果），锚差一拍对线网格判据（连续两拍步长门）的影响未验证。
- 回放的拍内工作段用睡眠占位（生产为规划器/决策真实 Python 负载）；晚读
  时机（40ms）取自 trace 段账，生产实现要按真实工作段位置定。
- 帧源 14 张循环（`speedrush_depthart_prod/frames`），线提取负载与真实对局
  有差；对时序结论影响有限，对绝对耗时保留疑点。

## 复算

```
# trace 分解（三局）：
.venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_trace_decomp.py \
    "%APPDATA%/MaaRacingMaster/logs/<会话编号>" \
    "%APPDATA%/MaaRacingMaster/logs/<会话编号>" \
    "%APPDATA%/MaaRacingMaster/logs/<会话编号>"
# 回放（先跑 V0 对标尺，首行必须显示「退出节流 成功」）：
.venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_full_chain_replay.py --variant v0 --duration 12
.venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_full_chain_replay.py --variant v2 --duration 12
.venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_full_chain_replay.py --variant v3 --duration 12
```
