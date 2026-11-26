// MaaRacingMaster native Launcher / 事务执行器
// ---------------------------------------------------------------
// 安装布局（更新收敛后的标准布局）：
//   <install>\MaaRacingMaster.exe        本程序（稳定入口，升级不替换）
//   <install>\program\                   可替换内容（app / runtime / maaracing_master / assets / ...）
//   <install>\.update\                   事务记录、暂存树、上一版备份、健康回执
//
// 职责：
//   1. 普通启动：读取并处理未完成事务（恢复或回滚），再启动 program\app\MaaRacingMaster.Shell.exe；
//   2. 事务执行（--apply <事务文件>）：等旧壳与旧启动器退出 → 备份旧 program → 移入新 program →
//      试启动新版本 → 收到健康回执才算成功，否则回滚到旧版本；
//   3. 兼容旧解压布局：没有 program\ 时按旧结构直接启动（不参与自动升级）。
//
// 不做：下载、版本检查、业务逻辑（都在壳与发布链）。
// 编译（MSVC，x64）：
//   cl /nologo /utf-8 /O2 /MT /Fe:MaaRacingMaster.exe launcher.c /link /SUBSYSTEM:WINDOWS user32.lib
// ---------------------------------------------------------------

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define PATH_CAP 32768
#define TX_PATH_CAP 4096
#define DIR_CAP 4096
#define VERSION_CAP 128
#define APPLY_HEALTH_TIMEOUT_MS 120000
#define APPLY_WAIT_SHELL_MS 60000
#define APPLY_WAIT_LAUNCHER_MS 15000
#define APPLY_RECOVER_WAIT_MS 30000
#define APPLY_NORMAL_WAIT_MS 5000

static WCHAR g_installRoot[PATH_CAP];
static WCHAR g_programDir[PATH_CAP];
static WCHAR g_updateDir[PATH_CAP];
static WCHAR g_txPath[PATH_CAP];
static WCHAR g_healthPath[PATH_CAP];
static WCHAR g_logPath[PATH_CAP];
// 安装实例锁：名字按安装路径哈希生成，两个独立解压目录互不阻塞。
static WCHAR g_lockName[128];
// 更新器为「试启动」拉起的根入口：它不能去抢父进程正持有的安装锁。
static BOOL g_isUpdateChild;

// 事务字段（由 shell 写入，本程序只读）
typedef struct
{
    WCHAR state[64];
    WCHAR install[TX_PATH_CAP];
    WCHAR staging[TX_PATH_CAP];
    WCHAR backup[TX_PATH_CAP];
    WCHAR target[VERSION_CAP];
    DWORD shellPid;
    DWORD launcherPid;
    DWORD watcherPid;
} TxRecord;

static void FatalBox(const wchar_t *msg)
{
    MessageBoxW(NULL, msg, L"MaaRacingMaster — 启动失败", MB_OK | MB_ICONERROR);
}

static BOOL PathExists(const wchar_t *path)
{
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static BOOL DirExists(const wchar_t *path)
{
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

// 规范化绝对路径：比较前归一（".."、短名、大小写），避免边界判断被字面差异绕过。
static BOOL CanonicalizePath(const wchar_t *path, wchar_t *out, size_t cap)
{
    if (path == NULL || path[0] == 0)
        return FALSE;
    DWORD length = GetFullPathNameW(path, (DWORD)cap, out, NULL);
    return length > 0 && length < cap;
}

static BOOL SamePath(const wchar_t *left, const wchar_t *right)
{
    wchar_t a[PATH_CAP], b[PATH_CAP];
    if (!CanonicalizePath(left, a, PATH_CAP) || !CanonicalizePath(right, b, PATH_CAP))
        return FALSE;
    return _wcsicmp(a, b) == 0;
}

// path 是否等于 dir 或位于 dir 之下（事务里的暂存/备份只允许在 .update 内）
static BOOL IsUnder(const wchar_t *path, const wchar_t *dir)
{
    wchar_t canonicalPath[PATH_CAP], canonicalDir[PATH_CAP];
    if (!CanonicalizePath(path, canonicalPath, PATH_CAP) ||
        !CanonicalizePath(dir, canonicalDir, PATH_CAP))
        return FALSE;
    size_t length = wcslen(canonicalDir);
    if (length == 0 || _wcsnicmp(canonicalPath, canonicalDir, length) != 0)
        return FALSE;
    return canonicalPath[length] == L'\\' || canonicalPath[length] == 0;
}

// .update 自身若是重解析点（玩家/低权限身份可做的目录联接），整条事务链都不可信。
static BOOL UpdateDirTrusted(void)
{
    DWORD attr = GetFileAttributesW(g_updateDir);
    if (attr == INVALID_FILE_ATTRIBUTES)
        return TRUE; // 目录尚未创建：没有可被劫持的对象
    return (attr & FILE_ATTRIBUTE_REPARSE_POINT) == 0;
}

// 事务实际操作的子目录（staging/backup）同样不能是重解析点。
static BOOL PathTrusted(const wchar_t *path)
{
    DWORD attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES)
        return TRUE;
    return (attr & FILE_ATTRIBUTE_REPARSE_POINT) == 0;
}

// 每套安装一把锁：锁名由规范化安装路径哈希得到。全局固定的名字会让两个独立
// 解压目录互相阻塞，也会把一个目录的恢复挡在另一个目录的事务后面。
static void BuildLockName(void)
{
    wchar_t canonical[PATH_CAP];
    const wchar_t *source = CanonicalizePath(g_installRoot, canonical, PATH_CAP)
        ? canonical
        : g_installRoot;
    unsigned int hash = 2166136261u;
    for (const wchar_t *p = source; *p != 0; ++p)
    {
        wchar_t c = *p;
        if (c >= L'A' && c <= L'Z')
            c = (wchar_t)(c + 32);
        hash ^= (unsigned int)(c & 0xFF);
        hash *= 16777619u;
    }
    swprintf_s(g_lockName, 128, L"Global\\MaaRM_UpdateApply_%08x", hash);
}

// 取得安装实例锁的所有权。CreateMutex 只创建对象、不取得所有权，必须 Wait 才算持有；
// 返回 NULL 表示别的实例正持有或创建失败。
static HANDLE AcquireApplyLock(DWORD timeoutMs)
{
    HANDLE mutex = CreateMutexW(NULL, FALSE, g_lockName);
    if (mutex == NULL)
        return NULL;
    DWORD wait = WaitForSingleObject(mutex, timeoutMs);
    if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED)
        return mutex;
    CloseHandle(mutex);
    return NULL;
}

static void ReleaseApplyLock(HANDLE mutex)
{
    if (mutex == NULL)
        return;
    ReleaseMutex(mutex);
    CloseHandle(mutex);
}

static BOOL JoinPath(wchar_t *out, size_t cap, const wchar_t *base, const wchar_t *child)
{
    return swprintf_s(out, cap, L"%s\\%s", base, child) > 0;
}

static void LogLine(const wchar_t *fmt, ...)
{
    if (g_logPath[0] == 0)
        return;
    FILE *fp = NULL;
    if (_wfopen_s(&fp, g_logPath, L"a, ccs=UTF-8") != 0 || fp == NULL)
        return;
    va_list args;
    va_start(args, fmt);
    vfwprintf(fp, fmt, args);
    va_end(args);
    fwprintf(fp, L"\n");
    fclose(fp);
}

// 递归删除目录；失败只记录，不中断事务（备份清理失败不致命）。
static BOOL DeleteTree(const wchar_t *path)
{
    DWORD attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES)
        return TRUE;
    if (!(attr & FILE_ATTRIBUTE_DIRECTORY))
    {
        SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL);
        return DeleteFileW(path);
    }

    wchar_t pattern[DIR_CAP];
    if (!JoinPath(pattern, DIR_CAP, path, L"*"))
        return FALSE;
    WIN32_FIND_DATAW fd;
    HANDLE find = FindFirstFileW(pattern, &fd);
    BOOL ok = TRUE;
    if (find != INVALID_HANDLE_VALUE)
    {
        do
        {
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0)
                continue;
            wchar_t child[DIR_CAP];
            if (!JoinPath(child, DIR_CAP, path, fd.cFileName))
            {
                ok = FALSE;
                break;
            }
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) &&
                !(fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT))
            {
                if (!DeleteTree(child))
                    ok = FALSE;
            }
            else
            {
                SetFileAttributesW(child, FILE_ATTRIBUTE_NORMAL);
                if (!DeleteFileW(child))
                    ok = FALSE;
            }
        } while (FindNextFileW(find, &fd));
        FindClose(find);
    }
    if (ok)
        ok = RemoveDirectoryW(path) != FALSE;
    return ok;
}

static BOOL WriteAllUtf8(const wchar_t *path, const char *text)
{
    HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return FALSE;
    DWORD written = 0;
    BOOL ok = WriteFile(file, text, (DWORD)strlen(text), &written, NULL);
    CloseHandle(file);
    return ok;
}

static void RemoveFileQuiet(const wchar_t *path)
{
    SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL);
    DeleteFileW(path);
}

static void EnsureUpdateDir(void)
{
    CreateDirectoryW(g_updateDir, NULL);
}

// 读事务文件（UTF-8，key=value 每行一项；值为路径时保留原样）
static BOOL LoadTx(TxRecord *tx)
{
    ZeroMemory(tx, sizeof(*tx));
    HANDLE file = CreateFileW(g_txPath, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return FALSE;
    char buf[65536];
    DWORD read = 0;
    BOOL ok = ReadFile(file, buf, sizeof(buf) - 1, &read, NULL);
    CloseHandle(file);
    if (!ok)
        return FALSE;
    buf[read] = 0;

    wchar_t wbuf[65536];
    int wlen = MultiByteToWideChar(CP_UTF8, 0, buf, (int)read, wbuf,
                                   (int)(sizeof(wbuf) / sizeof(wbuf[0])) - 1);
    if (wlen <= 0)
        return FALSE;
    wbuf[wlen] = 0;

    wchar_t *ctx = NULL;
    for (wchar_t *line = wcstok_s(wbuf, L"\r\n", &ctx); line != NULL;
         line = wcstok_s(NULL, L"\r\n", &ctx))
    {
        wchar_t *eq = wcschr(line, L'=');
        if (eq == NULL)
            continue;
        *eq = 0;
        wchar_t *value = eq + 1;
        if (_wcsicmp(line, L"state") == 0)
            wcsncpy_s(tx->state, 64, value, _TRUNCATE);
        else if (_wcsicmp(line, L"install") == 0)
            wcsncpy_s(tx->install, TX_PATH_CAP, value, _TRUNCATE);
        else if (_wcsicmp(line, L"staging") == 0)
            wcsncpy_s(tx->staging, TX_PATH_CAP, value, _TRUNCATE);
        else if (_wcsicmp(line, L"backup") == 0)
            wcsncpy_s(tx->backup, TX_PATH_CAP, value, _TRUNCATE);
        else if (_wcsicmp(line, L"target") == 0)
            wcsncpy_s(tx->target, VERSION_CAP, value, _TRUNCATE);
        else if (_wcsicmp(line, L"shell_pid") == 0)
            tx->shellPid = (DWORD)_wtoi(value);
        else if (_wcsicmp(line, L"launcher_pid") == 0)
            tx->launcherPid = (DWORD)_wtoi(value);
        else if (_wcsicmp(line, L"watcher_pid") == 0)
            tx->watcherPid = (DWORD)_wtoi(value);
    }
    return tx->state[0] != 0;
}

// 只改 state 行，保留其余字段（重写整个文件）。
static BOOL WriteTxState(const TxRecord *tx, const wchar_t *state)
{
    char utf8[65536];
    // 路径按 UTF-8 写出：先转成窄串再拼装
    char install[TX_PATH_CAP], staging[TX_PATH_CAP], backup[TX_PATH_CAP], target[VERSION_CAP];
    WideCharToMultiByte(CP_UTF8, 0, tx->install, -1, install, TX_PATH_CAP, NULL, NULL);
    WideCharToMultiByte(CP_UTF8, 0, tx->staging, -1, staging, TX_PATH_CAP, NULL, NULL);
    WideCharToMultiByte(CP_UTF8, 0, tx->backup, -1, backup, TX_PATH_CAP, NULL, NULL);
    WideCharToMultiByte(CP_UTF8, 0, tx->target, -1, target, VERSION_CAP, NULL, NULL);
    char stateUtf8[64];
    WideCharToMultiByte(CP_UTF8, 0, state, -1, stateUtf8, 64, NULL, NULL);
    sprintf_s(utf8, sizeof(utf8),
              "state=%s\ninstall=%s\nstaging=%s\nbackup=%s\ntarget=%s\n"
              "shell_pid=%lu\nlauncher_pid=%lu\nwatcher_pid=%lu\n",
              stateUtf8, install, staging, backup, target,
              (unsigned long)tx->shellPid, (unsigned long)tx->launcherPid,
              (unsigned long)tx->watcherPid);
    // 原子替换：先写临时文件再改名，断电时要么是旧记录、要么是新记录，
    // 不会留下半截文件让下次启动失去备份线索。
    wchar_t temp[PATH_CAP];
    if (!JoinPath(temp, PATH_CAP, g_updateDir, L"transaction.tmp"))
        return FALSE;
    if (!WriteAllUtf8(temp, utf8))
    {
        RemoveFileQuiet(temp);
        return FALSE;
    }
    if (!MoveFileExW(temp, g_txPath, MOVEFILE_REPLACE_EXISTING))
    {
        RemoveFileQuiet(temp);
        return FALSE;
    }
    return TRUE;
}

static BOOL IsProcessAlive(DWORD pid)
{
    if (pid == 0)
        return FALSE;
    HANDLE handle = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (handle == NULL)
        return FALSE;
    DWORD wait = WaitForSingleObject(handle, 0);
    CloseHandle(handle);
    return wait == WAIT_TIMEOUT;
}

static BOOL WaitForPid(DWORD pid, DWORD timeoutMs)
{
    if (pid == 0)
        return TRUE;
    HANDLE handle = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (handle == NULL)
        return TRUE; // 进程已不存在
    DWORD wait = WaitForSingleObject(handle, timeoutMs);
    CloseHandle(handle);
    return wait == WAIT_OBJECT_0;
}

static void KillProcessTree(DWORD pid)
{
    if (pid == 0)
        return;
    // 必须用系统目录里的绝对路径，并且命令行要带程序名：CreateProcess 的
    // lpApplicationName 非空时不搜 PATH，而 taskkill 仍按 argv[0] 解析参数——
    // 只给 "/PID ..." 会被它当成程序名，返回 1 且不杀进程。
    wchar_t systemDir[PATH_CAP];
    UINT length = GetSystemDirectoryW(systemDir, PATH_CAP);
    if (length == 0 || length >= PATH_CAP)
        return;
    wchar_t tool[PATH_CAP];
    if (swprintf_s(tool, PATH_CAP, L"%s\\taskkill.exe", systemDir) <= 0)
        return;
    wchar_t cmd[PATH_CAP];
    swprintf_s(cmd, PATH_CAP, L"\"%s\" /PID %lu /T /F", tool, (unsigned long)pid);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    SecureZeroMemory(&si, sizeof(si));
    SecureZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);
    if (CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                       NULL, NULL, &si, &pi))
    {
        WaitForSingleObject(pi.hProcess, 5000);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
}

// 启动普通实例（新版本或回滚后的旧版本）：返回进程句柄（NULL = 失败），PID 写入 pidOut。
// 句柄留给调用方等待/终止，不能创建完就丢——否则回滚前无法确认试启动进程已退出。
static HANDLE LaunchNormal(DWORD *pidOut)
{
    *pidOut = 0;
    wchar_t exe[PATH_CAP];
    if (!JoinPath(exe, PATH_CAP, g_installRoot, L"MaaRacingMaster.exe"))
        return NULL;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    SecureZeroMemory(&si, sizeof(si));
    SecureZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);
    // 试启动的根入口不能去抢本进程正持有的安装锁，也不能重复恢复事务：
    // 用环境变量标注它是「更新器拉起的子入口」，它据此跳过这两步。
    SetEnvironmentVariableW(L"MAARM_UPDATE_CHILD", L"1");
    BOOL started = CreateProcessW(exe, NULL, NULL, NULL, FALSE, 0, NULL, g_installRoot,
                                  &si, &pi);
    SetEnvironmentVariableW(L"MAARM_UPDATE_CHILD", NULL);
    if (!started)
        return NULL;
    CloseHandle(pi.hThread);
    *pidOut = pi.dwProcessId;
    return pi.hProcess;
}

// 健康回执：由新壳在启动完成后写入 <install>\.update\health.txt（version=<目标版本>）
static BOOL HealthMatches(const wchar_t *target)
{
    HANDLE file = CreateFileW(g_healthPath, GENERIC_READ, FILE_SHARE_READ, NULL,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return FALSE;
    char buf[4096];
    DWORD read = 0;
    BOOL ok = ReadFile(file, buf, sizeof(buf) - 1, &read, NULL);
    CloseHandle(file);
    if (!ok)
        return FALSE;
    buf[read] = 0;
    if (target[0] == 0)
        return FALSE;
    char targetUtf8[VERSION_CAP];
    WideCharToMultiByte(CP_UTF8, 0, target, -1, targetUtf8, VERSION_CAP, NULL, NULL);
    char needle[VERSION_CAP + 16];
    sprintf_s(needle, sizeof(needle), "version=%s", targetUtf8);
    return strstr(buf, needle) != NULL;
}

static BOOL WaitForHealth(const wchar_t *target, DWORD timeoutMs, DWORD *watchedPid)
{
    DWORD start = GetTickCount();
    while (GetTickCount() - start < timeoutMs)
    {
        if (HealthMatches(target))
            return TRUE;
        if (watchedPid != NULL && *watchedPid != 0 && !IsProcessAlive(*watchedPid))
            return FALSE; // 新实例已退出（启动失败）
        Sleep(500);
    }
    return FALSE;
}

static BOOL MoveDir(const wchar_t *from, const wchar_t *to)
{
    if (!PathExists(from))
        return FALSE;
    return MoveFileExW(from, to, MOVEFILE_COPY_ALLOWED) != FALSE;
}

// 回滚：把当前 program 移开（尽量删除），把备份恢复为 program。
// 返回 FALSE 表示备份没能回到原位——调用方必须保留事务记录等下次重试，
// 不能把它当成「已回滚」而删掉唯一的线索。
static BOOL RollbackProgram(TxRecord *tx)
{
    if (!DirExists(tx->backup))
    {
        LogLine(L"[recover] 备份目录不存在，无法回滚");
        return FALSE;
    }
    if (DirExists(g_programDir))
    {
        wchar_t failed[PATH_CAP];
        if (!JoinPath(failed, PATH_CAP, g_updateDir, L"failed"))
            return FALSE;
        DeleteTree(failed);
        if (!MoveFileExW(g_programDir, failed, MOVEFILE_COPY_ALLOWED))
        {
            LogLine(L"[recover] 移开当前程序目录失败，无法回滚");
            return FALSE;
        }
        DeleteTree(failed);
    }
    if (!MoveDir(tx->backup, g_programDir))
    {
        LogLine(L"[recover] 备份恢复到 program 失败");
        return FALSE;
    }
    LogLine(L"[recover] 已回滚到上一版程序目录");
    return TRUE;
}

// 普通启动前的恢复处理：绝不让半完成的替换留在盘上。
static void RecoverBeforeLaunch(void)
{
    // 任何清理或替换之前先确认 .update 不是重解析点：本函数可能以管理员身份运行，
    // 被联接出去的目录不能拿来做删除目标。
    if (!UpdateDirTrusted())
    {
        LogLine(L"[recover] .update 是重解析点，跳过恢复");
        return;
    }
    if (!DirExists(g_programDir) && !PathExists(g_txPath))
    {
        if (DirExists(g_updateDir))
        {
            wchar_t staging[PATH_CAP];
            if (JoinPath(staging, PATH_CAP, g_updateDir, L"staging") && PathTrusted(staging))
                DeleteTree(staging);
        }
        return;
    }
    TxRecord tx;
    if (!LoadTx(&tx))
        return;

    // 事务文件可能被低权限身份改写：目录必须与本次安装一致、暂存与备份必须在
    // .update 之下，且 .update 自身不能是重解析点，否则只清掉这条不可信记录，
    // 绝不按它删改任何目录。
    if (!SamePath(tx.install, g_installRoot) ||
        !IsUnder(tx.staging, g_updateDir) ||
        !IsUnder(tx.backup, g_updateDir) ||
        !UpdateDirTrusted() ||
        !PathTrusted(tx.staging) ||
        !PathTrusted(tx.backup))
    {
        LogLine(L"[recover] 事务记录的目录不可信，已忽略并清除");
        RemoveFileQuiet(g_txPath);
        return;
    }

    LogLine(L"[recover] 发现未完成事务 state=%s", tx.state);

    if (IsProcessAlive(tx.watcherPid))
    {
        // 正在执行的更新事务由 watcher 负责，本次启动不擅自回滚
        LogLine(L"[recover] watcher 仍在运行，跳过本地恢复");
        return;
    }

    if (_wcsicmp(tx.state, L"prepared") == 0)
    {
        DeleteTree(tx.staging);
        RemoveFileQuiet(g_txPath);
    }
    else if (_wcsicmp(tx.state, L"swapping") == 0)
    {
        if (!DirExists(g_programDir) && DirExists(tx.backup))
        {
            if (MoveDir(tx.backup, g_programDir))
            {
                LogLine(L"[recover] 替换中断，已从备份恢复旧版");
                RemoveFileQuiet(g_txPath);
            }
            else
            {
                LogLine(L"[recover] 从备份恢复失败，保留事务记录");
            }
        }
        else if (DirExists(g_programDir) && DirExists(tx.backup))
        {
            if (RollbackProgram(&tx))
                RemoveFileQuiet(g_txPath);
        }
        else
        {
            RemoveFileQuiet(g_txPath);
        }
    }
    else if (_wcsicmp(tx.state, L"swapped") == 0)
    {
        if (HealthMatches(tx.target))
        {
            DeleteTree(tx.backup);
            RemoveFileQuiet(g_healthPath);
            RemoveFileQuiet(g_txPath);
            LogLine(L"[recover] 新版本健康回执有效，清理备份");
        }
        else
        {
            RemoveFileQuiet(g_healthPath);
            if (RollbackProgram(&tx))
                RemoveFileQuiet(g_txPath);
            else
                LogLine(L"[recover] 回滚失败，保留事务记录等下次重试");
        }
    }
    else
    {
        RemoveFileQuiet(g_txPath);
    }
}

// 事务执行：等旧进程退出 → 备份旧 program → 移入暂存 → 试启动 → 健康回执决定成功或回滚
static int RunApply(const wchar_t *txPath)
{
    wcsncpy_s(g_txPath, PATH_CAP, txPath, _TRUNCATE);
    EnsureUpdateDir();

    HANDLE mutex = AcquireApplyLock(0);
    if (mutex == NULL)
    {
        LogLine(L"[apply] 安装锁被占用（更新或恢复进行中），退出");
        return 1;
    }

    TxRecord tx;
    if (!LoadTx(&tx))
    {
        LogLine(L"[apply] 事务文件不可读: %s", txPath);
        ReleaseApplyLock(mutex);
        return 1;
    }
    if (!SamePath(tx.install, g_installRoot))
    {
        LogLine(L"[apply] 事务安装目录与自身不一致，拒绝执行");
        ReleaseApplyLock(mutex);
        return 1;
    }
    if (!UpdateDirTrusted() || !PathTrusted(tx.staging) || !PathTrusted(tx.backup))
    {
        LogLine(L"[apply] .update 或暂存/备份是重解析点，拒绝执行");
        ReleaseApplyLock(mutex);
        return 1;
    }
    if (!IsUnder(tx.staging, g_updateDir) || !IsUnder(tx.backup, g_updateDir))
    {
        LogLine(L"[apply] 暂存或备份路径越出 .update，拒绝执行");
        ReleaseApplyLock(mutex);
        return 1;
    }
    if (!DirExists(tx.staging))
    {
        LogLine(L"[apply] 暂存目录不存在: %s", tx.staging);
        ReleaseApplyLock(mutex);
        return 1;
    }

    LogLine(L"[apply] 开始：目标 %s", tx.target);
    WaitForPid(tx.shellPid, APPLY_WAIT_SHELL_MS);
    WaitForPid(tx.launcherPid, APPLY_WAIT_LAUNCHER_MS);

    // 旧壳退出后再确认一次：仍在运行说明关窗未完成，放弃替换并保留旧版
    if (IsProcessAlive(tx.shellPid))
    {
        LogLine(L"[apply] 旧壳未退出，放弃本次替换");
        WriteTxState(&tx, L"aborted");
        ReleaseApplyLock(mutex);
        return 1;
    }

    DeleteTree(tx.backup);
    if (!WriteTxState(&tx, L"swapping"))
    {
        LogLine(L"[apply] 事务状态写入失败，放弃");
        ReleaseApplyLock(mutex);
        return 1;
    }

    if (!MoveDir(g_programDir, tx.backup))
    {
        LogLine(L"[apply] 备份旧程序目录失败，放弃（旧版未动）");
        WriteTxState(&tx, L"aborted");
        ReleaseApplyLock(mutex);
        return 1;
    }
    if (!MoveDir(tx.staging, g_programDir))
    {
        LogLine(L"[apply] 移入新程序目录失败，回滚");
        if (MoveDir(tx.backup, g_programDir))
            WriteTxState(&tx, L"aborted");
        else
            LogLine(L"[apply] 旧程序目录未能恢复，保留事务记录");
        ReleaseApplyLock(mutex);
        return 1;
    }

    // watcher 身份写入事务：普通启动看到 watcher 存活时不擅自回滚
    tx.watcherPid = GetCurrentProcessId();
    WriteTxState(&tx, L"swapped");
    LogLine(L"[apply] 目录已切换，试启动新版本");

    RemoveFileQuiet(g_healthPath);
    DWORD newPid = 0;
    HANDLE newProcess = LaunchNormal(&newPid);
    if (newProcess == NULL)
    {
        LogLine(L"[apply] 新版本启动失败，回滚");
        if (RollbackProgram(&tx))
            RemoveFileQuiet(g_txPath);
        else
            LogLine(L"[apply] 回滚失败，保留事务记录等下次启动重试");
        DWORD ignoredPid = 0;
        HANDLE restarted = LaunchNormal(&ignoredPid);
        if (restarted != NULL) CloseHandle(restarted);
        ReleaseApplyLock(mutex);
        return 1;
    }

    if (WaitForHealth(tx.target, APPLY_HEALTH_TIMEOUT_MS, &newPid))
    {
        LogLine(L"[apply] 健康回执有效，升级成功：%s", tx.target);
        DeleteTree(tx.backup);
        RemoveFileQuiet(g_healthPath);
        RemoveFileQuiet(g_txPath);
        CloseHandle(newProcess);
        ReleaseApplyLock(mutex);
        return 0;
    }

    LogLine(L"[apply] 未收到健康回执，回滚到上一版");
    KillProcessTree(newPid);
    // 回滚要移动程序目录：必须先确认试启动进程真的退出，否则移动会失败或留下半套文件。
    if (WaitForSingleObject(newProcess, 15000) == WAIT_TIMEOUT)
    {
        LogLine(L"[apply] 试启动进程未退出，暂不回滚（保留事务记录）");
        CloseHandle(newProcess);
        ReleaseApplyLock(mutex);
        return 1;
    }
    CloseHandle(newProcess);
    if (RollbackProgram(&tx))
        RemoveFileQuiet(g_txPath);
    else
        LogLine(L"[apply] 回滚失败，保留事务记录等下次启动重试");
    RemoveFileQuiet(g_healthPath);
    DWORD restartPid = 0;
    HANDLE restartedAgain = LaunchNormal(&restartPid);
    if (restartedAgain != NULL) CloseHandle(restartedAgain);
    ReleaseApplyLock(mutex);
    return 1;
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow)
{
    (void)hInstance;
    (void)hPrevInstance;
    (void)nCmdShow;

    if (!GetModuleFileNameW(NULL, g_installRoot,
                            (DWORD)(sizeof(g_installRoot) / sizeof(g_installRoot[0]))))
    {
        FatalBox(L"无法获取启动器自身路径。");
        return 1;
    }
    wchar_t *slash = wcsrchr(g_installRoot, L'\\');
    if (slash == NULL)
    {
        FatalBox(L"无法解析安装目录。");
        return 1;
    }
    *slash = 0;

    if (!JoinPath(g_programDir, PATH_CAP, g_installRoot, L"program") ||
        !JoinPath(g_updateDir, PATH_CAP, g_installRoot, L".update") ||
        !JoinPath(g_txPath, PATH_CAP, g_updateDir, L"transaction.txt") ||
        !JoinPath(g_healthPath, PATH_CAP, g_updateDir, L"health.txt") ||
        !JoinPath(g_logPath, PATH_CAP, g_updateDir, L"apply.log"))
    {
        FatalBox(L"路径过长，无法继续。");
        return 1;
    }
    BuildLockName();
    g_isUpdateChild = GetEnvironmentVariableW(L"MAARM_UPDATE_CHILD", NULL, 0) > 0;

    // 事务执行模式：--apply <事务文件>
    if (wcsstr(lpCmdLine, L"--apply") != NULL)
    {
        wchar_t txArg[PATH_CAP] = {0};
        const wchar_t *p = wcsstr(lpCmdLine, L"--apply");
        p += wcslen(L"--apply");
        while (*p == L' ' || *p == L'\t')
            p++;
        if (*p == L'"')
        {
            p++;
            size_t i = 0;
            while (*p != 0 && *p != L'"' && i + 1 < PATH_CAP)
                txArg[i++] = *p++;
            txArg[i] = 0;
        }
        else
        {
            wcsncpy_s(txArg, PATH_CAP, p, _TRUNCATE);
            // 去掉尾部空白
            size_t len = wcslen(txArg);
            while (len > 0 && (txArg[len - 1] == L' ' || txArg[len - 1] == L'\r' ||
                               txArg[len - 1] == L'\n' || txArg[len - 1] == L'\t'))
                txArg[--len] = 0;
        }
        if (txArg[0] == 0)
        {
            FatalBox(L"--apply 缺少事务文件路径。");
            return 1;
        }
        return RunApply(txArg);
    }

    // 普通启动：先处理未完成事务，再决定内容根（旧解压布局不创建 .update）。
    // 恢复与 --apply 共用同一把锁：更新器登记 watcher 之前，恢复不得把正在交接的
    // prepared 当垃圾清掉。试启动拉起的子入口（已带标记）跳过本段：父进程正持锁，
    // 它既不能重复恢复，也不该等锁 30 秒；普通双击遇到锁则提示后退出，绝不绕过。
    if (!g_isUpdateChild)
    {
        if (DirExists(g_programDir) || PathExists(g_txPath))
            EnsureUpdateDir();
        HANDLE recoverMutex = AcquireApplyLock(APPLY_NORMAL_WAIT_MS);
        if (recoverMutex == NULL)
        {
            LogLine(L"[start] 安装锁被占用，普通启动退出");
            FatalBox(L"程序正在更新，请稍候再试。");
            return 1;
        }
        RecoverBeforeLaunch();
        ReleaseApplyLock(recoverMutex);
    }

    BOOL portableLayout = DirExists(g_programDir);
    const wchar_t *contentRoot = portableLayout ? g_programDir : g_installRoot;
    LogLine(L"[start] 启动：portable=%d child=%d install=%s",
            portableLayout ? 1 : 0, g_isUpdateChild ? 1 : 0, g_installRoot);

    wchar_t childExe[PATH_CAP];
    if (portableLayout)
    {
        if (!JoinPath(childExe, PATH_CAP, g_programDir, L"app\\MaaRacingMaster.Shell.exe"))
        {
            FatalBox(L"路径过长，无法构建子进程路径。");
            return 1;
        }
    }
    else
    {
        if (!JoinPath(childExe, PATH_CAP, g_installRoot, L"app\\MaaRacingMaster.Shell.exe"))
        {
            FatalBox(L"路径过长，无法构建子进程路径。");
            return 1;
        }
    }

    if (GetFileAttributesW(childExe) == INVALID_FILE_ATTRIBUTES)
    {
        LogLine(L"[start] 缺少核心程序: %s", childExe);
        wchar_t msg[PATH_CAP + 256];
        swprintf_s(msg, PATH_CAP + 256,
                   L"未找到核心程序：\n%ls\n\n请确认 MaaRacingMaster 安装完整。", childExe);
        FatalBox(msg);
        return 1;
    }

    if (!SetEnvironmentVariableW(L"MaaRM_APP_ROOT", contentRoot))
    {
        LogLine(L"[start] 设置应用根环境变量失败");
        FatalBox(L"无法设置应用根环境变量。");
        return 1;
    }
    if (portableLayout && !SetEnvironmentVariableW(L"MAARM_UPDATE_INSTALL_ROOT", g_installRoot))
    {
        LogLine(L"[start] 设置安装根环境变量失败");
        FatalBox(L"无法设置安装根环境变量。");
        return 1;
    }
    // 壳发起更新事务时要把「旧启动器 PID」写进事务文件（事务执行器要等它退出）
    if (portableLayout)
    {
        wchar_t pidText[32];
        swprintf_s(pidText, 32, L"%lu", (unsigned long)GetCurrentProcessId());
        SetEnvironmentVariableW(L"MAARM_UPDATE_LAUNCHER_PID", pidText);
    }
    SetCurrentDirectoryW(contentRoot);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    SecureZeroMemory(&si, sizeof(si));
    SecureZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    if (!CreateProcessW(childExe, NULL, NULL, NULL, FALSE, 0, NULL, contentRoot, &si, &pi))
    {
        LogLine(L"[start] 启动核心程序失败 err=%lu: %s",
                (unsigned long)GetLastError(), childExe);
        wchar_t msg[PATH_CAP + 256];
        swprintf_s(msg, PATH_CAP + 256, L"无法启动核心程序：\n%ls\n\n错误码: %lu",
                   childExe, (unsigned long)GetLastError());
        FatalBox(msg);
        return 1;
    }
    LogLine(L"[start] 已启动核心程序 pid=%lu child=%d",
            (unsigned long)pi.dwProcessId, g_isUpdateChild ? 1 : 0);

    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exitCode = 1;
    if (!GetExitCodeProcess(pi.hProcess, &exitCode))
        exitCode = 1;
    CloseHandle(pi.hProcess);
    LogLine(L"[start] 核心程序退出码 %lu", (unsigned long)exitCode);
    return (int)exitCode;
}
