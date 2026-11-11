# speedrush_lane_model — 车道线专精模型替换调研（活跃）

status: active

> **本文是什么**：车道线模型替换的第一步——**候选调研与路线判读**。回答「有没有效果更好的
> 车道线专精模型」这个外部生态问题，并把「检测数量不足」变成可量化的实验入口。
> **本文不是什么**：不是选型定案，也不改生产代码——候选淘汰以文末探针的实测为准。
> **信源等级**：L4（过程与证据）。结论兑现后按 [tools/experiments/README.md](../README.md) 生命周期四步退役。
> **纪律**：委托调研的原始笔记不入树（外部调研两份报告留在工作台临时区，用完即清）；进树的只有
> 经核实的**采纳清单**与**更正清单**，外部事实一律附 URL，项目内事实指向 L0/L2 真源或 commit。

## 问题（2026-11-11，用户报告 + 既有证据）

现役模型在部分环境**车道线检测数量不足**。项目内既有证据：

- 远场：`lane_membership` 的远车样本实测（72 样本供货 4/72）失效三因为「不夹框心 44% /
  **缺线 35%** / 线对退化 15%」——缺线占三分之一（`maaracing_master/plugins/speedrush/lane_obs.py`
  模块 docstring，commit 历史）。
- 可用性：线观测可用率有从 45% 掉到 18%、盲窗拉到 8s 的实测案例（CODE_WIKI「线观测的绝对位
  与线锁」节的 `_assign_k` 网格接号背景）。
- **交叉依赖（新）**：深度模型选型实验正用「相邻车道线像素间距 ↔ 3.5 m」做深度米制金标
  （speedrush_vision 实验区的车道线金标探针，commit `1c82f4d`）——车道线**隔线漏检会把间距
  读成两倍、直接污染这把尺**。修召回不只是驾驶问题，也是深度标定链的数据质量问题。

算力窗口：深度模型替换实验进行中（MoGe 前向在 DML 上是 ~40ms 固定派发税，见
speedrush_depth_update 实验的实测节；轻量替代已在测 4ms 级）。深度让出 GPU 锁后，线观测
从「CPU 独立线程」上 DML 成为可选项——这改变了候选模型的算力判据，是现在做本次调研的时机。

## 生态核查（动笔前两问）

- **生态里已有什么**：MaaFW 不提供车道线观测能力；本仓已有 TwinLiteNet+（现役）与 LaneNet 系
  首眼验证（commit `07700d1`）两笔在案证据；外部生态的候选全景见下文调研（14 个模型逐一核实）。
- **为什么不能用/要换**：不是推倒现有链路，而是「同一接口/相邻接口上找召回更高的专精模型 +
  补上从未评估过的微调路线」。

## 现役基线（L0/L2 已核实）

| 项 | 事实 | 出处 |
| --- | --- | --- |
| 模型 | TwinLiteNet+ Small（官方 BDD100K 预训练权重，非商业许可） | config.py `LaneObs` docstring；模型包 `LICENSE-BDD100K` 说明（仓库 `.gitignore` 尾注） |
| 输入/输出 | 640×360 letterbox + 上下 12px 灰边 → 384×640；车道头概率图（640 尺度像素，消费端 ×2 回全幅） | `lane_obs.py` `_letterbox` / `_prob_from_out` |
| 延迟 | CPU 4 线程约 23ms/帧（文档口径）；生产配置 `hz=0` 全速推送 | config.py docstring；policy/decision.json `[lane_obs]` |
| 接口链 | 概率图 → 行扫描（行 206~359、步 3、阈值 0.30）→ 轨迹 → 近/远分段拟合 → 颜色/线角/残差门 → 线锁状态机 | `lane_obs.py` |
| 官方指标口径 | **BDD100K 车道 IoU（Small 29.3）与 Acc（75.8），官方不报 F1** | 官方仓 README 与论文（URL 见引用清单） |

**接口分水岭**（调研确认的最大迁移成本项）：概率图族（TwinLiteNet+ / HybridNets / YOLOPv2）
可直接沿用现有行扫描消费端；线坐标族（UFLD v1/v2 / CLRNet 系 / LaneATT / SRLane /
BézierLaneNet）输出线坐标或曲线参数，换它们 = 重写消费端。

## 既有实验证据（不重复踩坑）

- commit `07700d1`（speedrush_pretrain，已退役）：LaneNet 系预训练权重首眼 = 画面满屏乱线，
  域差大。**边界**：该结论的对象是 LaneNet（嵌入聚类族 + TuSimple 训练数据），不能外推为
  「一切预训练车道网都不能用」——现役 TwinLiteNet+（BDD100K 多任务分割族）同样是现成权重且
  在生产可用。「域差」是族 × 数据集相关的，不是普适判词。
- 微调从未评估（当时理由：缺标注数据集）——本轮调研补上的正是这条路线的就绪度与数据来源。

## 调研结果（2026-11-11，两名研究员并行调研，主会话抽查复核承重数字）

### 结论速览

1. **精度口径不可直接互比**：基线家族官方只报 BDD100K 车道 IoU；CULane F1 是另一套口径。
   公平比较必须在自建游戏场景评测集上统一测。
2. **CULane 分场景表公开可查，但没有「隧道」类**（官方 9 类 = normal + crowded/night/no
   line/shadow/arrow/dazzle/curve/cross）。夜间最强公开数字是 **CLRNet 家族**
   （ResNet18 夜间 F1 75.11；论文表 1，主会话已打开原文复核）。
3. **候选几乎没有公开 x86 CPU 延迟**（论文全是 GPU/Jetson 数字）：CPU 预算只能本地实测；
   按 GFLOPs 粗算 UFLDv2/CLRNet r18 约为基线 3~5 倍算力（推定，非实测）。
4. **2025–2026 未发现「换一个更强的现成轻量模型」的捷径**：近期活跃点在 CLRNet 改进
   （CLRerNet/CLRKDNet）、视频时序车道与 3D 车道，均未在 2D 轻量分场景上显著超越上述模型。
5. **纯换现成权重（零微调）不成立**：所有候选的公开权重都训练自真车数据（CULane/BDD100K）；
   公开实证（CARLANE：无监督域适应的假阳/假阴率显著高于全监督）与本仓 LaneNet 首眼都指向
   「少量目标域标注 + 全监督微调」才是可靠路径。

### 候选清单（关键数字均标注证据等级与出处，完整逐模型笔记见引用清单）

| 模型 | 许可 | CULane F1 / 夜间 | ONNX | 状态 | 判读 |
| --- | --- | --- | --- | --- | --- |
| CLRNet r18 | Apache-2.0 | 79.58 / **75.11**（[论文-自报]，已复核） | 社区转换有先例 | 2024-07 | **GPU/DML 路线首选**：夜间最强家族；Line-IoU 整线回归 + 跨层细化正对细线与遮挡；~12 GFLOPs CPU 存疑 |
| UFLDv2 r18 | MIT | 74.7 / 70.2（[论文-自报]） | **官方导出脚本 + PINTO 预转**（已复核 README） | 2024-04 | **迁移成本最低的跨族选项**：混合锚系统性修复「近水平边线在行锚下的误差放大」；头部体积需实测 |
| CLRKDNet r18 | Apache-2.0 | 79.66（教师蒸馏，[官方-自报]） | 未核实（可试 CLRNet 管线） | 2025-07 | CPU 方向的折中实验：CLRNet 级精度 + 更精简头；夜间分场景未公开 |
| CLRerNet (DLA34) | Apache-2.0 | 81.43（[官方-自报]；分场景未公开） | 未核实 | 2026-01 仍活跃 | LaneIoU 置信度校准正对「真线被阈值滤掉」；骨架偏重，GPU 路线 |
| TwinLiteNet+ Medium | MIT | （BDD 口径 IoU 32.3，+3.0 vs Small） | 项目内已证明可导 | 2026-05 仍活跃 | 概率图族内升级，**零消费端重写**；算力 ×3.3，CPU 大概率超预算（推定） |
| SRLane | **无 LICENSE、已归档** | 78.9 / 74.7 | — | 2024-06 归档 | **排除**：许可不明即视同保留所有权利 |
| E2EC（车道线版） | NOASSERTION | 常见引用 78.4 **不可复现** | — | 官方仓已转实例分割 | **排除** |
| CondLaneNet / O2SFormer | Apache-2.0 | — | 动态卷积/DETR 导出困难 | — | **排除**（ONNX/DML 不友好） |
| YOLOPv2 / HybridNets | MIT | （BDD 口径，车道 IoU 27.2 / 31.6） | HybridNets 官方导出 | 停更 | **排除**：体量大、车道精度不优于族内升级 |

### 采纳清单（经主会话复核后采信的结论）

- CLRNet r18 夜间 75.11 / UFLD v1 夜间 62.10 / CondLaneNet r18 夜间 73.23——已打开
  [CLRNet 论文原文](https://ar5iv.labs.arxiv.org/html/2203.10350)表 1 逐列核对。
- UFLDv2 官方 `deploy/pt2onnx.py` 导出路径与 PINTO #324 预转 ONNX——已打开
  [官方仓 README](https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2) 核对（含 r18/r34 权重表）。
- TwinLiteNet+ 四变体（Nano/Small/Medium/Large）参数量、速度、BDD100K 指标，及「官方不报 F1」——
  两名研究员独立读论文与 README 交叉一致；官方仓 commit `90f1b86`（2026-05-05）。
- SRLane 归档且无 LICENSE（GitHub API 元数据）→ 不具备采用条件。
- CULane 许可明文允许分发「以其训练的模型」（官方条款原文）；BDD100K 条款允许教育/科研/非营利
  的使用与再分发——与项目现行分发口径一致；TuSimple 无一手许可文本 → **不作预训练起点**。
- 微调就绪度（官方仓逐项核查）：训练/单任务微调/验证脚本齐全；缺口三处均为小改动——
  ① 发布版 `.pth` 不能直接喂 `--resume`（需几行胶水）；② 数据加载器写死 BDD100K 目录
  （自定义集按同构目录 + 灰度掩码、前景灰度 >1 供给）；③ 官方无 ONNX 导出脚本（项目已自证可行）。

### 更正清单（调研推翻/修正的预设，防止未来读者沿用错误前提）

- 「CULane 有隧道分场景 F1」→ **不存在该类别**；且用户 2026-11-11 裁定：游戏无夜间/隧道场景，
  不在适用域内（RULES 佐证：雨天不遮标线是 `[权威]` 事实、环境光照仅逐场随机分组）——
  **适用域收窄为日间正常光照、天气随机**，P0 分场景统计不再单列夜间/隧道。
- 「TwinLiteNet+ 论文报车道 F1」→ 官方只报 Acc 与 IoU。
- 「变体名 tiny」→ 官方四变体为 Nano/Small/Medium/Large。
- 「+ 家族变体权重公开可下载」→ **官方直链已死**：README「from google/baidu」是无链接死文本、
  无 GitHub Releases、HF 仓（nielsr/twinlitenetplus-nano）已 404（2026-11-11 逐一核实）。
  Medium/Large 现阶段不可得，除非维护者存档（当年 Small ONNX 就是官方权重转出，存档可能存在）。
- 「无监督域适应可以省标注」→ CARLANE（NeurIPS 2022）实证 FP/FN 显著高于全监督基线，不走。
- 「LaneCPP 是 2D 轻量候选」→ 3D 车道任务，不适用本项目。
- 「E2EC CULane F1≈78.4」的常见引用 → 车道线版已不可复现，取消候选资格。

## P1 首眼对比读数（2026-11-11 已跑）

探针：`probe_first_look.py`（自包含，线提取语义=生产同口径复刻：行扫描/轨迹/近远拟合/
颜色门/残差与线角门，线角焦距取标定中位 807.7 代用）；帧源=最近人工局录制帧
（`20261109_104823_manual_p1/p2`，应用数据 debug 根，掐首尾 10% 后均匀采样）。
叠加图与汇总表在 `first_look_out/{cpu,dml}/`（gitignored，可重跑）。

参与模型（≤5MB 且当天实际可得的全集）：

| 模型 | 体积 | 来源 |
| --- | --- | --- |
| plus_small（现役基线） | 530KB | 生产权重（TwinLiteNet+ Small 官方 BDD100K，项目内转换） |
| twinlitenet_orig | 1758KB | 原版 TwinLiteNet，harrylal/TwinLiteNet-onnxruntime 仓自带转换件（MIT）；sha256 前 12 位 `f505b56bd7c9`（全哈希见 summary.json） |

**CPU 4 线程（n=24/局）**：现役 p50 22.0~23.4ms；原版 **229~465ms（p95 达 619ms）**——
老导出图在 ORT-CPU 上是病理级慢，CPU 档直接出局。

**DML（n=16/局）**：现役 p50 6.2~7.0ms；原版 **6.5~8.2ms——同速**。

检出质量（同一套生产门下）：

| 指标（两局范围） | plus_small 现役 | twinlitenet 原版 |
| --- | --- | --- |
| 可用线数中位 | 1~2 条（p10~p90: 0~3） | 1~2 条（p10~p90: 0~3） |
| ≥2 线帧占比 | CPU 54~62% / DML 38~50% | CPU 54~75% / DML 38~62% |
| 双侧可配对帧占比 | CPU 50~62% / DML 31~44% | CPU 33~71% / DML 38~56% |

**P1 答案**：「数量不足」量化坐实——现役模型在这批帧上中位只出 2 条可用线、约四成帧连
可配对的双线都凑不齐。原版 TwinLiteNet 在 **GPU 同价**下召回普遍更高（双侧率 +5~12 个百分点），
与它更高的 BDD100K Lane IoU（31.1 vs 29.3）方向一致；CPU 上不可用。若深度替换落地腾出 DML，
**换原版权重是零消费端改动的直接受益路径**（+ 家族 Medium/Large 待权重可得性解决后同表补测）。

**观察（不写结论）**：黄昏场景下白色分界线被颜色门判成黄线（两个模型同病，生产行为同源）；
可用线的拟合段几乎全部来自中远带（近场带拟合中位 0）。

## 权重与来源台账（weights/ gitignored，取法）

- `twinlitenet_orig_360x640.onnx`：`git clone --depth 1 https://github.com/harrylal/TwinLiteNet-onnxruntime`
  后取 `models/best.onnx` 改名（原仓 MIT；转换工件，生产采纳前需记录哈希与来源）。
- `plus_small`：直接用仓库内生产权重，无新下载。
- + 家族 Medium/Large：官方无直链（见更正清单），暂缺。

## 路线判读（调研对问题的回答）

「有没有效果更好的车道线专精模型」拆成两层：

- **架构层有更强的**：CLRNet 系（夜间/遮挡公开数字最强，正对细线与断续）与 UFLDv2（混合锚正对
  远场近水平边线）在公开基准上明显强于基线同族的行锚分类法；但它们都是**线坐标族**，换架构 =
  重写概率图消费端，且 CPU 存疑、DML 依赖深度替换后的算力窗口。
- **数据层是共同前置**：两条路线都必须用游戏画面微调（零微调不成立）。本游戏的几何先验
  （四车道直道、车道线近水平直线、标定已知、帧库现成）使**几何规范化的伪标签 + 小样本全监督
  微调**成为最省人力且可核验的路径——替代公开文献里 LiDAR/外部真值的角色。
- P1 补充实测（见上节）：在「同接口、≤5MB、当天可得」的集合内，GPU 档原版 TwinLiteNet
  已经比现役 Small 召回更高且同速——**若深度替换腾出 DML，这是当下性价比最高的一步**；
  微调与 + Medium/Large 变体是接下来的增量。
- 因此执行顺序是：**① 深度替换落地后把线观测切 DML，试换原版 TwinLiteNet 权重（零改动）；
  ② 补标注（伪标签管线），微调现役基线；③ + Medium/Large 权重可得后同表补测，
  A/B 实测裁决**。「更好的模型」由实测裁决，不由论文数字裁决。

## 探针计划（下一步；未跑不得写成结论）

1. **P0 失效取证**：从 trace/调试图找出「检测数量不足」的典型段（按 RULES 适用域：日间正常
   光照 × 天气随机，分远场/遮挡两类），统计现役模型逐帧缺线率，并与「画面里实际可见的线」
   对账（区分模型漏检与线本来不可见）——把「某些环境」变成可分类的量化事实（判决统计前置
   检查四问适用；身份先于统计量）。P1 首眼已给出参考基线：现役中位 2 条、双侧配对 31~62%。
2. **P1 首眼对比：已跑，读数见上节。** 后续新候选权重入 `weights/` 后同命令补测
   （探针按模型名自动识别是否带填充；新模型需自 recognizable 的 `ll` 输出头）。
3. **P2 伪标签管线最小验证**：标定投影 × 现役模型共识出掩码 → 人工抽检 100 帧 →
   单任务微调 Small（lr 1e-4 量级、少 epoch）→ 同一质量门对比前后远场检出数。
4. **P3 延迟实测（淘汰判据）**：P1 已给首眼数据（CPU 4 线程与 DML 双档，见上节表）；
   新候选入 `weights/` 后按同口径补测；CPU >30ms 且无 DML 窗口的候选出局。
5. **停止条件**：P0 若「缺线」集中在画面里本来没有线的段（HUD 遮挡/全遮挡），那是采样问题
   不是模型问题——先换采样再谈模型（教训同 speedrush_vision：「判模型失效之前先确认画面里有
   目标」）。

## 主要引用（外部事实，全部为 2026-11-11 实际访问）

- TwinLiteNet+ 论文/仓库：[arXiv:2403.16958](https://arxiv.org/abs/2403.16958)、
  [chequanghuy/TwinLiteNetPlus](https://github.com/chequanghuy/TwinLiteNetPlus)（MIT，2026-05 仍活跃）
- CLRNet 论文/仓库：[ar5iv 2203.10350](https://ar5iv.labs.arxiv.org/html/2203.10350)、
  [Turoad/clrnet](https://github.com/Turoad/clrnet)（Apache-2.0）；社区 ONNX：
  [xuanandsix 转换仓](https://github.com/xuanandsix/CLRNet-onnxruntime-and-tensorrt-demo)
- CLRerNet：[hirotomusiker/CLRerNet](https://github.com/hirotomusiker/CLRerNet)（WACV 2024，LaneIoU）
- CLRKDNet：[weiqingq/CLRKDNet](https://github.com/weiqingq/CLRKDNet)、
  [arXiv:2405.12503](https://arxiv.org/abs/2405.12503)
- UFLD v2：[cfzd/Ultra-Fast-Lane-Detection-v2](https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2)、
  [arXiv:2206.07389](https://arxiv.org/abs/2206.07389)（TPAMI 2022）；
  [PINTO #324 预转 ONNX](https://github.com/PINTO0309/PINTO_model_zoo/blob/main/324_Ultra-Fast-Lane-Detection-v2/download.sh)
- UFLD v1：[cfzd/Ultra-Fast-Lane-Detection](https://github.com/cfzd/Ultra-Fast-Lane-Detection)（停更 2022-12）
- LaneATT：[lucastabelini/LaneATT](https://github.com/lucastabelini/LaneATT)；
  BézierLaneNet：[voldemortX/pytorch-auto-drive](https://github.com/voldemortX/pytorch-auto-drive)（BSD-3）
- 排除项核实：[SRLane](https://github.com/passerer/SRLane)（归档+无 LICENSE）、
  [zhang-tao-whu/e2ec](https://github.com/zhang-tao-whu/e2ec)、
  [aliyun/conditional-lane-detection](https://github.com/aliyun/conditional-lane-detection)、
  [zkyseu/O2SFormer](https://github.com/zkyseu/O2SFormer)、
  [CAIC-AD/YOLOPv2](https://github.com/CAIC-AD/YOLOPv2)、
  [datvuthanh/HybridNets](https://github.com/datvuthanh/HybridNets)
- 数据集许可：[BDD100K 条款](http://bdd-data.berkeley.edu/download.html)、
  [CULane 条款](https://xingangpan.github.io/projects/CULane.html)、
  [TuSimple benchmark](https://github.com/TuSimple/tusimple-benchmark)（无一手许可文本）
- 域适应实证：[CARLANE（NeurIPS 2022）](https://carlanebenchmark.github.io/)；
  蒸馏先例：[SAD（ICCV 2019）](https://arxiv.org/abs/1908.00821)、
  [LaneCorrect（IJCV 2025）](https://arxiv.org/abs/2404.14671)、
  [MLDA（CVPRW 2022）](https://arxiv.org/abs/2206.10692)
- 2024 综述配套清单：[Awesome-Lane-Detection](https://github.com/Core9724/Awesome-Lane-Detection)
  （[综述 arXiv:2411.16316](https://arxiv.org/abs/2411.16316)）
