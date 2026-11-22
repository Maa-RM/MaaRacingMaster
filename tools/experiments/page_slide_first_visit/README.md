# 切页动画首访缺失 · WebView2 墙钟动画快进探针

status: active

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
