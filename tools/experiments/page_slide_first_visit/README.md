# 切页动画首访缺失 · WebView2 墙钟动画快进探针

status: resolved（2026-11-23 修复落地壳侧 `--disable-gpu-rasterization`，机制与验收数据见本文与 plan；提交后按 experiments README 出口流程退役）

## 问题

真实 App 中「本会话第一次切到某个页面大类（数据 / 设置 / 关于）」时，切页滑入动画
（`.page-slide-in`，时长 `var(--maarm-duration-slow)`）不播放，用户观测为**页面以最终
位置瞬间出现**；同页再访动画正常。必然复现。

## 已证（隔离无头复现，引擎版本与 WebView2 运行时同为 Edge 157）

- **快进机制成立**：`.page-slide-in` 是墙钟动画——动画时钟跟随挂钟推进，不等帧产出。
  帧产出一侧停顿跨过动画时长时，下一帧呈现即终态。复现样本：帧产出停顿约 244ms 时，
  动画时钟从 5ms 直接跳至 249ms（透明度 0.07 → 0.999 一步完成），观感即「瞬间出现」。
- **首访成本存在**：首访对从未显示的子树做首次样式 + 布局 + 光栅，实测首布局
  10–36ms、首帧 17–250ms（会话内第一次切页最贵）；再访全部走缓存（1–6ms）。
- **will-change 非判别因素**：摘掉 `.page-slide-in` 的 `will-change` 的 A/B 无差异
  （动画本身就会提升图层）。
- **未闭环**：隔离环境（空闲无头 + 桩内容）的成本吃不满动画窗口；预热把首访冷成本
  前移到启动期的改动在真实 App 中「无改善」——但真实 App 实际加载的前端版本从未被
  验证过（CDP 通道此前从未建立成功），「无改善」是否等于「改动未被加载」未定。
  本探针的**前端指纹**输出即为此设。

## 探针

- `probe_launch.cmd`：自提权启动仓库 Debug 构建（CDP 9223），先清掉旧实例与残留
  WebView2 浏览器进程（同数据目录浏览器进程会被复用，参数以首个启动者为准）。
  **ASCII-only**：带 CJK 内容的批处理在 GBK 代码页下解析不稳（前代启动脚本闪退，
  根因未定位，此变体规避整类问题）。每步写 `_out/launch.log`。
- `probe_attach.js`（依赖 node 与全局 `@playwright/mcp` 自带的 playwright-core，运行时
  经 `npm root -g` 解析）：
  - `install`：装被动记录器（tab 点击时刻、page-slide 三事件、animationstart 自激活
    900ms 逐帧采样动画时钟 vs 墙钟、longtask、事件计时）+ **前端指纹**（帧资源 URL =
    仓库源码还是打包副本；app.js/settings.js 关键标记）→ `_out/fingerprint.json`。
  - `dump`：导出记录 → `_out/dump-<时间戳>.json`，stdout 给时间线摘要（含每段动画的
    maxSkip = max(动画时钟增量 − 墙钟增量)）。
  - 只装观察器，不模拟任何交互。
- `isolated_repro/`：隔离复现资产。`stub.js` 为 WebView2 桥桩（`get_initial_state` 最小
  数据集），`driver3.js` 为多轮首访/再访采样器（运行前把仓库 frontend 拷贝为本目录
  `frontend_copy/` 并按根目录 README 的桩注入方式插入 `stub.js`），三个 out JSON 为
  已采数据（out2_15 含 244ms 快进实况样本）。
- `inject_control.js`（2026-11-23）：对照实验注入器，经 CDP 运行时注入、**不改前端文件**。
  `status <port>` 页面状态；`predecode <port>` 齿轮封面点击前 load+decode（对照 A）；
  `delay <port> <ms>` 延迟 `get_module_dashboard` 响应（对照 B）；`preready <port>`
  预取看板 payload 后同参调用零等待（对照 C）；`strip <port> <preset>` 注入一次性
  `<style>` 隐藏/削平目标效果面（`data-no-perf` / `data-no-right` / `data-bare` /
  `manage-no-grid` / `manage-flat`），用于首访着色器来源拆分。与 `gui-debug.cjs compare`
  或 `trace_visit.js` 连用。
- `trace_visit.js`（2026-11-23）：单次冷首访的 trace 归因，支持 `data` 与 `manage` 两个
  入口——rAF 采样定位最大帧停顿窗口，页内 `console.timeStamp` 锚点对齐 trace 时基，
  输出窗口内各进程/线程事件分布；raw 落 `_out/trace-cold-<入口>-raw.json`。gpu/skia 类目已含。
- `trace_summary.js`（2026-11-23）：读 trace 产物输出停顿摘要（GPU Flush top、着色器
  缓存 load/store、ANGLE worker、渲染器主线程最大事件）。
- `kill_residual.ps1` / `list_debug_procs.ps1`：实验收尾清理与进程排查（**注意**：实测
  本机 windows-mcp PowerShell 通道非提权，杀不动提权壳实例；见 plan 探针裁决节的操作坑）。
- `_out/refs/`：机制坐实用官方源码存档（Chromium raster_decoder.cc / gr_shader_cache.{h,cc}、
  Skia GrContextOptions.h、ANGLE README），引用与行号见 plan「着色器编译原理」一节。

## 2026-11-23 裁决摘要（详见 plan §「探针裁决」，含外部复核勘误）

异步加载（等待/预就绪）与封面解码均证伪：数据页冷首访最大帧停顿在 基线 127.9ms /
延迟 500ms 144.5ms（占位期间原位发生）/ 预就绪 166.9ms / 再访 ≤22ms；齿轮预解码后
222.2ms（基线 222.3ms）。trace 归因（data 缺口 127.8ms、manage 缺口 244.3ms，同签名）：
渲染器主线程空闲（data 仅 Layout 24.3ms），GPU 进程被 `DoEndRasterCHROMIUM::Flush`
串行占满（data 79.9/30.4/9.8ms；manage 单段 176.9ms），等待期间
`GrShaderCache::load → ANGLE worker 突发 → store` 循环贯穿缺口（data ~20 组、manage
23/20）→ 停顿为入口子树首次呈现的首用着色器管线编译与串行等待。
勘误三条已并入 plan：click 处理器实为 39.2ms（136 是含渲染等待的 event timing）、
遮罩「时钟冻结」实为 endTime 钳制、渐进式加载仅「正文延迟」形态被证伪（轻外框未测）。
**首访着色器成本分散**：隐藏性能卡 127.8→61.1ms（循环 ~20→12）、隐藏右列 →88.9ms
（14/13），两列各占约半、无单一元凶 → 修复主攻改为替代预热形态（指纹校验下重测）。
数据：`_out/gui-baseline-manage.json`、`gui-baseline-data.json`、`gui-ctrlA-predecode-manage.json`、
`gui-ctrlB-delay500-data.json`、`gui-ctrlC-preready-data.json`、`trace-cold-data.json`、
`trace-cold-manage.json`。

## 判据与标尺

AnimationEvent / `getAnimations()` / PerformanceObserver 均为 Web 标准接口，独立于
待验证实现（标尺独立性 ✓）：

- **快进判决**：某次 animationstart 的逐帧轨迹中，相邻两帧的动画时钟增量显著超出
  墙钟增量（maxSkip ≳ 半个动画时长）→ 时钟在帧停顿期间照走。
- **未启动判决**：切页后 animationstart 未出现（类已挂但动画未创建）→ 另查渲染管线。
- **不播放判决**：三事件齐全、轨迹贴墙钟而用户仍见瞬间出现 → 呈现侧问题，转查
  窗口化合成路径。

## 用法

1. 双击 `probe_launch.cmd`（UAC 点是；App 窗口出现后保持）。
2. `node probe_attach.js install`（可与主代理协作自动执行；指纹随装随落盘）。
3. 用户复现：数据 → 设置 → 关于 各首访一次，回主控，再各访一次（对照）；保持 App 开着。
4. `node probe_attach.js dump` → `_out/dump-*.json`。
5. 判决前过 experiments README 的「判决统计前置检查」四问（同层 / 归一 / 身份 / 量变）。

## 数据与退役

数据落 `_out/`（gitignored，可重跑复算）。停顿源定位、结论兑现 home（前端 README 渲染
相关章节或壳 README）后按 experiments README 出口四步退役。
