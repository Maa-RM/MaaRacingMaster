# 极速狂飙插件（已退役 · 由无限驾驶后继）

> **本文是什么**：本插件的当前状态、与后继插件的分工、依赖边界、复活步骤。
> **本文不是什么**：不是玩法规则（见 [RULES.md](./RULES.md)，v1 口径）；不是域内实现知识（见 [CODE_WIKI.md](./CODE_WIKI.md)）；不是插件平台架构（见 [docs/CODE_WIKI.md](../../../../docs/CODE_WIKI.md) 与 [core/CODE_WIKI.md](../../../core/CODE_WIKI.md)）。
> **信源等级**：L2 —— 可作「本插件为何退役、哪些能力已转移、如何复活」的直接依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../../../AGENTS.md)。

## 当前状态

已退役，**归档于 `plugins/_archive/speedrush/`**。退役原因：游戏侧极速狂飙玩法更新 v2，
模式结构、商店、阶段节奏等框架级事实整体重建，本插件的操作流程与策略参数所依赖的
v1 事实大面积失效；玩法的驾驶底层事实（超车判定几何、分值、血量公式、NPC 参数、
车道几何）未变，对应能力已转移到后继插件 **无限驾驶**（`plugins/infinite_drive/`）。

归档状态由**目录位置**表达（`plugins/` 顶层只放现役插件），四处消费者按同一位置判据工作：

- **运行期**：`core/registry` 只扫描 `plugins/` 的直接子目录，本模块不注册、不出现在模块列表。
- **分发**：`assemble.ps1` 的 robocopy 以 `/XD _archive` 整目录排除，并在剔除段末尾带反向守卫（stage 里出现该目录即拒绝发布）。
- **仓库树**：代码、资源、测试、工具全部保留；`truth` 的插件扫描面只含 `plugins/` 顶层带 `module.py` 的目录，本插件归档后不在其内；命名空间在 `RETIRED_MODULE_NS` 冻结（core 占用或点名同属分层违规）。
- **测试**：回归锁位于本插件 `tests/` 目录，带 `retired` 标记；默认收集不含归档目录，考古时显式运行：
  `.venv\Scripts\python.exe -m pytest maaracing_master/plugins/_archive/speedrush/tests -m retired`。
  **复活时若不摘标记，回归锁会静默不跑**。

## 与后继插件的分工

| 问题 | 看哪里 |
|---|---|
| v2 玩法的规则事实与现役实现 | [`plugins/infinite_drive/`](../../infinite_drive/CODE_WIKI.md) |
| v1 玩法的完整策略实现（决策/规划/选道/路线/交通层） | 本插件（历史真源，策略参数按 v1 事实标定） |
| 感知/几何/观测层代码 | 两侧同源：本插件保留归档副本，现役副本在后继插件（搬迁时复制，未抽公共层） |

转移判据是「事实是否随 v2 失效」：不依赖 v1 行为参数、游戏事实未变的模块
（感知、深度几何、线观测、世界模型、追踪、观测面纯逻辑、控制基建）整体搬迁；
依赖 v1 策略参数或 v1 UI 流程的模块（决策引擎、规划、选道、路线、交通、走廊、
行为参数表、图节点与模板图、战绩看板）留档，待 v2 事实重验后按需重建。

## 依赖边界（护栏）

本插件仍在 import 的 core 能力**不因本插件退役而删除**；其中感知/几何相关能力
（`yolo_detector`、`dml_lock`、`ocr`、`perf_sampler` 等）同时被后继插件消费。
完整依赖清单以本插件源码的 import 语句为准（L0 真源）。

## 复活步骤

1. 把本目录整体移回 `plugins/speedrush/`（归档区不参与注册与分发，移回即恢复发现）。
2. 全仓搜索 `plugins._archive.speedrush` / `plugins/_archive/speedrush`，改回顶层路径
   （插件内导入、测试、实验区脚本、工具与文档指针都已按归档路径写死）。
3. 把本插件 `tests/` 移回仓库 `tests/`，摘掉 `retired` 标记，再跑 `pytest` 与分层机检。
4. 从 `tools/pipeline/truth.py` 的 `RETIRED_MODULE_NS` 移除 `speedrush`。
5. 注意与后继插件的能力重叠：感知/几何层两侧同源，复活前先裁决哪侧是真源，避免第二份真相。

无需改 registry、无需改发布脚本（归档区排除与顶层剔除各自独立）。