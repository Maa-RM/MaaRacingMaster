# DepthART slim-SPF 伪生产（C 类实验，2026-11-11）

status: active

> **本文是什么**：DepthART MNV4-M-slim-SPF 上产的开工前置——伪生产方案（在实验区
> 复刻生产链路验证 DepthART 走整条链）与验收口径。所有取舍据此定，**改就直接大块
> 删除重构，不做渐进改写**。
> **本文不是什么**：不是选型结论——选型已在上一轮对比收口（DepthART 优先，
> ZipDepth 乱跳垫底，MoGe 质量基准 56ms），见 git 历史与项目记忆。
> **信源等级**：L4（过程与证据）。**继承**：实验规范见 [`tools/experiments/README.md`](../README.md)。

## 背景与证据（上一轮对比结论，证据指针 = commit）

- 候选收敛：DepthART（10.6ms 全链、稳定、结构团块化）/ ZipDepth（23.6ms、结构最好、
  乱跳垫底）/ MoGe 生产 q4f16（56ms、质量基准、形态参考）。证据 = 上一轮对比区
  退役前的最后 commit（`git log -- tools/experiments/speedrush_depth_bakeoff`）。
- DepthART 取舍：原生只出深度图（无 mask/points/normal/scale）；metric 档需内参
  且纯 ORT 跑不了；448×448 方形 0.2MP；slim-SPF 档 24.2MB / 5.91ms，结构三档最好。
- 生产集成面：缝只有 `depth_geo.infer_points`（MoGe 前向+reconstruct → pts/valid/diag）；
  module 层全部消费者（视角守卫 cam_h=coef[2]、边缘检测 sides/left/right、far_extents、
  _box_anchor_z）只吃该产物；normal 无人吃。重构 = 大块删除 MoGe 分支换 DepthART 分支。
- 规则拍板（2026-11-11）：保险杠视角 FOV 曲线浅到几乎不可辨，**选曲线中间值固定套用**
  ——固定值方案下焦距不必逐帧自估（RULES §3.8 已改）。

## 伪生产方案

在实验区复刻整条链（不 import 业务代码，逐段最小自建）：

```
DepthART slim-SPF depth(448²) → 近=小换算 → 放大 720×1280
  → 逐帧地面行仿射 z=a·V+b（金标行 430~670，锚 = 权威 1.25m 关系）
  → 反投影 (X,Y,Z)（焦距方案见下）
  → 路面平面拟合同款（reading_from_points 口径）→ coef → cam_h
  → 视角守卫带检查（[1.12,1.55] 判停）+ _box_anchor_z 框底锚距
  → 对照 MoGe 生产 q4f16 基线（同链同口径）
```

**焦距方案（唯一新决策，DepthART 无逐帧焦距）**：
- A 混合供数：MoGe 降频跑（低频供 mask + 逐帧焦距 + 相机高），DepthART 每拍供深度；
- B 深度图解焦距：地面行仿射给 f·cam_h 乘积，配车道宽关系解 f（循环嫌疑，需验证）；
- **N 固定标称值**：保险杠 FOV 中间值套用（规则已拍板，2026-11-11）——本轮首选验证。

## 验收四条（全过才谈重构）

1. cam_h 跨帧稳定性：视角守卫带判停与 MoGe 基线一致；
2. 平面拟合质量：roll/pitch 门（PLANE_ROLL/PITCH_MAX_DEG）、内点不塌；
3. _box_anchor_z 锚距 vs MoGe 基线偏差（真接近段逐帧对照）；
4. 全链延迟（预处理+推理+仿射+反投影+平面拟合）p50。

## 资产

- `weights/`（gitignored）：DepthART slim-SPF / MNV4-S、MoGe 生产 q4f16、perception、
  车道线权重（拷自上一轮对比区，原件在业务侧）。
- `frames/`（gitignored）：撞出租车段（真接近、跟踪 15/18）+ 稳定 41 段
  （时序度量已被目标切换污染，只用于静景帧间噪声参考）。
