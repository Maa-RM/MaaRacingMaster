# launcher_silent_check — 启动器静默版本检查与搬运工更新

status: active

## 待闭环问题（C 类：外部程序行为，官方无文档）

1. **静默**：官方启动器（PySide6/Qt 应用，卸载表反查）能否在**不出现可见窗口**的前提下
   完成一次版本检查？候选手段：创建进程时 `SW_HIDE`、`launcher.ini [Settings]` 的
   `b_hide` / `b_background` 开关、窗口出现后移出屏幕（兜底）。
2. **搬运工信号**：启动器自写日志（`%LOCALAPPDATA%\RacingMaster\CN\Log.txt`）里，
   「需要更新 / 下载中 / 下载完成可进游戏」分别对应哪些行？有更新时是否需要人工点
   「更新游戏」按钮才开始下载？补丁装完后是否自动拉起游戏？

已知锚点（2026-11-22 基线快照，`_out/baseline_log_*.txt`）：

- 状态机行 `set launcher state: LaunchStateEnum.Init/EnvInstall/Ready/Update`；
- `Final download config:` 行后跟 JSON，`update_list` 空 = 无需更新（11-21 会话）；
- 有更新时状态停在 `Update`（11-22 19:15 会话，待补两个文件约 198MB）；
- Ready 后 2s 出现 `real_start_game` → `start game: <game_dir>\g112.exe --MuMuLauncher ...`
  ——但那次是否人工点击无法区分，本实验顺带裁定。

## 标尺（独立于待验证对象）

- 窗口事实：`EnumWindows` + `IsWindowVisible` + `GetWindowRect`（OS 口径，非启动器自述）。
- 检查事实：启动器自写 Log.txt 的状态机行（启动器自证，仅用于判定检查是否推进，
  不用于判定窗口是否可见）。

## 约束与回撤

- 探针只读启动器落盘产物；改写仅限 `launcher.ini` 两个布尔开关，测完按字节还原
  （快照存 `_out/baseline_launcher.ini`），每次还原后复核。
- 更新链路实验会触发官方补丁下载与写入游戏目录（`gamedir` 指向的安装目录）——
  这是用户知情且本就要做的常规更新，不额外改游戏文件。
- 探针落盘只进本区 `_out/`。

## 脚本

- `probe_silent.py`：逐用例验证静默手段，输出时间线与判定。
- `probe_update.py`：有更新场景下走完整链路，采集状态机与下载信号时间线。

## 结论

以下均为 2026-11-22 实测（证据：`_out/probe_silent_*.txt` 时间线、
`_out/update_session_20261122_1941_full.txt` 原始会话日志；进程时间戳见下）。

### 1. 静默检查——「抢窗关闭」成立，窗口隐藏手段全不成立

- 五种隐藏手段全部无效：`SW_HIDE` 与 `SW_SHOWMINNOACTIVE` 被启动器（Qt/PySide6）
  无视，`launcher.ini` 的 `b_hide` / `b_background` 不影响启动窗口可见性；
  窗口出现后移屏会命中最先出现的启动画面，而非主窗口。
- 但**检查先于窗口完成**（五次会话一致）：`Final download config` 与状态
  `Update/Ready` 在拉起后约 5–6.4s 落日志，主窗口 `window_shown` 要 8.7–9.4s。
- → 常规路径：拉起 → 盯日志等 `Final download config` → **在窗口出现前关闭启动器**
  （graceful close 实测五次全部成功，无退出确认框）→ 按结果直启或搬运。
  网络慢导致判定晚于 9s 时窗口会闪现——兜底：移屏藏窗，见 3。

### 2. 判据统一（生产实现直接用）

- `Final download config:` 的下一行 JSON：**`update_list` 空 = 可直启**
  （原本无需更新、或搬运已完成，两种情形同一信号）；
  非空 = 待更新（同时状态停在 `Update`）。
- 状态机：无需更新 `Init → EnvInstall → Ready`；有更新停在 `Update`，
  下载安装完成后 `download complete N → EnvInstall → Ready`。
- 日志按会话轮转（每场一个 `Log.txt`），只采本轮。

### 3. 搬运工更新——点击一次即触发，屏幕外可隔空点击

- 有更新时启动器**不会自动下载**（5 次静默运行无一自动开始；隔空点击后立即开始）。
- 移到屏幕外的窗口能收 `PostMessage` 点击：按钮中心按客户区比例 (0.811W, 0.899H)
  标定（1366×796 截图），一次命中触发 198MB 补丁下载。
- **移屏后的窗口不会自己回到屏幕**（实测 10 分钟保持 -25600,-25600 不动）。
- 下载+安装实测 57s（198MB，约 3.5MB/s）；完成后 `update_list` 变空、状态 Ready。
- **Ready 后不会自动拉起游戏**——由宿主直启，直启路径不受影响。
- 逐文件完成信号：`[Finish]` JSON（`finished: true`）+ `download file: <name>, 0`
  + `set key,value: <name> <md5>`（写回 config.ini 的 `[Local_MD5]`）。

### 4. 提权约束（生产集成必须处理）

- 启动器程序清单要求管理员：非提权进程裸 `Popen` 得 `WinError 740`。
- **顺带发现的存量缺陷**：生产「超时回退启动器」路径用的正是裸 `Popen`，
  在本机会同样失败（未在本机验证过该回退）。
- 生产集成选项：`ShellExecuteEx runas`（每次一次 UAC）／宿主提权运行／缓存检查
  结果以降低启动器运行频率。关闭提权中的启动器同样需要提权（`taskkill` 无 /F
  从提权侧投递 WM_CLOSE 实测可优雅退出）。

### 5. 时序（本场实测，供超时参数参考）

- 拉起 → 轮转出新日志 ~1s；远程版本比对 ~2s；状态 Init ~4s；
  `Final download config` ~5–6.4s；主窗口出现 ~8.7–9.4s。
- 本次更新链：点击 ~19:42 → 下载任务启动 19:43:17 → `download complete 1`
  19:44:14 → Ready 19:44:14 → 复核比对空列表 19:44:15。
  补丁文件落盘时刻与日志一致（`g112.exe` 19:43:18、主程序 19:44:16），
  `[Local_MD5]` 更新为远程清单目标值。

### 后续

- 生产化：core 启动链加「启动器检查+搬运」前置（抢窗关闭 / 屏幕外搬运 / 判据解析 /
  直启），失败兜底保留现状回退；一并修回退路径的 740 问题。
- 本目录在结论迁入 home（core CODE_WIKI / ADR-0018 相关章节，证据指 commit）后退役。
