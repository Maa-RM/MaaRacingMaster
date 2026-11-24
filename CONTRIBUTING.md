# 贡献指南（Contributing Guide）

> **本文是什么**：开发自定义插件、参与社区和向上游提交改动的指南。
> **本文不是什么**：不是架构与实现说明（见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md)）；不是 AI 协作守则（见 [AGENTS.md](AGENTS.md)）。
> **信源等级**：L3（导航与摘要）—— 可作「怎么参与」的依据；架构与规则细节一律回到其 home。

## 开发自己的插件

有想做的玩法？欢迎 [Fork MaaRacingMaster](https://github.com/Maa-RM/MaaRacingMaster/fork)，从自己最想实现的一小段流程开始。

1. 按[开发环境搭建](#开发环境搭建)克隆自己的 fork、安装依赖，再按 [README 的编译说明](README.md#4-编译-gui-并启动)启动界面。
2. 按 [活动插件样板](templates/plugin/README.md) 的启用步骤创建插件。先确认新插件能出现在模块列表里，再加入自己的玩法逻辑。
3. 插件可以留在自己的 fork 里维护和分享。想把插件或改进带回本项目，也欢迎 [向上游提 PR](#向上游提交改动)。

> **⚠️ 合规红线（必须遵守）**
> 本项目为游戏自动化技术研究与学习项目，**严禁**用于代练、外挂、作弊、刷榜或任何影响游戏公平与服务器排名的用途。所有贡献必须服务于"技术学习与个人效率工具"这一定位。详见 [README 免责与合规声明](README.md)。

---

## 我能帮上什么忙？

| 方向 | 说明 | 适合谁 |
|------|------|--------|
| 🐛 报 Bug | 使用中遇到异常，先查 [已知问题](https://github.com/Maa-RM/MaaRacingMaster/issues) 是否已有报告 | 所有用户 |
| 💡 提需求 | 描述你想要的活动 / 平台能力，说明使用场景 | 所有用户 |
| 🧹 修 Bug | 认领 Issue，修复后提 PR | 初级贡献者 |
| 🧩 写插件 | [做一个自己想用的插件](#开发自己的插件) | 想尝试插件开发的人 |
| 📝 文档 | 完善 README / CODE_WIKI / 快速开始指南 | 所有贡献者 |
| 🎨 素材 | 优化模板匹配图片、UI 界面、调试工具 | 设计师 / 前端 |

> 找不到方向？看 Issue 里带 `good first issue` 标签的任务，或直接开 Issue 描述你想做的事。

---

## 环境准备

### 运行环境

- **Windows 10/11 64-bit**
- **Python 3.11**
- 游戏窗口分辨率 **1280×720**
- 管理员权限（窗口截图必需）

### 开发环境搭建

从自己的 fork 克隆。将 `YOUR_USERNAME` 替换成你的 GitHub 用户名，或从 fork 的 **Code** 菜单复制克隆地址。

```bash
git clone https://github.com/YOUR_USERNAME/MaaRacingMaster.git
cd MaaRacingMaster
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

- **运行时依赖版本范围的唯一人写点是 `requirements.txt`**（逐件带「下一 major」上界，理由写在该文件头部注释）。打包元数据与本地可编辑安装在构建时读取这份文件，不在别处另写范围——两处各写一遍会漂出不同的许可范围，而漂出来的那一份随 wheel/sdist 元数据对外发布。新增或调整运行时依赖只改这里；只在训练、门禁、测试里用的包走可选依赖段。

- 启动 GUI：运行编译产物 `apps\MaaRacingMaster.Shell\bin\Debug\net8.0-windows10.0.19041.0\win-x64\MaaRacingMaster.Shell.exe`（exe 自身 manifest 自动 UAC 提权）
- 独立调试 sidecar（不经 GUI）：`.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar`
- 工具按归属放：跨插件现役工具 → `tools/`；单插件工具 → `plugins/<id>/tools/`；应用/子系统专用 → `apps/<app>/tools/`；活跃实验 → `tools/experiments/<主题>/`（README 标 `status: active`）。退役即删，历史由 git 保存；本机二进制与草稿走 gitignore，不入库。

---

## 架构速览（改代码前必读）

平台核心原则：**控制依赖传播 + 治理资源所有权**。

- **模块经能力接口（capability）接触宿主**：模块只能通过 `capture` / `gamepad` / `debug_renderer` 等窄接口办事，**无法获得高权限宿主对象**。
- **资源所有权统一治理**：虚拟手柄用租约（`with ctx.gamepad.acquire()`）表达借还；渲染器由 `ActivityContext` 的 `ExitStack` 托管生命周期，退出（含异常）自动释放。
- **克制不滥用抽象**：高权限对象必须隔离、有生命周期的资源必须治理；稳定纯函数直接依赖即可。

关键文件位置（完整导航见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md) 的「我要改 X，应该去哪里」）：

| 文件 | 职责 |
|------|------|
| `maaracing_master/core/controller.py` | 主控编排（生命周期 + 能力门面 `ActivityContext`） |
| `maaracing_master/core/capabilities.py` | 能力 Protocol + 最薄 adapter |
| `maaracing_master/core/base.py` | `ActivityContext` / `ActivityModule` 基类 |
| `maaracing_master/core/registry.py` | 插件注册表 |
| `maaracing_master/plugins/<id>/` | 活动插件（自包含目录，各含 `manifest.py` + `resources`） |
| `maaracing_master/core/gamepad_cursor.py` | 光标导航引擎 |
| `apps/MaaRacingMaster.Shell/` | WinUI 3 图形界面（Python sidecar 承载业务） |

**改代码前请先读对应对象的知识文档**：平台核心层见 [core/CODE_WIKI.md](maaracing_master/core/CODE_WIKI.md)，活动域见 `plugins/<id>/CODE_WIKI.md`，MaaFW 协议与红线见 [docs/MAAFW_GUIDE.md](docs/MAAFW_GUIDE.md)。

---

## 向上游提交改动

上游指本项目的原始仓库。提交前可参考 [项目地图](docs/CODE_WIKI.md) 和 [协作守则](AGENTS.md)。

### 1. 查找任务与讨论

- 先在 [Issues](https://github.com/Maa-RM/MaaRacingMaster/issues) 搜索是否已有相关讨论，避免重复。
- 报 Bug 请使用 **Bug 模板**，尽量包含：复现步骤、期望行为、实际行为、日志 / 截图、环境信息（Python 版本、系统版本、分辨率）。
- 提需求请使用 **Feature 模板**，说明使用场景与期望效果。

**维护者台账（`label: self-todo`）：** 开发方向与未定性疑点记入常驻台账 issue——一行一条，只写「一句话 + 涉及位置」。满足任一条件即升级为独立 issue 并从台账删行：① 需跨会话追踪或与 commit 关联（`closes #N`）；② 需他人复现或讨论；③ 已进入实施、有独立验收面。已定性且另有归宿的直接迁走，不留副本。

### 2. 开发

同步 fork 的 `master` 后，在本地从它创建分支，**一个主题一条分支，合入即删**（命名规则见[第 4 节](#4-我们接受什么样的-pr)）。

```bash
git fetch origin
git checkout -b feat/your-feature origin/master
# 或 git checkout -b fix/your-bugfix origin/master
```

**开发约定：**

- 新增模块必须走 `core/registry.py` 的插件发现（扫 `plugins/*/manifest.py`）+ 能力接口，不直接引高权限宿主对象。
- 修改已有插件的活动流程时，请先查看该插件目录下的 `RULES.md` 和 `CODE_WIKI.md`。
- 遵守 [AGENTS.md](AGENTS.md) 的协作守则（面向用户的输出、代码注释与交付文档使用简体中文）；代码风格为 4 空格缩进。

### 3. 提交与推送

```bash
git add <具体文件>
git commit -m "feat: 简述改动内容"
git push -u origin feat/your-feature
```

提交信息建议使用约定式提交：`feat:` / `fix:` / `refactor:` / `docs:` / `chore:` 等前缀。

**提交前跑什么（跑触碰面，不跑全仓）：**

- 改动相关的 `pytest` + 改动文件的 `ruff`；
- 动到真源（`plugins/*/resources/` 的图与锚点）时加跑 `python tools/pipeline/truth.py`；
- 动到 `tools/experiments/`、`docs/plan/`（新建或改动工作台文件）、正式文档中的实验/plan 指针、或卫生基线时加跑 `python tools/check_repo_hygiene.py`（AGENTS 红线 6 与红线 4 隐私扫描；基线只减不增；实验退役四步见 [tools/experiments/README.md](tools/experiments/README.md)，plan 头部三行与退役四步见 [docs/README.md](docs/README.md)）；
- 动到**声明为基线的数据方案**（真源是该域的复算报告，登记在对应实验区 README）或其报告时，按该目录登记的命令跑锚点校验，并对基线方案加 `--plan-strict`：方案里的数只能写成锚点，裸小数直接判失败；其余方案只跑锚点解析。
- 改到**跨模块共享契约**（core 能力接口、RPC 白名单、分层边）时建议自愿加跑全仓——影响面跨模块，早发现比等 CI 划算。
- 新增了抽象（core 目录、共用 pipeline、通用协议、新通路 / 新页面）时，先答一句「**第二个使用者是谁、今天是否真实存在**」——判据见 [AGENTS.md](AGENTS.md)「作业纪律与提交红线」中「抽公共层」那一条；答不出就不抽。

全仓 `ruff` / `pyright` / 全量 `pytest` **不在提交路径上**，完整反馈看 CI 与发版 gate，本地重跑一遍只是把等待时间翻倍。触发口径按场景分三种：

- **推送**：master 与 `release/**` 的直接推送触发 CI；**普通分支推送不触发**，只有你本地跑过的检查；
- **PR**：触发全量 CI（单测 + 静态检查 + 类型检查 + Shell 编译），这是拿到全仓反馈的入口；
- **发布标签**：`release.yml` 的发布前单测与 CI 读取同一份测试配置，测试通过后才会发布。

**跳过（skip）的口径**：CI 不按跳过数量设门槛，`pytest -ra` 会逐条打印原因。两类跳过是预期内的：

- `local_asset` 标记：依赖本机金标数据、模型权重或硬件的用例，资产缺失即跳过，不计入 CI 保护面；
- 依赖上限：Windows-only 能力、`node`、`rapidocr` 等可选依赖缺失时的 `skipif`。

新增跳过必须二选一：打 `local_asset` 标记，或在 `skipif(..., reason=...)` 里写清缺失的是哪一项。没有标记、没有原因的静默跳过视为测试写坏。

### 4. 我们接受什么样的 PR

**接受：**

- **一个 PR 一个主题**：能一句话说清「做了什么、为什么」。混杂的改动请拆成多个 PR——拆开评审更快，也不会因为一处有问题连累全部。
- **从上游 `master` 切出、命名合规**：`type/slug`，类型取 `feat` / `fix` / `chore` / `hotfix` / `release`，slug 全小写、只用 `a-z0-9.-`。命名不合规会被 CI 直接拦下（`dependabot/**` 除外）。
- **亲测过，并写清测了什么**：描述里给命令与结果（`pytest tests/xxx.py -q` → 12 passed，或一张调试截图 / 日志片段）。
- **只改该改的东西**：不顺手重排无关代码、不夹带格式化、不改无关文件的换行符。
- **修 Bug 带回归用例**：能做到就加；做不到在描述里说明为什么这个场景没法固化成测试。
- **依赖升级单独成 PR**（dependabot 自动开的不受此限）。

**不接受：**

- **为让 CI 变绿而放宽门禁**：删用例、加 `skip`、改断言、改 workflow 的必需检查。这类改动必须有「为什么这个失败是预期行为」的说明，否则不会合。**改门禁文件（`.github/workflows/**`、`tools/pr_conventions.py`）的 PR 必须逐行复核**：准入检查的工作流定义本身来自待审 PR，机检挡不住「把 job 换成空跑」；本仓库目前也没有经验证的独立准入检查（仓库级 Actions 策略为空、无上报 App）——门禁的可信度靠这一步人工把关，不靠「大概没问题」。
- **一个 PR 里既有功能改动又有大规模重构或格式化**：想重构请单独开 PR，先讨论。
- **只写「已测试」不给证据**。
- **引入未申报的依赖、来源不明的包或二进制**。
- **生成物进版本库**：`debug/` 截图、模型权重、日志、本地配置。

**规模**：建议单个 PR 在 **400 行 / 20 文件**以内；超过时在描述里说明为何不能拆。这不是硬门槛——维护者自己的收口 PR 也会超线，但会写理由。

**分支寿命**：主题分支目标寿命约两天，合入即删；跨周的大改动请拆成能独立合入的里程碑，每个里程碑都保持 `master` 可运行。

**AI 参与**：在描述里写明哪些部分由 AI 生成、你验证了什么（跑了哪些命令、看过哪些输出）。只声明「AI 写的、没跑过」的 PR 会被要求补验证。

### 5. 提 PR

- 从 fork 的开发分支向上游仓库的 `master` 提 PR；头分支名按第 4 节的命名规则。
- 使用 **PR 模板**，说明改动内容、动机、验证方式。
- 请在描述中**勾选自测项**（至少运行通过 / 无回归）。
- PR 解决了已有的 Issue 时，可以用 `Closes #123` 关联。

### 6. Review 与合并

- 维护者会尽快 review；如果 PR 较大，建议先开 Issue 讨论设计再实现。
- 合并采用 **Squash merge** 保持 master 历史整洁：一个主题恰好一个 commit，头分支合入后自动删除。

---

## 版本与发布

- 严格遵循 **SemVer 2.0.0**（`主版本.次版本.修订号`）。
- **Git Tag 是唯一信源**：`setuptools-scm` 从 tag 自动推导版本号，**禁止手动改源码版本号**。
- 0.x 开发阶段 tag 为 pre-release `v0.x.y-dev.N`。
- 发布流程由维护者通过 `project-update` skill 执行（清理 → 版本号 → 更新日志 → PR → tag → CI/CD 自动建 Release）。

---

## 行为准则

参与本项目即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：互相尊重、聚焦技术、不对游戏公平性做任何破坏。

---

## 感谢

每一份 Issue、每一个 PR 都是对项目的支持。技术交流、踩坑分享、架构讨论同样欢迎——这正是这个项目的初衷。🎉
