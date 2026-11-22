"""webview_text_sharp_motion：标定、度量与判决。

标尺独立于待验证对象（C 类硬约束 2）：
  1) 已知真值标定——对同一参考截图施加 PIL 高斯模糊（σ 已知，与 Chromium 无关），
     锐度指标必须随半径单调下降，标尺无效则全部判决作废；
  2) LCD 探测——纯灰文字(#333)在纯白底上文本色 RGB 相等，边缘像素出现 RGB 散布
     只能来自 ClearType 式亚像素渲染（物理事实，不依赖 Chromium 行为声明）。

判决全部同框、同字号、同 dsf，只变合成状态与偏移对齐方式；每组截图自带进度条
真值（文字起点 CSS 坐标），残差 = 起点×dsf 的小数部分——「糊 ⟺ 设备像素分数残差」
这条因果链用全部截图的 残差→锐度 相关表直接呈现，不靠单一对照。

用法：
  python tools/experiments/webview_text_sharp_motion/analyze.py
输出：控制台判决表 + _out/metrics.json。
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE / '_out'

# 裁剪框（CSS px）：text_* 是主样本块（含 +30px 位移后的位置），center_* 是居中块，
# bar 是进度条条带（反推每张截图的实际位移真值）。
CROP_TEXT = (36, 112, 640, 160)
CROP_CENTER = (36, 212, 300, 256)
CROP_BAR = (36, 166, 700, 182)
BAR_GRAY_MAX = 120      # 进度条为 #1F2328 深色，底为白


def load_rgb(png):
    return Image.open(png).convert('RGB')


def crop_css(img, box, dpr_eff):
    x0, y0, x1, y1 = [int(round(v * dpr_eff)) for v in box]
    return img.crop((x0, y0, x1, y1))


def gray_arr(img):
    return np.asarray(img.convert('L'), dtype=float)


def laplacian_var(g):
    core = (g[1:-1, 1:-1] * -4 + g[:-2, 1:-1] + g[2:, 1:-1]
            + g[1:-1, :-2] + g[1:-1, 2:])
    return float(core.var())


def tv(g):
    dx = np.abs(np.diff(g, axis=1)).mean()
    dy = np.abs(np.diff(g, axis=0)).mean()
    return float(dx + dy)


def chroma(img):
    """边缘像素的 RGB 散布（LCD 亚像素渲染证据）与文本核心散布（颜色基线）。"""
    a = np.asarray(img, dtype=float)
    g = np.asarray(img.convert('L'), dtype=float)
    spread = a.max(axis=2) - a.min(axis=2)
    e = np.zeros_like(g, dtype=bool)
    e[:, :-1] |= np.abs(np.diff(g, axis=1)) > 10
    e[:-1, :] |= np.abs(np.diff(g, axis=0)) > 10
    edge = float(spread[e].mean()) if e.any() else 0.0
    core_mask = g < 100
    core = float(spread[core_mask].mean()) if core_mask.any() else 0.0
    return edge, core


def measure_shot(entry):
    img = load_rgb(OUT / entry['png'])
    dpr_eff = img.width / entry['css_w']
    is_center = entry['variant'].startswith('center_')
    text = crop_css(img, CROP_CENTER if is_center else CROP_TEXT, dpr_eff)
    g = gray_arr(text)
    # 进度条右端只作 sanity（绘制管线已把它对齐到整数物理像素，测不出 CSS 意图小数）；
    # 残差真值 = 采集端记录的 CSS 意图坐标 × dsf 的小数部分
    strip = crop_css(img, CROP_BAR, dpr_eff)
    gs = gray_arr(strip)
    dark_cols = np.where((gs < BAR_GRAY_MAX).any(axis=0))[0]
    bar_end_css = (dark_cols.max() + 1) / dpr_eff if dark_cols.size else 40.0
    residue = (entry['intent_css'] * dpr_eff) % 1.0
    if residue > 0.5:
        residue = 1.0 - residue   # 等效残差：0.9 与 0.1 的重采样强度同级
    ce, cc = chroma(text)
    return {'tag': entry['tag'], 'variant': entry['variant'], 'dsf': entry['dsf'],
            'png': entry['png'],
            'mode': entry['mode'], 'lcd': entry.get('lcd', False),
            'sw_raster': entry.get('sw_raster', False),
            'dpr_eff': round(dpr_eff, 4), 'bar_end_css': round(bar_end_css, 3),
            'residue': round(residue, 4),
            'lap_var': laplacian_var(g), 'tv': tv(g),
            'chroma_edge': ce, 'chroma_core': cc}


def calibrate(shots):
    """已知真值：PIL 高斯模糊（σ 已知）施加在参考截图上，指标须单调下降。"""
    base = next((s for s in shots
                 if s['variant'] == 'ref' and s['dsf'] == 1.25
                 and s['mode'] == 'headless' and not s['lcd']), None)
    if base is None:
        base = next((s for s in shots
                     if s['variant'] == 'ref' and s['mode'] == 'headless'
                     and not s['lcd']), None)
    if base is None:
        return None, 'SKIP: 找不到无头 ref 截图'
    img = crop_css(load_rgb(OUT / base['png']), CROP_TEXT, base['dpr_eff'])
    rows, prev = [], None
    for sigma in (0.0, 0.3, 0.6, 1.2):
        g = gray_arr(img.filter(__import__('PIL.ImageFilter', fromlist=['GaussianBlur'])
                                .GaussianBlur(sigma)))
        lap = laplacian_var(g)
        rows.append({'sigma': sigma, 'lap_var': lap})
        if prev is not None and lap >= prev:
            return rows, 'FAIL: σ=%.2g 起指标未下降，标尺无效，全部判决作废' % sigma
        prev = lap
    return rows, 'PASS'


def get(shots, variant, dsf, mode='headless', lcd=False):
    return next((s for s in shots if s['variant'] == variant and s['dsf'] == dsf
                 and s['mode'] == mode and s['lcd'] == lcd), None)


def verdicts(shots):
    out = []

    def add(name, ok, note):
        out.append({'verdict': name,
                    'result': 'PASS' if ok else 'FAIL',
                    'note': note})

    c = get(shots, 'comp_frac', 1.25)
    i = get(shots, 'comp_int', 1.25)
    r = get(shots, 'rept_frac', 1.25)
    if c and i:
        add('V2a 合成 2D 平移：分数偏移与对齐后同锐（合成器已做像素对齐）',
            c['lap_var'] >= 0.85 * i['lap_var'],
            'comp_frac=%.0f comp_int=%.0f' % (c['lap_var'], i['lap_var']))
    c3 = get(shots, 'comp_frac_3d', 1.25)
    if c3 and c:
        add('V3d 合成 3D 平移与 2D 同锐（合成器对 3D 矩阵同样做光栅适配）',
            c3['lap_var'] >= 0.85 * c['lap_var'],
            'comp_frac_3d=%.0f comp_frac=%.0f' % (c3['lap_var'], c['lap_var']))
    for dsf in (1.0, 1.5):
        c = get(shots, 'comp_frac', dsf)
        i = get(shots, 'comp_int', dsf)
        if c and i:
            add('V3 整数 dsf=%g 下合成分数偏移不额外糊' % dsf,
                c['lap_var'] >= 0.85 * i['lap_var'],
                'comp_frac=%.0f comp_int=%.0f' % (c['lap_var'], i['lap_var']))
    t = get(shots, 'center_t50', 1.0)
    a = get(shots, 'center_auto', 1.0)
    tn = get(shots, 'center_t50_nowc', 1.0)
    if t and a:
        add('V4 transform 居中@1.0（残差 0.5）糊于布局居中',
            t['lap_var'] < 0.85 * a['lap_var'],
            'center_t50=%.0f center_auto=%.0f' % (t['lap_var'], a['lap_var']))
    if tn and a:
        add('V8 重绘路径对分数居中免疫（居中的糊全部来自合成层）',
            abs(tn['lap_var'] - a['lap_var']) < 0.15 * a['lap_var'],
            'center_t50_nowc=%.0f center_auto=%.0f' % (tn['lap_var'], a['lap_var']))
    snp = get(shots, 'comp_int', 1.25)
    r25 = get(shots, 'rept_frac', 1.25)
    mw = get(shots, 'js_mid_wc', 1.25)
    mn = get(shots, 'js_mid_nowc', 1.25)
    if mw and snp:
        add('V5 合成层中间帧与静止同锐（逐帧写样式不产生额外糊）',
            mw['lap_var'] >= 0.85 * snp['lap_var'],
            'js_mid_wc=%.0f comp_int=%.0f' % (mw['lap_var'], snp['lap_var']))
    if mn and r25:
        add('V6 重绘路径中间帧与静止同锐',
            mn['lap_var'] >= 0.85 * r25['lap_var'],
            'js_mid_nowc=%.0f rept_frac=%.0f' % (mn['lap_var'], r25['lap_var']))
    apf = get(shots, 'anim_pause_frac', 1.25)
    if apf and snp:
        add('V12 CSS 动画中间帧（暂停建模）与静止同锐',
            apf['lap_var'] >= 0.85 * snp['lap_var'],
            'anim_pause_frac=%.0f comp_int=%.0f' % (apf['lap_var'], snp['lap_var']))
    aps = get(shots, 'anim_pause_scale', 1.25)
    cs = get(shots, 'comp_scale', 1.25)
    if aps and cs:
        add('V11a scale 稳定中间态（暂停建模）不糊',
            aps['lap_var'] >= 0.85 * cs['lap_var'],
            'anim_pause_scale=%.0f comp_scale=%.0f' % (aps['lap_var'], cs['lap_var']))
    am = get(shots, 'anim_mid_scale', 1.25)
    if am and cs:
        add('V11b scale 动画进行中（虚拟时间截停，非稳定态）发糊',
            am['lap_var'] < 0.85 * cs['lap_var'],
            'anim_mid_scale=%.0f comp_scale=%.0f' % (am['lap_var'], cs['lap_var']))
    rs = get(shots, 'rept_scale', 1.25)
    cs = get(shots, 'comp_scale', 1.25)
    co = get(shots, 'comp_opacity', 1.25)
    if rs and cs:
        add('V9 合成路径 scale 发糊（重绘 scale 不糊）——文字禁 scale 动画的依据',
            cs['lap_var'] < 0.7 * rs['lap_var'],
            'comp_scale=%.0f rept_scale=%.0f' % (cs['lap_var'], rs['lap_var']))
    if co and i:
        add('V10 opacity 动画不重采样（fade 安全）',
            co['lap_var'] >= 0.85 * i['lap_var'],
            'comp_opacity=%.0f comp_int=%.0f' % (co['lap_var'], i['lap_var']))
    # V7：LCD 亚像素渲染证据（无头 new 管线即含 LCD；chroma_edge 是纯灰文字的彩色散布）
    r25 = get(shots, 'rept_frac', 1.25)
    c25 = get(shots, 'comp_frac', 1.25)
    if r25 and c25:
        add('V7a 重绘路径有 LCD、合成层失去 LCD',
            r25['chroma_edge'] > 3 * max(c25['chroma_edge'], 0.3),
            'rept=%.2f comp=%.2f (chroma_edge)'
            % (r25['chroma_edge'], c25['chroma_edge']))
    if mn and mw:
        add('V7b 重绘路径中间帧仍保 LCD（合成层中间帧无 LCD）',
            mn['chroma_edge'] > 3 * max(mw['chroma_edge'], 0.3),
            'js_mid_nowc=%.2f js_mid_wc=%.2f (chroma_edge)'
            % (mn['chroma_edge'], mw['chroma_edge']))
    return out


def residue_table(shots):
    """全部非 LCD 截图：残差桶 → 平均锐度。「糊 ⟺ 分数残差」的直接证据。"""
    rows = {}
    for s in shots:
        if s['lcd']:
            continue
        q = round(s['residue'] * 8) / 8.0
        rows.setdefault(q, []).append(s['lap_var'])
    return [{'residue_bucket': q, 'n': len(v), 'lap_var_mean': sum(v) / len(v),
             'lap_var_min': min(v), 'lap_var_max': max(v)}
            for q, v in sorted(rows.items())]


def main():
    manifest = json.loads((OUT / 'manifest.json').read_text(encoding='utf-8'))
    shots = [measure_shot(e) for e in manifest]
    cal_rows, cal_result = calibrate(shots)

    print('=== 标定（PIL 高斯模糊 σ 已知 → lap_var 须单调下降）：%s' % cal_result)
    for r in (cal_rows or []):
        print('  σ=%.2g lap_var=%.0f' % (r['sigma'], r['lap_var']))

    print('\n=== 残差 → 锐度相关表（全部非 LCD 截图，lap_var 越高越锐）')
    for r in residue_table(shots):
        print('  残差≈%.3g  n=%d  lap_var≈%.0f  [%d..%d]'
              % (r['residue_bucket'], r['n'], r['lap_var_mean'],
                 r['lap_var_min'], r['lap_var_max']))

    print('\n=== 截图逐项')
    for s in shots:
        print('  %-28s dpr=%.4g 残差=%.3f lap=%7.0f tv=%5.2f chroma_e=%5.2f core=%4.1f'
              % (s['tag'], s['dpr_eff'], s['residue'], s['lap_var'], s['tv'],
                 s['chroma_edge'], s['chroma_core']))

    print('\n=== 判决')
    vs = verdicts(shots)
    for v in vs:
        print('  [%s] %s  (%s)' % (v['result'], v['verdict'], v['note']))
    if not any(v['verdict'].startswith('V7') for v in vs):
        print('  [SKIP] V7 LCD 证据：缺有头 lcd 截图（capture.py --mode headed --lcd）')

    (OUT / 'metrics.json').write_text(
        json.dumps({'shots': shots, 'calibration': cal_rows,
                    'calibration_result': cal_result,
                    'verdicts': vs}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    print('\n->', OUT / 'metrics.json')


if __name__ == '__main__':
    main()
