"""采集 webview_text_sharp_motion 各变体在多档 DPI 缩放下的渲染截图。

用法（仓库根，用 .venv 的 Python）：
  python tools/experiments/webview_text_sharp_motion/capture.py --dsf 1 1.25 1.5
  python tools/experiments/webview_text_sharp_motion/capture.py --mode headed \
      --dsf 1.25 --variants ref,rept_frac,comp_frac --lcd

无头模式（默认）：msedge --headless=new --screenshot，用 --virtual-time-budget
把 rAF 动画推进到预算耗尽的那一帧后截图（motion_* 变体依赖它）。
有头模式（--mode headed）：--app 独立实例 + 独立 profile，按窗口客户区物理矩形
屏幕截屏——用于 LCD 亚像素渲染证据（无头模式可能整程灰度，LCD 探测必须走真实
浏览器合成管线）。有头实例用一次性 --user-data-dir 隔离，退出时连同 profile 清理。

产物：_out/<mode>_d<dsf>_<variant>[_lcd].png + _out/manifest.json（分析脚本输入）。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import shutil
import subprocess
import time
from pathlib import Path

from PIL import ImageGrab

HERE = Path(__file__).resolve().parent
OUT = HERE / '_out'
PAGE = HERE / 'variants.html'
CSS_W, CSS_H = 1100, 320
WINDOW_TITLE = 'webview_text_sharp_motion variants'

ALL_VARIANTS = [
    'ref', 'rept_frac', 'comp_frac', 'comp_int',
    'comp_frac_3d', 'comp_int_3d',
    'rept_scale', 'comp_scale', 'comp_opacity',
    'center_t50', 'center_auto', 'center_t50_nowc',
    'anim_pause_frac', 'anim_pause_scale', 'anim_mid_scale',
    'js_mid_wc', 'js_mid_nowc',
]
MOTION_BUDGET_MS = 600   # rAF 变体的虚拟时间预算：> 动画时长 500ms，停在确定终值
STATIC_BUDGET_MS = 400

EDGE_CANDIDATES = [
    Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
    Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
]


def intent_css(variant, dsf):
    """文字元素起点的 CSS 意图坐标（页面按此设置 transform/居中）。

    残差真值来自这里而不是截图像素：绘制管线会把任何元素对齐到整数物理像素，
    从像素反推不出 CSS 意图的小数部分；意图坐标由采集端按变体解析式给出，
    残差 = intent_css × dsf 的小数部分。
    """
    snap30 = round(30 * dsf) / dsf
    table = {
        'ref': 40.0,
        'rept_frac': 70.0, 'comp_frac': 70.0, 'comp_frac_3d': 70.0,
        'comp_int': 40 + snap30, 'comp_int_3d': 40 + snap30,
        'center_t50': 90.5, 'center_auto': 90.5, 'center_t50_nowc': 90.5,
        'anim_pause_frac': 47.5, 'anim_pause_scale': 40.0, 'anim_mid_scale': 40.0,
        'rept_scale': 40.0, 'comp_scale': 40.0, 'comp_opacity': 40.0,
        'js_mid_wc': 55.2, 'js_mid_nowc': 55.2,
    }
    return table[variant]


def find_edge(explicit):
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise SystemExit('--edge 指定的路径不存在: %s' % p)
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit('找不到 msedge.exe，用 --edge 显式指定')


def page_url(variant, dsf, lcd):
    url = 'file:///%s?v=%s&dsf=%g' % (str(PAGE.as_posix()).lstrip('/'), variant, dsf)
    if lcd:
        url += '&lcd=1'
    return url


def capture_headless(edge, url, png, dsf, budget, sw_raster):
    flags = ['--headless=new', '--force-device-scale-factor=%g' % dsf,
             '--window-size=%d,%d' % (CSS_W, CSS_H), '--hide-scrollbars',
             '--no-first-run', '--virtual-time-budget=%d' % budget,
             '--screenshot=%s' % png, url]
    if sw_raster:
        flags.insert(1, '--disable-gpu')
    subprocess.run([str(edge)] + flags, capture_output=True, text=True, timeout=60)
    if not png.exists():
        # 新无头失败时退回旧无头再试一次（旧管线同样支持虚拟时间截图）
        flags[0] = '--headless'
        subprocess.run([str(edge)] + flags, capture_output=True, text=True, timeout=60)
    if not png.exists():
        raise SystemExit('无头截图失败: %s' % url)


def _enum_windows():
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        if wt.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            wt.GetClassNameW(hwnd, buf, 256)
            pid = wt.DWORD(0)
            wt.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append((hwnd, buf.value, pid.value))
        return True

    wt.EnumWindows(cb, 0)
    return result


def _window_by_title():
    for hwnd, cls, _pid in _enum_windows():
        if cls != 'Chrome_WidgetWin_1':
            continue
        buf = ctypes.create_unicode_buffer(512)
        wt.GetWindowTextW(hwnd, buf, 512)
        if buf.value == WINDOW_TITLE:
            return hwnd
    return None


def wait_window(pid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, cls, wpid in _enum_windows():
            if wpid == pid and cls == 'Chrome_WidgetWin_1':
                return hwnd
        hwnd = _window_by_title()
        if hwnd is not None:
            return hwnd
        time.sleep(0.3)
    raise SystemExit('有头模式下找不到 Edge 窗口（pid=%d）' % pid)


def client_rect_physical(hwnd):
    """客户区左上角与尺寸，物理像素（进程须先设 Per-Monitor V2 感知）。"""
    rect = wt.RECT()
    if not wt.GetClientRect(hwnd, ctypes.byref(rect)):
        raise SystemExit('GetClientRect 失败')
    pt = wt.POINT(0, 0)
    if not wt.ClientToScreen(hwnd, ctypes.byref(pt)):
        raise SystemExit('ClientToScreen 失败')
    return (pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom)


def capture_headed(edge, url, png, dsf):
    # 截图坐标按物理像素计算：不设感知时按 96 DPI 虚拟化，坐标错位
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    prof = OUT / '_profile'
    if prof.exists():
        shutil.rmtree(prof)
    cmd = [str(edge), '--app=' + url, '--force-device-scale-factor=%g' % dsf,
           '--user-data-dir=%s' % prof, '--no-first-run', '--no-default-browser-check',
           '--window-size=%d,%d' % (CSS_W, CSS_H)]
    proc = subprocess.Popen(cmd)
    try:
        hwnd = wait_window(proc.pid)
        time.sleep(3.0)  # 首帧渲染与合成稳定
        rect = client_rect_physical(hwnd)
        img = ImageGrab.grab(bbox=rect, all_screens=True)
        img.save(png)
        css_w = (rect[2] - rect[0]) / dsf
        return {'css_w': css_w, 'png_w': img.width, 'png_h': img.height}
    finally:
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       capture_output=True)
        time.sleep(0.5)
        if prof.exists():
            shutil.rmtree(prof, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['headless', 'headed'], default='headless')
    ap.add_argument('--dsf', nargs='+', type=float, default=[1.0, 1.25, 1.5])
    ap.add_argument('--variants', default=','.join(ALL_VARIANTS))
    ap.add_argument('--lcd', action='store_true',
                    help='追加 &lcd=1（纯灰文字，用于 LCD 亚像素渲染探测）')
    ap.add_argument('--edge', default=None)
    ap.add_argument('--sw-raster', action='store_true',
                    help='无头模式追加 --disable-gpu（软件栅格对照）')
    args = ap.parse_args()

    edge = find_edge(args.edge)
    variants = [v.strip() for v in args.variants.split(',') if v.strip()]
    unknown = [v for v in variants if v not in ALL_VARIANTS]
    if unknown:
        raise SystemExit('未知变体: %s' % unknown)
    OUT.mkdir(exist_ok=True)

    manifest_path = OUT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else []
    # 清理已退役变体的残留条目与截图，避免废弃数据混进分析
    stale = [m for m in manifest if m['variant'] not in ALL_VARIANTS]
    for m in stale:
        png = OUT / m['png']
        if png.exists():
            png.unlink()
    manifest = [m for m in manifest if m['variant'] in ALL_VARIANTS]
    if stale:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding='utf-8')

    for dsf in args.dsf:
        for v in variants:
            tag = '%s_d%g_%s%s' % (args.mode, dsf, v, '_lcd' if args.lcd else '')
            png = OUT / (tag + '.png')
            url = page_url(v, dsf, args.lcd)
            info = {'tag': tag, 'variant': v, 'dsf': dsf, 'mode': args.mode,
                    'lcd': args.lcd, 'png': png.name, 'css_w': CSS_W,
                    'intent_css': intent_css(v, dsf)}
            if args.mode == 'headless':
                capture_headless(edge, url, png, dsf, STATIC_BUDGET_MS, args.sw_raster)
            else:
                info.update(capture_headed(edge, url, png, dsf))
            info['sw_raster'] = args.sw_raster
            manifest = [m for m in manifest
                        if not (m['tag'] == tag and m.get('sw_raster') == args.sw_raster)]
            manifest.append(info)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                     encoding='utf-8')
            print('captured', tag)

    print('done ->', manifest_path)


if __name__ == '__main__':
    main()
