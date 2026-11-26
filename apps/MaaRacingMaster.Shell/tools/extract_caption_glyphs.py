"""从 Windows 自带的标题栏字形里导出窗口控制图标的 SVG 路径（10×10 视框，1 单位 = 1px）。

为什么这么做：标题栏按钮的图标不自己画。系统 caption button 的形状就在
`SegoeIcons.ttf`（Win11 `Segoe Fluent Icons`，Win10 `segmdl2.ttf` 同码位）里——最小化是一根
两端圆角的 1px 横条、最大化是圆角 1.5px 的方框、还原是两个错位的圆角方框、关闭是圆角端点的
斜叉。手绘的对不上这些圆角（原始版本就是尖角方框 + 光秃秃的斜线，看着比原生糙）。

为什么不用图标字体文本：WebView2 的文字走 ClearType，10px 字形只有 1px 笔画，会被亚像素拆成
两列红蓝彩边；强制灰度又会把笔画摊成两列浅灰。直接取轮廓画成矢量，只有圆角/斜线走灰度抗锯齿，
直线部分（外框 0/10、内框 1/9、横条 0/1）正好落在整像素上。

用法：
  python apps/MaaRacingMaster.Shell/tools/extract_caption_glyphs.py            # 打印四个 d 字符串
  python apps/MaaRacingMaster.Shell/tools/extract_caption_glyphs.py --check ../frontend/index.html
                                                                             # 校验 html 里的路径没被手改
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\SegoeIcons.ttf"),   # Segoe Fluent Icons（Win11）
    Path(r"C:\Windows\Fonts\segmdl2.ttf"),      # Segoe MDL2 Assets（Win10，同码位）
)
CODEPOINTS = {"min": 0xE921, "max": 0xE922, "restore": 0xE923, "close": 0xE8BB}
BOX = 10.0  # 图标视框边长（px）


class Ttf:
    """只读 sfnt 的最小解析：表目录 / head / maxp / loca / glyf / cmap(format 4, 12)。"""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        num = struct.unpack(">H", self.data[4:6])[0]
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(num):
            tag, _, off, length = struct.unpack(">4sIII", self.data[12 + i * 16: 12 + (i + 1) * 16])
            self.tables[tag.decode()] = (off, length)
        head = self.tables["head"][0]
        self.upem = struct.unpack(">H", self.data[head + 18: head + 20])[0]
        self.loc_fmt = struct.unpack(">h", self.data[head + 50: head + 52])[0]
        self.loca = self.tables["loca"][0]
        self.glyf = self.tables["glyf"][0]

    def _gid_range(self, gid: int) -> tuple[int, int]:
        if self.loc_fmt == 0:
            a, b = struct.unpack(">HH", self.data[self.loca + gid * 2: self.loca + gid * 2 + 4])
            return a * 2, b * 2
        return struct.unpack(">II", self.data[self.loca + gid * 4: self.loca + gid * 4 + 8])

    def gid(self, cp: int) -> int:
        off = self.tables["cmap"][0]
        n = struct.unpack(">H", self.data[off + 2: off + 4])[0]
        for i in range(n):
            rec = off + 4 + i * 8
            _, _, sub_off = struct.unpack(">HHI", self.data[rec: rec + 8])
            sub = off + sub_off
            fmt = struct.unpack(">H", self.data[sub: sub + 2])[0]
            if fmt == 4 and cp < 0xFFFF:
                seg2 = struct.unpack(">H", self.data[sub + 6: sub + 8])[0]
                segs = seg2 // 2
                ends = struct.unpack(f">{segs}H", self.data[sub + 14: sub + 14 + seg2])
                starts = struct.unpack(f">{segs}H", self.data[sub + 16 + seg2: sub + 16 + 2 * seg2])
                deltas = struct.unpack(f">{segs}h", self.data[sub + 16 + 2 * seg2: sub + 16 + 3 * seg2])
                ro = sub + 16 + 3 * seg2
                ranges = struct.unpack(f">{segs}H", self.data[ro: ro + seg2])
                for k in range(segs):
                    if starts[k] <= cp <= ends[k]:
                        if ranges[k] == 0:
                            return (cp + deltas[k]) & 0xFFFF
                        addr = ro + 2 * k + ranges[k] + 2 * (cp - starts[k])
                        g = struct.unpack(">H", self.data[addr: addr + 2])[0]
                        return (g + deltas[k]) & 0xFFFF if g else 0
            elif fmt == 12:
                groups = struct.unpack(">I", self.data[sub + 12: sub + 16])[0]
                for g in range(groups):
                    s, e, sg = struct.unpack(">III", self.data[sub + 16 + g * 12: sub + 28 + g * 12])
                    if s <= cp <= e:
                        return sg + (cp - s)
        return 0

    def contours(self, gid: int, depth: int = 0, dx: float = 0, dy: float = 0):
        start, end = self._gid_range(gid)
        if start == end:
            return []
        d = self.data[self.glyf + start: self.glyf + end]
        ncont = struct.unpack(">h", d[0:2])[0]
        if ncont >= 0:
            ends = struct.unpack(f">{ncont}H", d[10: 10 + 2 * ncont])
            npts = ends[-1] + 1
            ins = struct.unpack(">H", d[10 + 2 * ncont: 12 + 2 * ncont])[0]
            p = 12 + 2 * ncont + ins
            flags: list[int] = []
            while len(flags) < npts:
                f = d[p]
                p += 1
                flags.append(f)
                if f & 0x08:
                    flags.extend([f] * d[p])
                    p += 1
            flags = flags[:npts]
            xs, ys = [], []
            v = 0
            for f in flags:
                if f & 0x02:
                    step = d[p]
                    p += 1
                    v += step if f & 0x10 else -step
                elif not f & 0x10:
                    v += struct.unpack(">h", d[p: p + 2])[0]
                    p += 2
                xs.append(v)
            v = 0
            for f in flags:
                if f & 0x04:
                    step = d[p]
                    p += 1
                    v += step if f & 0x20 else -step
                elif not f & 0x20:
                    v += struct.unpack(">h", d[p: p + 2])[0]
                    p += 2
                ys.append(v)
            out, s = [], 0
            for e in ends:
                out.append([(xs[i] + dx, ys[i] + dy, bool(flags[i] & 1)) for i in range(s, e + 1)])
                s = e + 1
            return out
        # 复合字形：只处理 ARGS_ARE_XY_VALUES 的平移（caption 字形用不到缩放/旋转）
        out = []
        p = 10
        while True:
            flags, gidx = struct.unpack(">HH", d[p: p + 4])
            p += 4
            if flags & 0x0001:
                a1, a2 = struct.unpack(">hh" if flags & 0x0002 else ">HH", d[p: p + 4])
                p += 4
            else:
                a1, a2 = struct.unpack(">bb" if flags & 0x0002 else ">BB", d[p: p + 2])
                p += 2
            if flags & 0x0008:
                p += 2
            elif flags & 0x0040:
                p += 4
            elif flags & 0x0080:
                p += 4
            out.extend(self.contours(gidx, depth + 1, dx + a1, dy + a2))
            if not flags & 0x0020:
                break
        return out


def _fmt(v: float) -> str:
    """2 位小数、去掉多余的 0（0.01px 的误差在 10px 图标上看不出来，但能让路径短一半）。"""
    s = f"{round(v, 2):g}"
    return "0" if s == "-0" else s


def _contour_to_path(pts, scale: float, upem: int) -> str:
    """TrueType 二次曲线 → SVG path（Q + Z）。"""
    def xy(p):
        return p[0] * scale, (upem - p[1]) * scale

    on = [p for p in pts if p[2]]
    if not on:
        return ""
    # 起点：若首点在曲线上就从它开始，否则取首末控制点的中点
    if pts[0][2]:
        start = pts[0]
        body = pts[1:] + pts[:1]
    else:
        mid = ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2)
        start = (mid[0], mid[1], True)
        body = pts[:]
    sx, sy = xy(start)
    parts = [f"M{_fmt(sx)} {_fmt(sy)}"]
    last = (sx, sy)
    i = 0
    n = len(body)
    while i < n:
        cur = body[i]
        if cur[2]:
            x, y = xy(cur)
            if (x, y) != last:
                parts.append(f"L{_fmt(x)} {_fmt(y)}")
                last = (x, y)
            i += 1
            continue
        nxt = body[(i + 1) % n]
        end = nxt if nxt[2] else ((nxt[0] + cur[0]) / 2, (nxt[1] + cur[1]) / 2, True)
        cx, cy = xy(cur)
        ex, ey = xy(end)
        parts.append(f"Q{_fmt(cx)} {_fmt(cy)} {_fmt(ex)} {_fmt(ey)}")
        last = (ex, ey)
        i += 1
        if not nxt[2]:
            # 两个连续控制点：上一步已收在它们的中点上，下一个循环从该点继续
            body[i - 1] = end
    parts.append("Z")
    return "".join(parts)


def glyph_paths(font_path: Path | None = None) -> dict[str, str]:
    if font_path is None:
        for cand in FONT_CANDIDATES:
            if cand.exists():
                font_path = cand
                break
        else:
            raise FileNotFoundError("找不到 SegoeIcons.ttf / segmdl2.ttf")
    font = Ttf(font_path)
    scale = BOX / font.upem
    out = {}
    for name, cp in CODEPOINTS.items():
        cs = font.contours(font.gid(cp))
        if not cs:
            raise ValueError(f"{font_path.name} 里 U+{cp:04X}（{name}）没有轮廓")
        out[name] = "".join(_contour_to_path(c, scale, font.upem) for c in cs)
    return out


def main(argv: list[str]) -> int:
    paths = glyph_paths()
    if len(argv) >= 3 and argv[1] == "--check":
        html = Path(argv[2]).read_text(encoding="utf-8")
        bad = []
        for name, d in paths.items():
            if d not in html:
                bad.append(name)
        if bad:
            print(f"index.html 的图标路径与系统字形不一致：{', '.join(bad)}", file=sys.stderr)
            return 1
        print("ok")
        return 0
    for name, d in paths.items():
        print(f"{name}: {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
