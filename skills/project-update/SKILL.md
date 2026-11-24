---
name: "project-update"
description: "MaaRacingMaster 提交纪律与发版手册：单主干 + 短命主题分支（命名规范、PR 准入三道闸）、commit 粒度、amend 边界、独立工作区与共享检出的最小约束，以及完整发版流程（清理临时文件 → 定版本号 → 更新 docs/update_log.md → 提交到 type/slug 主题分支 → 开 PR 并 squash 合入 → 打 tag 并 push 触发 CI/CD）。Invoke when the user says '发布', '更新项目', '发版', 'release', wants to prepare a new version, or whenever the task will touch the remote repository — push / push tag / push branch / open or merge PR / publish."
---

# 项目更新助手（MaaRacingMaster 发版流程）

为 MaaRacingMaster 项目执行发布前的完整准备流程。本文件反映**当前实际发布模式**
（2026-11 复核）：**单主干 + 短命主题分支——一切工作从 `origin/master` 切出
`type/slug` 分支，一个主题收口即 squash 合入并删除该分支；`master` 历史保持线性，
发版打 tag 触发 CI**。决策依据见 [ADR-0028](../../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md)。

## 权威定义与安装

- **本文件** **`skills/project-update/SKILL.md`** **= 团队发布流程的唯一权威定义**，随仓库入库，协作者可见。

- 各 harness 若以安装副本方式使用技能，副本放自己的安装目录（位置随工具的约定而定，**不构成团队约定**），发版前以本仓库权威文件核对一致后再执行。

> 不做 agent 侧自动回写：方向无判据（「最新版本」缺可判定标准），且会把本机上下文带进仓库。

***

## 提交纪律（任何会写远端历史的操作都适用）

本节是**操作手册**——口径细则在此，[AGENTS.md](../../AGENTS.md) 只保留红线声明。适用于**任何会写远端历史的操作**：push 分支、push 稳定线、打 tag、开 / 合 PR、发布，不限于发版。

### 分支模型（单主干 + 短命主题分支）

- **稳定线**（默认分支 `master`）是唯一的长期分支，**历史线性**：只收**沉淀**——一个主题收口（实现+回归+验收齐了）才经 PR 合入，合并时 CI 自动全量测试——稳定线上每个点都可运行、可发布。稳定线受 **GitHub 规则集（`stable-master-pr-only`）保护**：直接 push 一律被远端拒绝，只允许 **squash 压扁合并**（每个主题恰好一个 commit，master 历史保持一行主题列表），**任何人（含仓库所有者）不可绕过**：规则集 `bypass_actors` 为空，`--admin` 也会被规则集拒绝（传统分支保护的 admin 豁免不覆盖规则集，两套规则叠加）；紧急通道见「对外动作先取得确认」。
- **主题分支**：一切工作从 `origin/master` 切出，**一个主题一条，收口即合入、合入即删**（仓库开着 `delete_branch_on_merge`，合入后远端头分支自动消失）。**分支是脚手架，不是档案**——它的提交不承诺长期存在，不要在其上写「以后还要回来续做」的期待；跨周工作按**可交付里程碑**拆成多条分支，每条都能独立合入并保持 master 可运行。
- **命名规范**（[Conventional Branch](https://conventional-branch.github.io/)，CI 硬拦，ADR-0028）：

  | 类型 | 用途 | 示例 |
  | --- | --- | --- |
  | `feat/<slug>` | 新功能 / 新能力 | `feat/branch-conventions` |
  | `fix/<slug>` | Bug 修复 | `fix/settlement-rounding` |
  | `chore/<slug>` | 文档、CI、依赖、治理、杂务 | `chore/repo-branch-model` |
  | `hotfix/<slug>` | 线上紧急修复（从 tag 或 master 切） | `hotfix/launcher-crash` |
  | `release/<版本>` | 打 tag 前的最后收口 | `release/v0.32.2-dev.2` |

  slug 全小写、仅 `a-z0-9.-`，用连字符不用下划线，需说明用途的分段加在后缀（如 `chore/actions-bump-pkgtest`）。`dependabot/**` 为机器生成分支的豁免，不受此约束。
- **历史例外只留一条**：`archive/dev-d542Bb` 永久保留，作用是让旧草稿线的提交在远端仍可解析（证据锚、历史比对要用）。它**只读**：不新增、不写入、不当合并源；新的长历史一律不再产生。
- **一个主题一份独立工作区**：并行工作时每个主题用独立工作区（`git worktree` 或独立 clone 均可），HEAD 只停在自己的主题分支；共享检出不切到 `master`、不改史、不丢别人未提交内容。解释器与验证入口用仓库声明的环境（见 [CONTRIBUTING](../../CONTRIBUTING.md)），环境放在哪个目录属个人配置。
- 发版顺序随之固定：**先沉淀（PR 合入稳定线）→ 再在稳定线上打 tag**。tag 必须打在稳定线含全部改动的那份上（见发版节）。
- 旧的「长期草稿线 + 压扁沉淀」模型与它带来的假冲突分流、集成提交，已随本模型退役（决策与代价见 [ADR-0028](../../docs/adr/0028-arch-global-分支模型定为单主干与短命主题分支.md)），本节不再保留其操作口径。

### PR 准入（开 PR 前自检）

- **命名**：`type/slug`（见上表）。CI 硬拦不合规的头分支；`dependabot/**` 豁免。
- **体积**：>400 行或 >20 文件属**软线越线**，在 PR 描述里写清为何不能拆；它不阻断，也不要为凑数字拆成「看不见全貌」的碎 PR。
- **门禁自伤**：同一个 PR 既改门禁定义（`.github/workflows/**` 含锁文件、`tools/pr_conventions.py`）、又在测试上做减法（删用例、加 skip、放宽断言）时，必须人工复核并写明理由。**「为变绿改门禁」不接受**。机检由 **base 上的检查器副本**执行、PR 快照只作 diff 输入——改检查器本身的 PR 由旧判据判定，另加「门禁定义改动」警告提示人工复核。
- **一个 PR 一个主题**：人判，无自动检查——主题是否单一由评审决定。混杂时拆 PR，不靠描述掩盖。

### commit 粒度

- **一个 commit 是一个有意义的历史节点**：能独立解释、能独立回滚即可；开发期允许细粒度提交以便回溯、Review 与实验。
- **交付面**（tag、`docs/update_log.md` 小节、Release 正文）才要求语义完整。
- **稳定线 push 是历史整理的不可逆分界**：进稳定线（PR）的内容必须语义完整，**整理发生在开 PR 之前**；稳定线合并由规则集锁定为压扁，历史天然一个主题一个 commit。
- **稳定线禁止对已推送历史 rebase / squash / force push**（AGENTS.md 红线），修正一律新 commit 前进。
- **主题分支不受上述限制**：允许 agent 直接推送；允许对已推送的主题分支历史 rebase / squash / force push（`--force-with-lease`），改写只落自己的主题分支，不触碰稳定线、不改写别人的分支。

### 退役、改名、目录瘦身类提交

提交前全仓 grep 旧名——模块名、节点前缀、章节号、目录名都算。旧名残留会以幽灵登记（日志站点、注册表条目、文档指针）的形式继续被机检当真，或让指向虚空的指针静默通过。grep 范围含生产代码、测试、工具与 L1~L3 文档；L4 历史记录（ADR、`update_log.md`）按定义保留旧名。


### amend 的边界

仅当**两条同时成立**才可 `git commit --amend` 并入上一提交：① 修改仍属于该提交同一逻辑任务的补充 / 修正；② 该提交**未推送**，且该分支没有其他人在用。已推送的 `master` 历史一律新 commit 前进。

### 独立工作区与共享检出

并行或与他人共用检出目录时的最小约束：

1. **每个主题一份独立工作区与分支**：`git worktree add` 或独立 clone 都行，提交落在哪个工作区是确定的。
2. **共享检出不改史、不丢别人内容**：共享索引上的 `amend` / `rebase` / `reset --hard` 一律不做；在共享检出里提交时用 pathspec 只带上本主题的文件（`git commit -m "…" -- <路径>`），提交前 `git diff --cached --name-only` 核一遍。
3. **用完回收**：主题合入后移除工作区（有未提交内容时先确认归属），远端分支由 `delete_branch_on_merge` 自动删除。

环境放在哪、用什么命令跑门禁属个人配置；仓库只要求用 CONTRIBUTING 声明的解释器与验证入口。

### 对外动作先取得确认

稳定线写入（开 / 合 PR）、打 tag、发布、改远端配置一律先向用户确认（AGENTS.md 红线）；发版动作不可逆。**已授权豁免**：`feat/**`、`fix/**`、`chore/**`、`hotfix/**`、`release/**` 主题分支的 push 与历史改写 agent 可直接执行、不需逐次确认（AGENTS.md「对外可见的动作」豁免条款）；`--force-with-lease` 只落自己的主题分支，永不触碰 `master` 与 `archive/dev-d542Bb`（archive 只读，任何写入都要单独确认）。**逐次确认不可让的三件**：临时改规则集绕过必需检查（紧急合入的唯一通道，见下）、tag 推送、稳定线历史改写。**紧急合入流程**：检查未绿而确需合入时，`--admin` 在当前规则集配置下会被拒绝（bypass 名单为空）；唯一通道是向用户逐次确认后**临时修改规则集**（网页或 API 暂停必需检查 / 临时加 bypass）、合入后**立即恢复原配置**并复核生效。不设常设 bypass 名单。

***

## 使用流程

当用户调用此 skill 时，先向用户确认意图（可多选）：

| 操作                   | 说明                                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| ① 清理临时文件             | 删除 `__pycache__`、`*.pyc`、`*.egg-info`、`build/`、`dist/`、`.pytest_cache/` 等（**不含** `debug/` 调试截图） |
| ② 确定版本号              | 按「当前 dev 系列顺延 / minor 升级开新系列」规则计算，**用户指定优先**                                                    |
| ③ 更新文档               | 更新 `docs/update_log.md` 添加新版本条目（README 如需一并更新）                                                  |
| ④ 提交改动               | 从 `origin/master` 切 `type/slug` 主题分支提交；稳定线只经 PR 收入，见「分支模型」                      |
| ⑤ 更新版本标记 + 打 Tag 并推送 | 更新 `docs/latest_release.json`，创建 `vX.Y.Z[-dev.N]` tag 并 push，触发 CI/CD Release                   |
| ⑥ 本地打包验证（可选）         | 跑 `scripts/release/assemble.ps1` 生成 zip+sha256 本地校验                                             |
| ⑦ 全部执行               | 按 ①→②→③→④→⑤ 顺序（⑥ 可选插入 ⑤ 前）                                                                      |

**顺序依赖：** ③ 需先定 ②（日志条目含版本号）；④ 应包含 ②③ 的改动；⑤ 必须晚于 ④
（tag 打在含全部改动的 master 上）。

***

## 操作详解

### ① 清理临时文件

1. 删除所有 `__pycache__` 目录（递归）
2. 删除所有 `*.pyc` 文件
3. 删除 `*.egg-info/`、`build/`、`dist/`、`.pytest_cache/`（若存在）

**注意：** 不删除 `logs/`、`dataset/`、`assets/`、`config/`、`.venv/`、`debug/`。
`debug/` 下的调试截图（ROI 框选定位用）有保留价值，一律不清理。

### ② 确定版本号

**当前实际模式（2026-11 复核，git tag 历史）：**

- 每次 minor 升级后开新 dev 系列：`v0.X.0-dev.1`、`-dev.2` …（如 `v0.15.0-dev.1` → `v0.15.0-dev.8`）

- dev 系列成熟后（用户拍板）才发正式版 tag（如 `v0.13.0`）

- 因此：**dev 序号在当前 minor 系列内检测顺延**，不是「基于最近正式版 tag 算」

| 场景                 | 版本格式                    | 示例                                      |
| ------------------ | ----------------------- | --------------------------------------- |
| 新功能（minor 升级，开新系列） | `v0.(x+1).0-dev.1`      | `v0.15.0-dev.8` → 新功能 → `v0.16.0-dev.1` |
| 同系列 Bug 修复         | `v0.x.y-dev.(N+1)`      | `v0.15.0-dev.8` → 修复 → `v0.15.0-dev.9`  |
| 系列成熟发正式版（用户拍板）     | `v0.x.y`                | `v0.15.0`（SemVer 正式版）                   |
| 正式版后 minor 升级      | 开新系列 `v0.(x+1).0-dev.1` | `v0.13.0` → `v0.14.0-dev.1`             |

**规则：**

1. `git tag --sort=-v:refname` 列出全部 tag（不只看第一条）
2. 判断本次变更类型（minor / patch / 用户指定），按上表计算候选版本号
3. **用户指定版本号优先**：如用户已明确（如 `v0.16.0-dev1`），以用户为准
4. 格式建议 `v0.x.y-dev.N`（pre-release 段用点，与现有 `v0.15.0-dev.8` 及 CI SemVer 校验一致）
5. 用正则校验 SemVer：`^v\d+\.\d+\.\d+(-[\w.]+)?$`
6. 输出版本号让用户确认后再继续

### ③ 更新文档

在 `docs/update_log.md` 顶部（最新 `## YYYY-MM-DD` 之后）插入新版本小节。**这是面向玩家的更新说明，不是开发日志**——收录判据与禁止项见 [`docs/README.md`](../../docs/README.md)「update_log 收录判据」。

```markdown
## YYYY-MM-DD

### vX.Y.Z[-dev.N] 一句话主题 🏷️

- **新增：** …
- **改进：** …
- **修复：** …
- **移除：** …
```

- 每条都过一遍判据：**玩家能看到什么不同？** 答不出的删掉；没有内容的分类不写
- 变更来源是 `git log <上个 tag>..HEAD`——过程细节已在 commit message，不重复搬进本文
- 让用户确认或编辑日志内容再写入

- 若功能列表/项目结构等涉及 README.md，询问用户是否一并更新

- 若本次是重写 skill/规范变更，同步更新 `AGENTS.md`「版本交付与协作」小节

### ④ 提交改动

**当前实际模式（2026-11 起）：从 `origin/master` 切 `type/slug` 主题分支，一个主题一条，合入即删**（命名见「分支模型」）。

1. 建分支：`git switch -c feat/<slug> origin/master`（并行工作时改用独立工作区，见「独立工作区与共享检出」）
2. `git status --short` 查看未提交改动；提交按 pathspec 直提：`git commit -m "标题" -m "正文" -- <具体文件>`（PowerShell 不支持 heredoc，用 `-m`）
3. push：`git push -u origin <主题分支>`——只同步/备份，不触发测试（触发面只有 PR、`master` push 与 tag）

**开 PR（主题收口时，用户认定可合）：**

1. **先跑全仓门禁再开 PR**：`ruff check .` → `pyright maaracing_master` → 全量 `pytest -q`（用共享 `.venv` 显式路径）。CONTRIBUTING 把这三项留在 CI 侧（提交路径只跑触碰面），但 PR 的红挡在稳定线门口，而它们是**串行露出**的：ruff 不绿看不到 pyright，pyright 不绿看不到测试。
2. `gh pr create --base master --head <主题分支> --title "<type>(<scope>): <主题>" --body-file <文件>`（长正文先写临时文件）。
3. **冲突**：单主干下没有假冲突——分支从 master 切出、只含自己的提交，报冲突就是真冲突（期间 master 前进）。先 `git rebase origin/master` 并在本地重跑门禁；仍有内容分叉交用户裁决，agent 不自动解决语义冲突。冲突态的 PR 建不出 merge ref，`pull_request` 流水线**整个跳过**——「Test 一条都没跑」先查 PR 是否 CONFLICTING，再怀疑 workflow 配置。
4. 合并走 `gh pr merge <N> --squash --subject "<type>(<scope>): <主题> (#<N>)" --body "<正文>"`——**显式给标题与正文**，不要默认值（头分支的提交数不可预期，默认信息取不到可用的摘要）。规则集只允许压扁合并（`--merge` / `--rebase` 会被远端拒绝），master 上一个主题恰好一个 commit；检查未绿时先报告用户，确需绕过时走「对外动作先取得确认」的临时改规则集流程（`--admin` 在当前规则集下不可用），合完恢复配置。
5. **PR 是稳定线写入的唯一通道**：不在共享检出里 checkout `master` 本地操作（见「分支模型」）。

**合入之后**：远端头分支由 `delete_branch_on_merge` 自动删除；本地 `git branch -d <主题分支>`，用了独立工作区的把它移除。下一条主题从最新的 `origin/master` 切。

**旧的「集成提交」已退役**：它存在的原因是压扁草稿线与稳定线之间没有合并基线，需要把「日常线的整棵树」挂成一个父为 master 的提交来制造可审对照物。单主干下 PR 的净增量天然等于主题增量，不再需要构造对照树；`release/**` 只保留为「打 tag 前的最后一次收口」这一分支名用途，按常规主题分支操作。`release/**` 在 test.yml 的 push 分支表里，推上去自带一轮全量 CI——PR 流水线与分支流水线各跑一次，**同一提交两边结论可以不同**（计时类断言吃不住负载差），红的先本地复现再定性。

### ⑤ 更新版本标记 + 打 Tag 并推送（触发 CI/CD）

1. **更新** **`docs/latest_release.json`**（程序内「检查更新」的唯一信源）：`tag`、`version`、`published_at` 三项指向本次版本，`download_url` 保持不变。CI **不回写此文件**（master 受分支保护），漏写会造成「Release 页面已有新版，但玩家软件里永远收不到更新提示」的错位。该改动必须作为 commit 落在 **tag 之前**——tag 落点那份代码要同时含 changelog 小节与此标记文件（Release 正文由 `extract_changelog.py` 从 tag 那份 `docs/update_log.md` 抽取，改文案必须与 tag 落点一起前移）。
2. 确认工作区干净（`git status --porcelain` 无输出）；确认待发内容**已沉淀进稳定线**（PR 已合入 master、CI 绿）。
3. **取本次发版 PR 的 squash 提交并显式对它打 tag**：`gh pr view <N> --json mergeCommit --jq .mergeCommit.oid` 拿到 SHA；**先 `git fetch origin master`**（刚合并时本地可能还没有该对象、或跟踪引用尚未更新，不 fetch 会让正确的提交也过不了校验）；再核 `git merge-base --is-ancestor <sha> origin/master` 成立、且 `git rev-parse <sha>^{tree}` 与验收过的主题 HEAD 树一致（不一致说明合并带了额外解决残留，先查清）。然后 `git tag <tag> <sha>`（0.x 阶段通常为 pre-release；本仓库自 v0.12 起统一用**轻量 tag**，`git cat-file -t refs/tags/<上一个>` 可验类型）。**不要省略目标提交**：`git tag <tag>` 默认打在本地 HEAD（可能停在主题分支）上，`git tag <tag> origin/master` 则会随其他 PR 合入而前进——两者都不绑定本次验收。
4. push tag：`git push origin <tag>`（tag 已绑定本次 squash 提交，不随 master 前进；不在共享检出里切分支）
5. **push 前必须向用户确认**（发版动作不可逆）
6. 告知用户：push tag 触发 GitHub Actions（`.github/workflows/release.yml`），流水线 = `test`（与 PR / master push 同一条 test.yml：pytest、ruff、pyright、真源与卫生机检、Shell 编译）→ `release`（创建 GitHub Release **草稿**，正文含下载指引；sdist/wheel 只构建验证、不再上传）→ `win-build`（assemble.ps1 组装解压即用 win-x64 zip + 本地 sha256，并生成可选模型包，一并补传到该草稿；两个资产上传完成后由 action 把草稿转为公开，随后校验「已公开且资产齐全」）→ `cnb-release`（同步 zip/模型包与正文到 CNB）。草稿不可见，因此构建失败只留下草稿，不会出现没有下载包的公开页面
7. **tag 触发的工作流与 master push 的工作流 checkout 语义不同**：前者完整历史（拿得到 tag），后者 `--no-tags` 浅克隆。因此「master 的 Test 红、Release gate 绿」可以同时成立，排查时先分清是哪条流水线

### ⑥ 本地打包验证（可选）

```powershell
powershell -File scripts\release\assemble.ps1 -Version <版本号> -RepoRoot <仓库根> -OutRoot scripts\release
# 或复用已验证 runtime 加速：-SourceRuntimeDir <先前构建的 runtime\python 目录>
```

- 输出 `MaaRacingMaster-<版本>-win-x64.zip` + `.sha256`

- 自检：embedded Python import `maa/onnxruntime/cv2/numpy/rapidocr/windows_capture` + `maaracing_master` 打印 `__version__`

- 注意 `-HostPython` 必须为 3.11（cp311）以匹配 embedded runtime；`-SkipPublish` 仅当 GUI 源指纹一致时复用缓存

- 默认包不含深度/线观测权重（约 82MB），由可选模型包分发；要出离线全量包加 `-WithModels`

- 本步需要 MSVC（编译 native Launcher）。没有本机 MSVC 时改用云端 `package-test.yml` 验证同一脚本（Windows runner + `mlocati/setup-msvc`），产物只上 artifact、不发布

***

### 不发版模式（不动程序代码 → 不 tag，方案 C）

**适用：** 本次改动只涉及文档 / CI / 元数据，**不触碰运行时程序代码**——此时不需要新的安装包，**不打 tag、不触发 release**；改动走常规 `chore/<slug>` 主题分支 PR 进稳定线（稳定线受规则集保护，不能直推）。

**判定"是否动程序代码"：** 用 `git diff --name-only` 检查本次即将提交的文件，凡落入下列**程序路径**即视为「需要出包」：

- `maaracing_master/**`（Python 运行时代码）

- `apps/**`（C# / XAML GUI）

- `maaracing_master/plugins/*/resources/models/**`（模型权重）、`maaracing_master/**/resources/**`（识别资源与模板）

- `config/**`、`requirements*.txt`、`scripts/release/requirements-runtime-lock.txt`

**反之（纯文档 / CI / 元数据，不发版）：** `*.md`、`docs/**`、`.github/**`、`pyproject.toml`（仅元数据 / extra 调整，不影响 win 包 runtime 的 lock 依赖）。README / update\_log / CONTRIBUTING / workflow 改动均属此类。

**不发版模式流程：**

1. 从 `origin/master` 切 `chore/<slug>` 主题分支提交（纯文档 / CI / 元数据改动统一用 `chore` 前缀）
2. **不写 update_log**——纯文档 / CI / 元数据改动玩家感知不到，按收录判据一律不进（过程已在 commit message）
3. `git push -u origin <主题分支>` 后开 PR 合入（push 主题分支不触发测试，PR 才跑）
4. **不打 tag、不触发 release**；改动随常规 PR 进稳定线

**技术背景：** GitHub Actions 的 `push` tag 事件**不支持** **`paths`** **/** **`paths-ignore`** **过滤**，无法在 trigger 声明层区分「纯文档 tag」与「程序 tag」，故本项目用\*\*流程约定（方案 C）\*\*而非 workflow 自动检测——纯文档改动一律不 tag，只有程序代码有实质改动、需要新产物时才走完整发版（① 清理 → ② 定版本 → ③ 日志 → ④ 提交 → ⑤ tag → push）。

***

## 版本管理规范（当前实际）

版本规则以本 skill 为准，与 [CONTRIBUTING.md](../../CONTRIBUTING.md)（仓库根，自本文件所在目录上溯两级）「版本与发布」小节同源于 git tag 事实：

| 变动                 | 版本格式                            | 示例                                |
| ------------------ | ------------------------------- | --------------------------------- |
| minor 升级（新功能）      | `v0.(x+1).0-dev.1`（开新系列，dev 顺延） | `v0.15.0-dev.8` → `v0.16.0-dev.1` |
| patch（同系列修复）       | `v0.x.y-dev.(N+1)`              | `v0.15.0-dev.8` → `v0.15.0-dev.9` |
| 正式版（dev 系列成熟，用户拍板） | `v0.x.y`                        | `v0.15.0`                         |

- **Git Tag 是唯一信源**：禁止手动改源码版本号，`setuptools-scm` 从 tag 自动推导（写入 `maaracing_master/_version.py`）

- **发布模式**：从 `origin/master` 切 `type/slug` 主题分支 → 主题收口开 PR 并 squash 合入稳定线 → 在稳定线上打 tag → push tag 触发 CI/CD（见「分支模型」）

- tag 触发 CI 时 `test` 会先跑 pytest，测试失败不发布

***

## 参考文件

- **版本号来源：** Git Tag（`git tag --sort=-v:refname`）

- **版本号自动生成：** `pyproject.toml` → `setuptools-scm` → `maaracing_master/_version.py`

- **更新说明：** `docs/update_log.md`（面向玩家，Release 正文信源）

- **发布 CI/CD：** `.github/workflows/release.yml`（push tag `v*` 触发：test → release → win-build → cnb-release）

- **版本标记：** `docs/latest_release.json`（程序「检测更新」信源，CNB raw 优先读取；由发版提交携带，CI 不回写）

- **CNB 镜像：** `.github/workflows/mirror-to-cnb.yml`（master push 自动 git 镜像同步到 cnb.cool）

- **本地打包脚本：** `scripts/release/assemble.ps1`

- **云端打包验证：** `.github/workflows/package-test.yml`（push `**/*-pkgtest` 或手动触发，只上传 artifact，不创建 Release；版本号读 `docs/latest_release.json`）。触发方式：在主题分支上开 `chore/<slug>-pkgtest`，推它即跑一轮 Windows 打包

- **项目规范：** `AGENTS.md`

- **README：** `README.md`

***

## PowerShell 兼容性注意事项

本项目的运行环境是 Windows PowerShell，与类 Unix shell 有显著差异，执行命令时特别注意：

| 场景                   | 不要用                   | 改用                                                      |
| -------------------- | --------------------- | ------------------------------------------------------- |
| git commit 多行消息      | `cat <<'EOF'` heredoc | `git commit -m "标题" -m "正文"`                            |
| gh pr create 多行 body | `cat <<'EOF'`         | 先写入临时文件，用 `--body-file` 传入                              |
| 管道取前 N 条             | `head -N`             | `Select-Object -First N`                                |
| 管道取后 N 条             | `tail -N`             | `Select-Object -Last N`                                 |
| 管道计数                 | `wc -l`               | `Measure-Object \| Select-Object -ExpandProperty Count` |
| 环境变量引用               | `$VAR` 或 `${VAR}`     | `$env:VAR`                                              |
| 布尔值                  | `true` / `false`      | `$true` / `$false`                                      |

**禁止项（安全）：**

- 不写死代理到 git config（`git config http.proxy ...`）——按需临时环境变量 `$env:HTTP_PROXY`/`$env:HTTPS_PROXY` 或提示用户自行配置

- 不用 `git reset --hard` / `git clean -f` 等破坏性命令；同步 master 用 `git pull --rebase origin master` 或仅在用户明确要求时操作

- 稳定线写入（push / 开合 PR）、打 tag、merge、发布前必须向用户确认；主题分支（`feat/**` `fix/**` `chore/**` `hotfix/**` `release/**`）push 与历史改写已授权，不需逐次确认；`archive/dev-d542Bb` 只读，任何写入单独确认

