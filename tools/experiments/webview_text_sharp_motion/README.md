status: active

# webview_text_sharp_motion — WebView2 文字动画发虚的机制判决

> **本实验回答什么**：前端「一有动画文字就糊一截」到底是哪个机制、哪种动画属性安全、
> 「逐帧对齐物理像素」这类换算修复还有没有必要。对象是外部系统（Chromium 合成器 +
> Skia 文字管线）的行为，属 C 类：先查官方文档（检索记录见下），文档不含本机引擎的
> 当前行为细节，故落最小实验判决。
> **信源等级**：L4。结论属 shell 前端（home = `apps/MaaRacingMaster.Shell/frontend/README.md`），
> 实施后本目录按生命周期退役。

## 背景与官方信源检索（2026-11-22）

- 合成层分数偏移文字发糊的历史机制：crbug 40431598（栅格一次、逐帧双线性过滤）、
  crbug 41231035（合成器只对滚动容器像素对齐）；Chromium 长期在合成层禁 LCD 文字
  （`--disable-prefer-compositing-to-lcd-text` 等 switch 存在）。
- LCD（ClearType 式）文字未被正式移除（2024–2025 无 removal 落地），但灰度回退场景扩大中
  （crbug 372873705）；macOS 已全面灰度；WPF 同样规定动画/变换下禁 ClearType（MS 官方文档）。
- 文档缺口：上述 crbug 均为 2021 前后行为，不含 2026 年引擎（本机 Edge 157）是否已修复
  分数偏移重采样——这正是本实验要判决的。

## 方法

- **变体矩阵**（`variants.html`，URL 参数选变体；17 变体 × dsf 1.0/1.25/1.5）：
  重绘路径（无 will-change）vs 合成路径（will-change 常驻层）× 平移（2D/3D、整数/分数
  设备偏移）× scale（重绘/合成/暂停动画中间态/**进行中动画**）× opacity × 居中方式
  （transform -50% vs margin auto）× JS 逐帧中间帧建模。
- **采集**：msedge `--headless=new --screenshot --force-device-scale-factor`，
  GPU 与 `--disable-gpu` 软件栅格双跑对照；`capture.py` 按 manifest 记录每张图的
  CSS 意图坐标（残差真值来自采集端解析式，不从像素反推——绘制管线会把一切对齐到
  整数物理像素，像素反推不出意图）。
- **建模论证（运动中间帧）**：`--headless=new` 的虚拟时间不驱动 rAF 逐帧推进
  （实测截图恒为初始帧），故运动中间帧用「同步写一次中间值」建模：合成器对每帧
  采样当前 transform，静止分数偏移与运动中间帧同一采样路径；CSS 动画用负 delay
  暂停/虚拟时间截停两种方式直接取得动画机制的真实中间态（该路径不受 rAF 限制）。
- **标尺独立（C 类硬约束 2）**：
  1. 已知真值标定——对参考截图施加 PIL 高斯模糊（σ=0.3/0.6/1.2），lap_var 6105→4576→
     2015→202 单调下降，标尺有效后才有资格参与判决；
  2. LCD 探测不依赖任何 Chromium 行为声明——纯灰文字(#333)在纯白底上文本色 RGB 相等，
     边缘像素出现 RGB 散布只能来自 ClearType 式亚像素渲染（物理事实）；
  3. GPU 与软件栅格同几何截图逐位一致（comp_frac 等均为 5572==5572），管线无关。
- **判决同层性**：全部 PASS/FAIL 对比同裁剪框、同字号、同 dsf，只变合成状态/偏移方式；
  dsf 是受控变量逐档分析。

## 复算

```bash
.venv/Scripts/python.exe tools/experiments/webview_text_sharp_motion/capture.py --dsf 1 1.25 1.5
.venv/Scripts/python.exe tools/experiments/webview_text_sharp_motion/analyze.py
# LCD 专项（可选）：--mode headed --lcd（本引擎 headless=new 已含 LCD，见结论 5）
```

产物 `_out/`（gitignore）：PNG + manifest.json + metrics.json。

## 结果（Edge 157.0.4341.60，`--headless=new`）

判决表（analyze.py 输出，2026-11-22）：

| 判决 | 结果 | 关键读数 |
|---|---|---|
| V2a 合成 2D 平移分数偏移与对齐后同锐 | PASS | 5572 vs 5444 |
| V3d 合成 3D 平移（translate3d）与 2D 同锐 | PASS | 5572 vs 5572 |
| V3 整数 dsf 下分数偏移不额外糊（1.0/1.5） | PASS | 7360==7360 / 4654==4654 |
| V4 transform 居中糊于布局居中（12px 文字） | PASS | 1654 vs 2274 |
| V8 重绘路径对分数居中免疫 | PASS | 2221 vs 2274 |
| V5 合成层中间帧与静止同锐（JS 逐帧建模） | PASS | 5444==5444 |
| V6 重绘路径中间帧与静止同锐 | PASS | 6105 vs 6252 |
| V12 CSS 平移动画中间帧与静止同锐 | PASS | 5440 vs 5444 |
| V11a scale 稳定中间态（暂停）不糊 | PASS | 5658==5658 |
| **V11b scale 动画进行中发糊** | **PASS** | **3680 vs 5658（−35%）** |
| V9 合成 scale 静止态不糊 | PASS | 5658==5658 |
| V10 opacity 动画不重采样 | PASS | 5444 vs 4654 |
| **V7a 重绘路径有 LCD、合成层失去 LCD** | **PASS** | **chroma_edge 44.7 vs 4.5（12×）** |
| **V7b 重绘路径中间帧仍保 LCD** | **PASS** | **45.1 vs 3.9** |

残差→锐度相关表（全部非 LCD 截图）未出现「残差越大越糊」的单调关系——分数残差不再
是糊的来源（详见 metrics.json）。

## 结论

1. **「糊一截」的持续来源只有一个：合成层上 ClearType 消失（灰度 AA）**。合成层与
   重绘路径的 LCD 证据差 12 倍（chroma_edge 44.7 vs 4.5），锐度差 −11%（18px 文字），
   小字号更重（12px 居中块 −26%）。静止期留在合成层的元素（本前端 = `.stepper-strip`
   的常驻 `will-change`）永不恢复 ClearType。
2. **平移类动画（2D/3D、CSS transition/animation、JS 逐帧、分数设备偏移）在当前引擎
   已全部被合成器像素对齐，零重采样糊**——crbug 40431598 描述的行为在 Edge 157 不复现，
   与 dsf 无关（1.0/1.25/1.5 三档一致）。
3. **「逐帧对齐物理像素」的修复路线被证伪为不必要**（无害但无收益）：分数偏移不再糊，
   残差与锐度无单调关系。先前基于旧 crbug 的两轮分析假设在此被本实验修正。
4. **scale 动画「进行中」是唯一真实的重采样糊**（−35%）：稳定态（暂停/静止）引擎会按
   有效缩放重栅格并恢复锐利，持续变化中存在光栅滞后窗口。modal 的 scale 进场（300ms）
   属瞬态糊，结束即恢复。
5. **opacity/fade 全程安全**；无头 new 管线已含 LCD（无需有头采集即能测 LCD 证据）。

## 会改变判断的边界

- 引擎版本锚定 Edge 157.0.4341.60（WebView2 常青跟随 Chromium；行为若升级变化需重跑，
  复算成本几分钟）。
- headless=new 与 GPU/软件栅格一致（双跑对照），但 headed Windows 真窗口未采
  （capture.py --mode headed 能力保留）；若实测与实验观感冲突先跑 headed 对照。
- 单一字号（18px 主样本 + 12px 居中块）；更小字号上灰度代价按 −26% 外推方向加重，
  未逐档实测。
- scale「进行中」用虚拟时间截停 CSS 动画时间线取得；JS 驱动的连续 scale 未单独建模
  （同一光栅滞后机制）。

## 出口计划

结论 1/3/4/5 实施后迁 `frontend/README.md`（渲染纪律住户）与必要的机检；随后拔引用、
`git rm -r` 本目录，证据指针写 commit。
