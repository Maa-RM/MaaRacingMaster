# 活动插件样板（Plugin Template）

> **本文是什么**：活动插件样板的构成与契约——目录结构、改名步骤、能力接口用法。
> **本文不是什么**：不是插件平台架构（见 [docs/CODE_WIKI.md](../../docs/CODE_WIKI.md) 与 [core/CODE_WIKI.md](../../maaracing_master/core/CODE_WIKI.md)）；不是 MaaFW 协议口径（见 [docs/MAAFW_GUIDE.md](../../docs/MAAFW_GUIDE.md)）。
> **信源等级**：L2 —— 可作「新插件怎么起」的直接依据；样板代码本身即真源（`templates/plugin/`）。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../AGENTS.md)。

开发新活动插件的起点：本目录是一份**最小完整、可运行骨架**，演示平台对插件的全套契约。

> **为什么放在 `templates/` 而不是 `maaracing_master/plugins/`**：
> [core/registry.py](../../maaracing_master/core/registry.py) 只扫描 `maaracing_master/plugins/*/manifest.py`。
> 样板在 `plugins/` 之外，**不会被主程序识别为活动模块**，GUI 模块列表里也看不到它；
> 复制进 `plugins/<id>/` 并完成三处改名后才会被装载。

## 目录结构

```
templates/plugin/
├── manifest.py          # 清单：ID + MODULE_CLASS（registry 发现入口）+ 可选声明
├── __init__.py          # PLUGIN_DIR / RES_DIR 常量（资源自引用）
├── module.py            # SampleModule：ActivityModule 最小完整实现 + 声明面示范
└── resources/           # 插件专属资源（自包含，随插件分发）
    ├── image/           # 模板图（导航/阶段判定用）
    ├── pipeline/        # 导航图真源：MAA Pipeline 节点 JSON（v4，编辑器与引擎同读一份）
    └── policy/          # 策略表与感知执行规格真源（`action: "Custom"` 节点指向它）
```

## 启用步骤（复制即装）

1. **复制**：把 `templates/plugin/` 整个目录复制为 `maaracing_master/plugins/<你的插件id>/`。
2. **改 `manifest.py`**：`ID = "<你的插件id>"`（与目录名一致，全局唯一）。
3. **改 `module.py`**：类名 `SampleModule` → `<你的模块类名>`，同步 `manifest.py` 的
   `MODULE_CLASS = "module.<你的模块类名>"`；填写 `NAME` / `STAGE_ORDER` / `REQUIRES` / `REQUIRED_ASSETS`。
4. **重启 GUI**：模块下拉自动出现新模块（registry 启动时扫描，无需注册代码）。

> 模板自带的示范随复制生效：配置面板（循环间隔）、任务卡摘要、封面与调试存盘档位在
> GUI 里就能看到效果，按需删减；性能行与看板是注释示例，想用再启用。
> 删除 `plugins/<id>/` 目录即卸载。所有资源引用一律经 `__init__.py` 的常量自引用，
> 不依赖 `assets/` 或其它插件的路径，目录搬走即完整带走。

## 各文件职责

| 文件 | 职责 |
|------|------|
| `manifest.py` | 清单：`ID`（唯一标识，GUI 下拉/配置持久化用它）+ `MODULE_CLASS`（`文件名.类名`）；可选有效期 / 成就目录指针 / 资产安装器声明见文件内注释 |
| `__init__.py` | `PLUGIN_DIR` / `RES_DIR` 路径常量；插件内所有资源路径从这里派生 |
| `module.py` | 模块实现：生命周期（`start`/`stop`/`cleanup`）、阶段状态机、声明面（配置面板/封面/看板/性能）与能力取用示范 |
| `resources/` | 插件专属资源；`REQUIRED_ASSETS` 声明的路径相对插件根目录 |

## 模块类契约速查（来自 `core/base.py`）

**必填**：

| 成员 | 说明 |
|------|------|
| `ID` / `NAME` | 标识与显示名 |
| `STAGE_ORDER` | 阶段清单；GUI 阶段列表与断点下拉的唯一来源 |
| `current_stage` | property；GUI 运行状态与断点定位读取 |
| `start(start_from)` | 模块入口（独立线程）：`ctx.connect()` → 断点解析 → 主循环；返回 True=自然结束 / False=启动校验失败，不得返回 None |
| `stop()` | `ctx.lifecycle.request_stop()`，主循环 100ms 级响应 |
| `cleanup()` | 释放模块自有资源；返回 False=收尾未完成（编排层保留资源并拒绝下一轮） |

**启动约束**（类属性 = 完整声明；同一模块在不同配置下条件不同，覆盖查询按次求值）：

| 成员 | 说明 |
|------|------|
| `REQUIRES` | 需要的可选能力集，启动前校验；`"gamepad"` 需装 ViGEmBus 驱动 |
| `required_capabilities(config)` | 本次启动需要的能力（默认即 `REQUIRES`）；不接管输入的模式在此放宽 |
| `REQUIRES_GAMEPAD_EXCLUSIVE` | 是否要求物理手柄断开（虚拟手柄独占） |
| `requires_exclusive_gamepad(config)` | 按配置求值（默认取类属性）；需要物理手柄在位的模式（如录制驾驶）在此放宽 |
| `REQUIRED_ASSETS` | 插件自带必需资源，启动前逐项存在性检查，缺失即拦截 |

**可选声明**（不声明 = 无该功能，宿主按缺省渲染；模板里激活的看 `module.py` 注释）：

| 成员 | 说明 |
|------|------|
| `DEFAULT_MODULE_CONFIG` | 用户可设置配置项的键与默认值；同时是存档回填白名单——不加进这里不会被保存 |
| `CONFIG_UI_CONTRACT` / `CONFIG_UI` | 配置面板声明（控件五型 switch/segmented/select/number/text、文案、显隐、按值换文案）；宿主通用渲染，插件**不提供**自带 HTML/JS——不够用就扩控件类型并升契约版本 |
| `set_module_config(config)` | 配置注入口：controller 启动时注入实例、GUI 写缓存也走它；非法值静默修正，不抛 |
| `normalize_config(config)` | 旧键折算与不变量；宿主在读回存档与接收写入前各调一次 |
| `config_choices()` | 下拉型字段的动态候选；表单里只写引用键，不复制候选清单 |
| `config_summary(config)` / `config_status()` | 任务卡摘要 / 面板运行状态行（文本由模块产出，宿主只校验与渲染） |
| `COVER` | 封面文件名（相对 `resources/`）；定位与 URL 解析在 `core/covers.py`，未声明用通用默认封面 |
| `DEBUG_SAVE` | 统一调试流存盘档位：`on`（默认，写运行目录）/ `self`（模块自有取证，统一流只记段边界）/ `off` |
| `manual_input_active()` | 人工接管期声明：此时段按键不当急停信号（录制驾驶等玩家自己操作的阶段） |
| `PERF_CONTRACT` / `PERF_ROWS` / `THREAD_LABELS` | 性能面板声明行 + 线程中文别名；运行期在 `read_perf_snapshot()` 只给值，判级归 core（缺失即缺失，显示「—」不冒充 0） |
| `DASHBOARD_CONTRACT` / `read_dashboard()` | 数据看板：版本化 block 载荷（stats/list/text/path/action/empty）；classmethod，不得依赖模块实例 |

配置面板契约与字段白名单见 `core/config_ui_contract.py`（决策背景见
[ADR-0016](../../docs/adr/0016-contract-core-模块配置面板由插件声明宿主渲染.md)）；
性能契约见 `core/perf_contract.py`（判级权二分）；看板契约见 `core/dashboard_contract.py`。

**主循环铁律**：每帧检查 `ctx.lifecycle.running`；睡眠一律 `ctx.lifecycle.sleep()`，
禁止 `time.sleep`（无法响应停止）；单帧异常自行兜底，不要让一帧失败杀死整个模块。

## 能力门面（`ctx`）

模块与宿主的全部交互走 `ActivityContext` 窄接口，拿不到高权限宿主对象：

| 能力 | 用法 | 可用性 |
|------|------|--------|
| `ctx.capture.screenshot()` | 截图（RGB ndarray，底部 16:9 裁剪）；None = 采集链路故障，**不是**「画面无变化」 | 恒可用 |
| `ctx.capture.frame_with_age()` | (帧, frame_id, 时间戳, 帧龄 ms)；需要新鲜度判断时用它 | 恒可用 |
| `ctx.lifecycle.running / sleep() / request_stop()` | 停止信号 / 可中断睡眠 | 恒可用 |
| `ctx.connect()` | 幂等连接游戏窗口（720p 校验内置） | 恒可用 |
| `ctx.window_foreground` | 游戏窗口是否前台；前台鼠标点击的安全前置（程序不主动抢前台） | 恒可用 |
| `ctx.verify_frame_client(w, h)` / `ctx.check_window_aspect()` | 帧尺寸 vs 客户区 1:1 / 客户区约 16:9（模板与 ROI 按 720p 归一化的前提） | 恒可用 |
| `ctx.debug_renderer.renderer(r)` | 调试 HUD 渲染租约（PEEP 叠加），经 `ctx.enter_context()` 接管；渲染分支参考 `core/debug.py` | 恒可用 |
| `ctx.gamepad.acquire()` | 虚拟手柄**租约**：进入取用、退出归零归还；`persistent_adapter()` 长期持有、`reset_device()` 销毁重建（活跃租约存在时必须抛错） | 需 `REQUIRES` 声明 + ViGEmBus |
| `ctx.bind_tasker(tasker, resource)` | MAA 深度绑定（Pipeline 驱动型活动） | 需 WGC 中心采集器就绪（绑的是帧注入控制器，插件侧无同步截图通道） |
| `ctx.enter_context(cm)` | 登记资源到生命周期栈，模块退出（含异常）自动释放 | — |
| `ctx.debug / proj / click_mode / intent_mode / hwnd` | 调试器、项目根、点击方式、意图开关、窗口句柄（只读事实） | 恒可用 |

## core 共享引擎与工具速查

跨插件复用的基建都住 core，插件**直接 import**（插件自包含契约禁止插件互相 import，
跨插件复用只能落地在公共层）：

| 能力 | 入口 | 用法与坑 |
|------|------|----------|
| 日志与组协议 | `core/logger.py` | `logger.log(msg, level, channel=)`；成组用 `logger.group(...)`。INFO+ 的直接调用必须带 `group_id` 或在 `tests/log_sites.py` 登记站点（机检） |
| 模板匹配 | `core/template_match.py` | 多尺度 `find_any_cs` / `best_match_score`；模板图放插件 `resources/image/` |
| 文字识别 | `core/ocr.py` | 单 ROI 通路关文本检测（毫秒级，HUD 读数）；整帧通路 `recognize_blocks`（秒级）只做未知页面兜底，**勿进高频循环**。「文本怎么解释」属业务层，不上提 core |
| 目标检测 | `core/yolo_detector.py` | 模型放 `resources/models/` 并进 `REQUIRED_ASSETS`（权重不随发行包分发）；类别集由插件定义，core 不内置 |
| 玩法流水 | `core/trace_sink.py` | `get_trace_sink("<模块>")` 注册一次；`begin_stage` / `append` / `end_stage`，落点随日志会话目录（`trace/<模块>.jsonl`） |
| 性能上报 | `ActivityModule.read_perf_snapshot()` | 配合 `PERF_ROWS` 声明：纯读、常数时间，只给值不判级；没数据就不给键 |
| 成就 | `core/achievement.py` | 定义放插件 JSON + manifest `ACHIEVEMENTS` 指针；运行期 `report_fact(kind, dedup_key, payload)` 尽力而为、永不抛 |
| 重试状态机 | `core/step_machine.py` | 「发动作 → 等后置条件 → 有限重发」的状态转移唯一解释者；调用方只喂 Verdict 收 Command，不自己记重试计数 |
| 键盘读取 | `core/key_state.py` | `key_down("w")` / `read_keys()`；插件不得直接 import `window_utils`（平台件边界） |
| 手柄光标导航 | `core/clicker.py` + `core/gamepad_cursor.py` | 后台（手柄）点击方式与光标趋近引擎；手柄与截图均依赖注入（复用模块的 capture 与手柄租约），不自建 |
| 用户数据目录 | `core/paths.py` | `module_data_dir(module_id)` 按 manifest.ID 授予 `data/<模块>/` 并拒绝越界子路径 |
| 会话目录命名 | `core/session_layout.py` | `fresh_session_dir()` 落盘目录防撞（同秒换名追加序号）；按时刻命名的目录都用它，不自写防撞规则 |

## 常用扩展点

按活动类型从样板增补，均为现成基建：

- **导航真源（v4）**：`resources/pipeline/<图>.json` —— 节点 JSON 即真源，编辑器与引擎
  同读一份，中间没有编译层；局内复杂决策与感知执行规格放 `resources/policy/<表>.json`，
  由图里的 `action: "Custom"` 节点指向。图闭合、数据面装配与分层红线由
  `tools/pipeline/truth.py` 机检（CI 同款），编辑与 ROI 校准台见
  [tools/pipeline/README.md](../../tools/pipeline/README.md)；真源组织口径见
  [docs/MAAFW_GUIDE.md](../../docs/MAAFW_GUIDE.md)。
- **MAA Pipeline 执行通路**：`core/nav_graph.py`（`NavGraph` + `MaaRM_Template`/`MaaRM_Click` 桥）。这条通路已接线——内置启动步骤与生产模块都在用它跑导航图。
- **MAA Pipeline 任务驱动**：`Resource.post_bundle(RES_DIR)` + 自定义
  `CustomAction`（参考 `core/nav_graph.py` 的桥实现与 docs/MAAFW_GUIDE.md）。
- **调试 HUD**：参考 `core/debug.py` 的渲染分支；`ctx.debug` 会话落盘。

## 打包与分发

无需任何配置：`scripts/release/assemble.ps1` 按 robocopy 整包复制
`maaracing_master/` 目录，`plugins/<id>/`（含资源与模型）自动随包分发。
`REQUIRED_ASSETS` 声明的文件缺失时，启动前会给出插件内具体路径提示。

分发语义：打包时刻已过 `manifest.VALID_UNTIL` 的模块不随包分发（判定复用
`core/module_validity`，与运行期置灰同一口径，见 `scripts/release/list_ship_excluded_modules.py`）；
仓库树保留全部插件，源码开发与 CI 门禁不受影响。未到 `VALID_FROM` 的模块照常随包，
由运行期按窗口置灰。
