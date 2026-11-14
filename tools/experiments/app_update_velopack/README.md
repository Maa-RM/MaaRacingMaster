# 应用更新器适配验证

status: active

## 问题

验证 Velopack 能否用于现有原生入口、WinUI 壳与 Python 侧车组成的统一应用更新。维持单产品发行，验证不改动正式工程依赖、不访问真实用户数据、不安装到系统应用目录。

## 信源与判据

- 官方 SDK 与 CLI：NuGet 的 `Velopack`、`vpk`，实验固定使用经官方包页核实的稳定版本；精确版本与来源记录在实验输出。
- 主入口须最早处理更新钩子并快速退出：[官方集成契约](https://docs.velopack.io/integrating/overview)。
- Windows 整个应用内容目录会替换，更新前必须释放占用：[官方 Windows 行为](https://docs.velopack.io/packaging/operating-systems/windows)。
- 独立判据为钩子退出状态、实际生成归档、更新前后磁盘内容与进程退出，不以自报成功代替。

## 验证范围

- 原生入口参数传递和壳启动钩子早退。
- 当前便携分发目录作为整体应用打包的适配。
- 隔离便携副本的完整更新与内容校验。
- 提权、子进程生命周期及首次迁移的剩余限制。

## 产物

`_out/` 存放隔离工具、下载包、构建输出和日志，不进入正式分发或生产测试依赖。生产代码与正式文档不得引用本实验目录；收敛后按实验区退役规范迁移结论与清理产物。

## 复算

从仓库根运行，Python 仍使用根虚拟环境。准备脚本从 NuGet 官方下载固定版本 SDK，验证固定包哈希；CLI 安装到实验输出目录。运行需要本机 .NET SDK 与可访问的官方包源。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py tool
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py hooks
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py update
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py nested-update
# 受管真实壳整链（标准布局内容树 + 双版本打包 + 提权链；UAC 与关窗需人工配合）
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py managed-shell-minimal
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py pack-managed
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py managed-update
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py managed-manifests
# P0 收尾用例（孤儿侧车升级 + 旧应用根毒化；feed 追加 0.3.0，三轮窗口）
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py pack-p0-tail
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py p0-tail
# P1 正式壳整链（直接发布正式工程，无任何注入；UAC 与关窗需人工配合）
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py formal-shell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py pack-formal
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py formal-update
# 壳合同探针（pytest 同级 .NET 侧合同锁；--python 供侧车关停用例指定解释器）
dotnet run --project apps/MaaRacingMaster.Shell/tools/ShellContractProbe -- approot
dotnet run --project apps/MaaRacingMaster.Shell/tools/ShellContractProbe -- hook
dotnet run --project apps/MaaRacingMaster.Shell/tools/ShellContractProbe -- sidecar --python .venv/Scripts/python.exe
# 白窗取证与拆分（历史判定轮，详见「白窗现象」）
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py real-shell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py publish-fd
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py managed-shell-nl
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py managed-shell-v1
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py managed-shell-v1fd
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py pure-sc-publish
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py targets-launch
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py fd-launch
```

所有实验程序为独立样本或正式壳的隔离源码副本，不向真实安装目录应用更新。便携升级使用本地目录源，未测试线上 CNB/GitHub 分发。受管整链需要提权上下文：探针经一次 UAC 自提权，子进程无控制台窗口、进度写 `_out/managed-result.launch.log`；**运行期间不要关闭任何弹出的终端窗口**（关闭控制台会连带杀掉整棵实验进程树）。运行时复用主线检出的 `build/runtime-cache`（可用 `MAARM_RUNTIME_SOURCE` 覆盖）；worktree 检出无 `.venv` 时，实验脚本用主线虚拟环境的 Python 驱动。

## 已验证结果

| 问题 | 结果与边界 |
|---|---|
| 未接 SDK 的真实壳能否直接打包 | 原有壳产物被 CLI 的启动契约检查拒绝；不能以跳过检查代替适配 |
| 钩子能否早退 | 缺少初始化的样本先失败；加入启动初始化后通过，正常启动仍可进入 |
| 全量升级能否在本地完成 | 根入口样本与嵌套 `app/` 样本均用真实 CLI/SDK 完成便携升级并自动重启；目标内容由磁盘文件和新进程报告确认 |
| 下载后是否会在普通启动偷偷应用 | 显式关闭默认启动自动应用后，已下载包在普通启动保持待应用，显式调用后才升级 |
| 外置数据是否被更新器触碰 | 包外样靶字节保持；不证明业务数据格式兼容或迁移可恢复 |
| 真实壳是否能加入 SDK | 隔离副本添加 SDK 引用与启动前置调用后编译、发布、打包成功；未修改正式工程 |
| 真实壳钩子验证了什么 | 通过 `dotnet` 宿主调用程序集，证明钩子早于单实例与 GUI；该宿主定位不是壳自身路径，不证明真实受管版本识别或 UI/侧车已运行 |
| 提权声明是否保留 | PE 资源读取确认壳与生成入口均请求管理员权限；不证明实际 UAC、取消提权和更新后提权链通过 |
| 受管标准布局整包升级（真实壳，最小注入 + 树外助手） | 提权状态下全链完成：生成入口两跳启动、内容根与版本识别正确、下载、普通启动不提交待更新包、提交后磁盘替换为 0.2.0、包外用户数据未动、更新器自动重启新版、**重启 GUI 的 WebView2 环境成功创建（EBWebView 完整生成）**、升级后助手在新装上下文读到 0.2.0、入口探针正常 | 壳的 WebView2 数据目录仍在 exe 旁（P1 待迁用户数据根）；重启窗口未被人工确认关闭（探针按上限自动收尾） |
| 中文与空格路径 | live 副本位于「中文 目录 空格」层级；入口启动、升级、重启全程无路径故障 | 无 |
| 白窗现象 | 带完整注入（版本报告 + 下载/提交探针）的发布产物，WebView2 环境创建必然挂起：UDF 目录被创建但 EBWebView 结构不生成、无异常、无 WER 事件、壳自身 stderr 无输出；最小注入（仅 VelopackApp.Run + 探针参数早退）与无注入产物必正常渲染（各 7:5 样本，含同二进制复跑） | 根因机制未明：挂起/正常样本的文件级差异只有壳自身 IL（执行层面无差别）；已排除 Release/Debug、自包含/依赖框架、发布/构建、深/短路径、中/英文路径、stub/直启、句柄形态、APPDATA 重定向、VelopackLocator 访问、提权、侧车有无。**已绕开**：壳保持最小注入，下载/提交改由受管树外的独立助手以显式定位器（TestVelopackLocator）驱动，整链实测通过 |
| 受管树内运行次要 exe | 独立更新助手（非注册主程序）在受管安装树内直启必挂（默认定位器解析），与路径无关；以主程序身份运行无此问题 | 机制未明（默认定位器对「运行 exe ≠ 注册主程序」的处理）；规避方式：助手住树外，经 TestVelopackLocator 显式指定受管根 |
| 崩溃遗留侧车（孤儿用例） | 强杀壳后侧车无遗留：侧车主循环读 stdin（sidecar.py:762），父死即 EOF 自退；其后下载与提交照常完成（0.1.0→0.3.0） | 无（孤儿风险对侧车不成立；有序关闭路径的退出确认仍由 P1 覆盖） |
| 旧应用根毒化 | 壳带 MaaRM_APP_ROOT 覆盖值启动：环境变量压过内容根——无侧车进程、无新会话日志（前端落旧根不可用） | 无（确认受管模式必须拒绝冲突覆盖值，归 P1） |

## 独立复核裁决

- 嵌套入口在固定工具版本下通过实测，SDK 源码也有对应定位分支；但该分支明确提示不推荐深层嵌套。保留为候选兼容路线，首版优先按更新器标准布局评估仅在打包时调整 GUI 文件位置，不改产品版本模型。
- 现有侧车关闭超时后发出终止请求，但没有等待最终退出。正式适配须补充实际退出与占用确认，不能把壳关闭回调结束当作全部释放。
- 正式资源定位优先采信继承的应用根覆盖值；受管入口应优先使用更新器确认的内容根，开发与旧 ZIP 模式再保留原有路径规则。
- 更新器的退出等待有上限；它只应在业务任务和模型安装收尾、必要数据快照完成后启动，不能用它等待游戏自然结束。
- 真实 UAC、WebView/侧车握手与退出、业务数据迁移、自定义插件保留、旧 ZIP 首次迁移、损坏包与取消等仍未验收。样本成功不能写成完整产品已经适配。
- 白窗矩阵推翻了句柄、路径、构建配置、发布方式、APPDATA、定位器访问等全部归因；「注入执行层无差但产物必挂」机制未明，属本机待解异常，不得以任何样本成功扩大为产品适配完成。已绕开（最小注入 + 树外助手显式定位器），正式接入须沿用此形态并在 CI/二机上复验。
- 受管树内运行非注册主程序必挂默认定位器解析；任何「在受管树内跑工具 exe」的想法都必须改走树外显式定位器模式。

## 后续最小实施边界

只在应用交付与宿主生命周期层接入固定版本 SDK 和工具生成的更新器。产品、内置插件和共享运行时仍一起发行。先完成便携版全量更新，暂不实现安装向导、独立插件发版或运行期热替换。
