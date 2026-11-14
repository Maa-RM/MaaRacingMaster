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
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py probe
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py hooks
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py update
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py nested-update
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py real-shell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py real-shell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/prepare.py pack-real-shell
.\.venv\Scripts\python.exe -B -X utf8 tools/experiments/app_update_velopack/probe.py manifests
```

所有实验程序为独立样本或正式壳的隔离源码副本，不向真实安装目录应用更新。便携升级使用本地目录源，未测试线上 CNB/GitHub 分发。

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

## 独立复核裁决

- 嵌套入口在固定工具版本下通过实测，SDK 源码也有对应定位分支；但该分支明确提示不推荐深层嵌套。保留为候选兼容路线，首版优先按更新器标准布局评估仅在打包时调整 GUI 文件位置，不改产品版本模型。
- 现有侧车关闭超时后发出终止请求，但没有等待最终退出。正式适配须补充实际退出与占用确认，不能把壳关闭回调结束当作全部释放。
- 正式资源定位优先采信继承的应用根覆盖值；受管入口应优先使用更新器确认的内容根，开发与旧 ZIP 模式再保留原有路径规则。
- 更新器的退出等待有上限；它只应在业务任务和模型安装收尾、必要数据快照完成后启动，不能用它等待游戏自然结束。
- 真实 UAC、WebView/侧车握手与退出、业务数据迁移、自定义插件保留、旧 ZIP 首次迁移、损坏包与取消等仍未验收。样本成功不能写成完整产品已经适配。

## 后续最小实施边界

只在应用交付与宿主生命周期层接入固定版本 SDK 和工具生成的更新器。产品、内置插件和共享运行时仍一起发行。先完成便携版全量更新，暂不实现安装向导、独立插件发版或运行期热替换。
