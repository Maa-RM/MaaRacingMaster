# -*- coding: utf-8 -*-
"""轻量车道线观测（TwinLiteNet+ Small）的推理、跟踪与状态机。

线可用时是生产供数链的位置原点（`line_primary`），也供车道成员插值；
不可用时控制链退回深度/墙，不抛进控制环。模型随可选模型包分发：缺失时
worker 不启动并返回 None，深度档本身不受影响。

生产者：`LaneObsWorker` 在独立线程跑（默认 provider=cpu，不抢 GPU 锁——
实测 dml 与深度共用锁会相位锁，把深度饿到只剩 5% 帧；dml 仍可选，但要
自证不与深度争锁），队列深度 1，控制拍只 push 最新帧并读 `latest()`。

坐标系与离线 gold 同源：地面线满足 ``x_px = VPX + L*LANE_W*(y-Y_H)/cam_h``，
线的横向偏移 ``L = slope*cam_h/LANE_W``（与道号无关）；自车横位
``x = k - L``。**k 不再由颜色/黄边界决定**：每拍用深度路心做锚点
``k_i = round(anchor + L_i)``，等间距与残差门剔除野线，多条线取残差中值
还原横位。多线共同证实的新鲜锚点当拍重锚，单线仍等连续两拍；深度空窗里
新线由所有在期线投票定号，未过门的新线与被拒线都不留道号。线锁建立后
网格冻结：新线不再靠相对票补位，只能等跳变/重锁后由深度锚重新定号。颜色
不参与提取与道号（2026-11-11 起：概率图提取不使用任何颜色匹配，线字段恒
"?"，仅供显示兜底）。

状态机：
  TRACK  有通过等间距/残差门的线：输出锚定后的绝对横位；
  COAST  短断链（≤``COAST_MAX_TICKS``）：保持上次横位，用恒速+横向速度预测；
  RELOCK 长断链/跳帧：清空可用性，等下一拍深度锚点或线重锁。
"""
from __future__ import annotations

import math
import queue
import threading
from collections import deque

from maaracing_master.core.perf_sampler import beat
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import model_assets

LANE_W = 3.5
VPX = 640.7
Y_H = 354.2
DEFAULT_CAM_H = 1.28
# 车心离路心的物理上限（道）：路缘实测 ≈2.5 道（2026-10-31 帧 0112 目检
# 车正压右黄线，横位 1.92），留 0.1 道余量。超过即坐标失效（多为路心锚
# 整道错位/坏平面），不能拿它给线定道号。生产值由 policy.validate.
# x_lane_abs_max 注入（LaneObsWorker 构造参数），此处只是直构缺省。
OFF_MAX = 2.6
# 线角焦距 = gate0 标定的相机焦距（全幅像素），由调用方随帧传入：
# 线角正切 = 像素偏置 ÷ 焦距。注意它与合成点云的固定投影焦距（630）不是
# 同一个口径（后者是点云/框投影的焦距，见 depth_geo 模块头）——拿它代入会把车头角放大 1.2 倍、在线速度
# 比例反向压低（HUD 得分速度锚实测 0.9→0.62）。本模块不再持有焦距字面量。
FOCAL_LO = 400.0
FOCAL_HI = 1400.0
FOCAL_EMA = 0.25          # 逐帧焦距轻平滑（场景内容会让估计帧间抖 ±10%）
# 线几何可用的相机高带（v5 2026-11-12）：输入已从「深度平面 c」换成车道线
# 反解值，与视角守卫同源。带要覆盖两种合法机位（保险杠 ≈1.25、追尾 ≈1.9），
# **不是视角判定门**（判定门在 view_guard，仍只认保险杠）；上界只挡反解失败
# 的离谱值。旧带 1.12~1.55 会把追尾机位砍回标定值 1.28，线横位静默错 1~2
# 道；旧注释「坏平面 coef[2] 飙到 3.2~4.5」是 v4 事实，v5 下 c 恒定。
# 放宽后的单点离群由 module 侧 5 拍中位滤除，本带只兜底。
CAM_H_LO = 1.10
CAM_H_HI = 2.20
NEAR_Y = (560.0, 720.0)
FAR_Y = (440.0, 545.0)
PROB_THRESH = 0.30
# 灭点行（地平线）估计的三级门（2026-11-11 车道几何探针定案）：步长 1 后远场
# 虚线碎片变多，稀疏碎片两两交点会给出 300 行的假灭点。俯仰物理门约 ±2°
# （fy=630 下 ±22px），行 520 处线距门挡「同线碎片/跨太多道」，MAD 挡少数派。
VANISH_Y_LO, VANISH_Y_HI = 341.0, 381.0
VANISH_PROBE_Y = 520.0
VANISH_SPAN_LO, VANISH_SPAN_HI = 60.0, 900.0
# 灭点行的时间平滑（2026-11-12）：单帧两两交点中位仍有 ±2~3px 的帧间抖，而它
# 是合成点图与逐车距离的**尺度基准**——1px 约 40m 处 5%、66m 处 8%，逐帧直发
# 会让同一辆车 0.1s 内的距离读数摆 20~40%（实机段实测 −27%~+43%）。地平线是
# 慢变量（俯仰随载重/坡度变），取近 YV_WIN 个检测拍的中位即可，代价 0.1~0.2s
# 的滞后；间隔超过 YV_GAP_S 说明中间断过（丢检/换段），旧样本不再代表当前姿态。
YV_WIN = 5
YV_GAP_S = 1.0
# 相机高反解（线间距法）的物理带与样本门：结果只给视角守卫用（粗门），
# 与深度链的 1.25 无耦合关系。
CAM_H_EST_LO, CAM_H_EST_HI = 0.6, 3.0
CAM_H_EST_MIN_ROWS = 10.0     # 两条线在同一行的可比对行窗（行）
CAM_H_EST_MIN_DX = 5.0        # 同一行上两线的像素间距下限（重合线不算）
TRACK_TOL = 0.15
TRACK_GAP = 6
COAST_MAX_TICKS = 10
# 线为主（2026-10-31）：线自身连续稳定这么多拍后，道号不再让新鲜深度改判，
# 深度也不再能否决线——整道级深度跳变时两根线会"整齐地"支持错误网格，正是
# 路心整道跳、场景参考系反复否决的来源。
# 锁定期道号同时冻结：新线只能等重锁后由深度锚定号，不能靠邻居相对票补位
# （车跨线后线组换新时，相对票会给整道偏移的号且横位保持连续，锁定不重置）。
LINE_LOCK_TICKS = 5
# 整道级复核（2026-11-13）：锁定期深度不许改判道号（见上），但"错得离谱"
# 必须能纠正。判据=新鲜深度锚与线网格横位**连续差超一整道**（半道级分歧是
# 噪声，不触发），且最近 ``GROSS_WIN`` 个新鲜锚里至少 ``GROSS_HITS`` 个如此。
# 触发后解冻一次：道号走正常重锚流程（每线仍需 ANCHOR_CONFIRM 拍确认），
# 复核完重新累积锁定。计数进 trace 的 gross_reanchor 列，可审计。
GROSS_LANE = 1.0
GROSS_HITS = 3
GROSS_WIN = 5
LINE_THETA_MAX = 0.35      # 线角（rad）；变道中车头相对道路可达 ~0.2，急变道 0.3+
LINE_RMS_MAX = 3.0         # 单线拟合残差（px，短段/坏拟合不参与位置原点）
# 强锁：曾用「双侧都在」的严格条件建立过线锁。此后单侧/一条线也继续作
# 绝对位置（k 已稳定，深度只会更差）；只要 RELOCK 或横位跳变才作废。
LINE_JUMP_RESET = 0.5      # 线横位单拍跳超此值（道）= 连续性中断，重新锁定
# 单线（只有一条 accepted）时的单拍横位步长封顶（道）：单条线的 L 误差会直接
# 写进 x_last，下一拍 k=round(x_last+L) 就可能整道错判（2026-11-12 局 p2
# idx316 起漂到 −3.4 道）。0.10 道/拍 = 2 道/s，仍高于实测最大横移 1.4 道/s。
LINE_SINGLE_STEP = 0.10
# 黄边界侧别硬约束：用最靠车头的几个点判画面左右侧，压中线留死区。
SIDE_NEAR_PTS = 5
SIDE_DEADBAND_PX = 20.0


@dataclass(frozen=True)
class LaneObsConfig:
    enabled: bool = False
    weights: str = ""
    hz: float = 10.0
    provider: str = "cpu"      # cpu=独立线程不抢 GPU 锁；dml=与深度共用锁
    threads: int = 4           # 仅 CPU：4 线程实测约 23ms/帧，仍在控制拍内
    detect_every: int = 1      # 每 N 帧做一次完整检测；1=逐拍检测，中间帧只预测
    model_input: tuple[int, int] = (384, 640)


@dataclass
class _Track:
    L: float
    color: str
    idx: int
    v: float = 0.0
    seen: int = 0
    k: int | None = None
    resid: float | None = None
    k_pending: int | None = None
    k_pending_n: int = 0


def _letterbox(rgb: np.ndarray) -> np.ndarray:
    import cv2
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def _prob_from_out(out) -> np.ndarray:
    logits = out[1][0][:, 12:372, :]
    e = np.exp(logits - logits.max(0, keepdims=True))
    return (e[1] / e.sum(0)).astype(np.float32)


def _lane_prob(sess, rgb: np.ndarray) -> np.ndarray:
    inp = sess.get_inputs()[0].name
    return _prob_from_out(sess.run(None, {inp: _letterbox(rgb)}))


def _row_segments(prob: np.ndarray) -> list[tuple[int, list[dict]]]:
    """逐行扫描概率图成段（步长 1）。

    2026-11-11 起步长 1（原 3）：远处分界虚线在图像里是被透视压扁的横条
    （概率图里高 2~5 行），步长 3 只命中 1~2 行，虚线轨迹永远凑不满拟合
    点数——实验区分解诊断的主死因（见 speedrush_lane_model 实验区）。"""
    rows = []
    for yc in range(206, 360):
        row = prob[yc]
        idx = np.flatnonzero(row > PROB_THRESH)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = row[seg]
            segs.append({"yc": yc, "xc": float((seg * p).sum() / p.sum()),
                         "p": float(p.mean())})
        if segs:
            rows.append((yc, segs))
    return rows


def _build_tracks(rows, tol: float = 13.0, max_gap: int = 10) -> list[list[dict]]:
    tracks: list[dict] = []
    for yc, segs in rows:
        used = set()
        for tr in tracks:
            tr["_pred"] = None
            if yc - tr["yc"] > max_gap:
                continue
            tr["_pred"] = tr["xc"] + tr.get("v", 0.0) * (yc - tr["yc"])
        for seg in segs:
            best, best_d = None, tol
            for j, tr in enumerate(tracks):
                if j in used or tr["_pred"] is None:
                    continue
                d = abs(seg["xc"] - tr["_pred"])
                if d < best_d:
                    best, best_d = j, d
            if best is None:
                tracks.append({"pts": [seg], "yc": yc, "xc": seg["xc"], "v": 0.0})
                used.add(len(tracks) - 1)
            else:
                tr = tracks[best]
                dv = (seg["xc"] - tr["xc"]) / max(1, yc - tr["yc"])
                tr["v"] = 0.7 * tr.get("v", 0.0) + 0.3 * float(np.clip(dv, -8, 8))
                tr["pts"].append(seg)
                tr["yc"], tr["xc"] = yc, seg["xc"]
                used.add(best)
    return [t["pts"] for t in tracks]


def _near_x(pts) -> float | None:
    """最靠车头的几个点的全幅像素横坐标中值（判左右侧用）。"""
    near = pts[-SIDE_NEAR_PTS:]
    if not near:
        return None
    return float(np.median([2.0 * q["xc"] for q in near]))


def _side_from_x(x: float | None) -> int | None:
    """画面左侧=-1、右侧=+1；压在中线死区内不给侧别。"""
    if x is None or abs(x - VPX) < SIDE_DEADBAND_PX:
        return None
    return -1 if x < VPX else 1


def _lane_cam_h(cam_h: float | None) -> tuple[float, bool]:
    """线几何用的相机高度：无值或反解明显失真时回退标定值。

    v5 起输入来自车道线间距反解（``cam_h_lane``），不再是深度平面 c——合成
    点图把贴地像素钉在 Y≡CAM_H_M，读 c 等于读常数；机位被打回追尾时线几何
    仍按保险杠高度换算，横位静默错 1~2 道。这里只挡反解失败的离谱值，合法
    机位（保险杠 1.25 / 追尾 1.9）都原样采用；单点反解噪声由调用方（module
    侧的近 5 拍中位）先滤一层，本函数不做二次平滑。
    """
    if cam_h is None or not (CAM_H_LO <= cam_h <= CAM_H_HI):
        return DEFAULT_CAM_H, True
    return float(cam_h), False


def _fit(pts, cam_h: float, y_lo: float, y_hi: float,
         focal: float | None = None):
    use = [q for q in pts if y_lo <= 2.0 * q["yc"] <= y_hi]
    if len(use) < 4:
        return None
    ys = np.array([2.0 * q["yc"] for q in use])
    xs = np.array([2.0 * q["xc"] for q in use])
    wt = np.array([max(0.1, q["p"]) for q in use])
    if ys.max() - ys.min() < 40:
        return None
    A = np.vstack([ys, np.ones_like(ys)]).T
    s, a = np.linalg.lstsq(A * wt[:, None], xs * wt, rcond=None)[0]
    L = float(s * cam_h / LANE_W)
    # 无有效焦距只丢线角（横位 L 不依赖焦距，仍可用）；不拿写死值冒充测量。
    theta = None
    if focal is not None and math.isfinite(focal) \
            and FOCAL_LO <= focal <= FOCAL_HI:
        theta = math.atan((VPX - a - L * LANE_W * Y_H / cam_h) / focal)
    rms = float(np.sqrt(np.average((xs - (s * ys + a)) ** 2, weights=wt)))
    return {"L": L, "theta": None if theta is None else float(theta), "rms": rms,
            "s": float(s), "a": float(a),
            "y0": float(ys.min()), "y1": float(ys.max()), "n": len(ys)}


# 双线夹住框心的最小像素间距（防近重合线插值发散；近场相邻车道线实测
# 数百 px，60px 只挡退化拟合）
MEMBERSHIP_SPAN_PX = 60.0
# 实测线距尺的道差下限：|ΔL| 更小的线对（近重合/重复道号）不参与定尺
SCALE_MIN_DL = 0.5


def _line_evals(y_b: float, lines: list[dict]) -> list[tuple[float, float]]:
    """框底行处各 accepted 线的像素位与横向量（夹逼与最近线回退共用）。

    接受门/残差门/线角门/拟合覆盖一倍跨度同口径：每条线带近/远两段拟合，
    吃覆盖 y_b 的那一段；越出数据范围允许外推不超过拟合自身跨度（线方程
    线性，外推一倍跨度 σ 至多翻倍，仍在近场 A1 量级 0.15~0.25 道；远车
    框底 ~390px 本就在两段范围之外，严格卡范围会把这门修成摆设）。"""
    evals: list[tuple[float, float]] = []
    for c in lines:
        if not c.get("accepted"):
            continue
        if c.get("rms") is not None and float(c["rms"]) > LINE_RMS_MAX:
            continue
        if abs(float(c.get("theta") or 0.0)) > LINE_THETA_MAX:
            continue
        best = None
        for ft in c.get("fits") or ():
            s, a = ft.get("s"), ft.get("a")
            y0, y1 = ft.get("y0"), ft.get("y1")
            if s is None or a is None or y0 is None or y1 is None:
                continue
            span = y1 - y0
            if not (y0 - span) <= y_b <= (y1 + span):
                continue               # 越出拟合范围一倍跨度：外推不可信
            d = max(y0 - y_b, y_b - y1, 0.0)   # 距数据范围的外推距离
            if best is None or d < best[0]:
                best = (d, s, a)
        if best is None:
            continue
        evals.append((best[1] * y_b + best[2], float(c["L"])))
    return evals


def lane_membership(cx: float, y_b: float, lines: list[dict]) -> float | None:
    """双线夹住框心（车道线定道）：两条 accepted 线在框底行的像素位置夹住
    框心时，按线间比例插值出车道成员（道，车心所在的车道坐标）。

    免 vpx/a_x/cam_h/Y_H 常数：线在 y_b 的像素位置来自各自的 2D 拟合
    （斜率/截距是测量值，不是标定常数），比例里的尺子全部消掉——免疫归一
    尺（A1，a_x 与线方程理论值差 ~19%）与 cam_h 的尺度争端。线不夹住框心、
    单线、线角过分/残差超限一律弃权（None）：消费端最坏回到 lane_side 粗
    方向，无退化路径。远场框底行（~380px）线几何收敛后夹逼系统性失效
    （102658_p1 实测 72 远车样本供货 4/72：不夹框心 44%、缺线 35%、线对
    退化 15%）——夹逼留给近场，远场由 lane_pos_nearest 回退顶上。"""
    evals = _line_evals(y_b, lines)
    if len(evals) < 2:
        return None
    evals.sort()
    for (x1, L1), (x2, L2) in zip(evals, evals[1:]):
        if x1 <= cx <= x2 and x2 - x1 >= MEMBERSHIP_SPAN_PX:
            return L1 + (cx - x1) / (x2 - x1) * (L2 - L1)
    return None


def lane_pos_nearest(cx: float, y_b: float, lines: list[dict],
                     cam_h: float | None = None) -> float | None:
    """最近线 + 米制尺度（2026-11-04 供货修正）：夹逼插值在远场框底行
    失效后的回退——框心相对最近 accepted 线的带符号像素偏移，按像素每道
    换成车道量 ``L_i + (cx − x_i)/像素每道``。

    尺子优先同帧实测线距（横向量不同的线对，中位像素间距 ÷ 道差，免
    vpx/Y_H/cam_h 常数）；缺对回退地面线几何（``LANE_W·(y_b−Y_H)/cam_h``，
    灭点/地平线 2026-11-04 真帧核对吻合、投影偏差中位 +5px）。不要求两线
    夹住框心：远场线距收敛到 ~76px/道后夹逼边界外就是同车道车，噪声一动
    出夹（失效形态见 lane_membership）。接受门/残差门/线角门/拟合覆盖与
    夹逼同口径（_line_evals）；地平线上（y_b ≤ Y_H）几何无效弃权。真帧
    验证（102658_p1，64 远车样本）：供货 64/64，车道量聚在自车 ±0.5 与
    邻道 ±1 附近；白车正前帧读 −0.14~−0.47 道（远场拟合噪声 ~±0.2 道内，
    够决策用）。"""
    if y_b <= Y_H:
        return None
    evals = _line_evals(y_b, lines)
    if not evals:
        return None
    scales = []
    for i in range(len(evals)):
        for j in range(i + 1, len(evals)):
            dL = abs(evals[j][1] - evals[i][1])
            if dL >= SCALE_MIN_DL:
                scales.append(abs(evals[j][0] - evals[i][0]) / dL)
    if scales:
        px_per_lane = float(np.median(scales))
    else:
        used_h, _fb = _lane_cam_h(cam_h)
        px_per_lane = LANE_W * (y_b - Y_H) / used_h
    x_i, L_i = min(evals, key=lambda e: abs(e[0] - cx))
    return L_i + (cx - x_i) / px_per_lane


def _lines_from_prob(prob: np.ndarray, cam_h: float,
                     focal: float | None) -> list[dict]:
    """概率图 → 线候选（无颜色匹配）。

    颜色不参与提取（2026-11-11 维护者叫停）：黄昏场景下白色分界线系统性
    过不了白门、甚至被误判成黄线，而几何门（残差/线角/等间距/离群）已
    足以筛野线；线字段恒 "?"，仅供显示兜底。虚线与实线同待遇，进
    LineTracker 同一套道号逻辑。"""
    lines = []
    for pts in _build_tracks(_row_segments(prob)):
        near = _fit(pts, cam_h, *NEAR_Y, focal)
        far = _fit(pts, cam_h, *FAR_Y, focal)
        f = near or far
        if f is None:
            continue
        x_near = _near_x(pts)
        # 双线同行插值用：每条线的像素位来自各自的 2D 拟合（s·y+a）。
        # 近/远两段拟合都带（框底行落在哪段就吃哪段，不外推出拟合范围）。
        fits = [{"s": ft["s"], "a": ft["a"],
                 "y0": ft["y0"], "y1": ft["y1"]}
                for ft in (near, far) if ft is not None]
        lines.append({"L": round(f["L"], 3),
                      "theta": None if f["theta"] is None
                      else round(f["theta"], 4),
                      "n": f["n"], "rms": round(f["rms"], 2),
                      "y0": int(f["y0"]), "y1": int(f["y1"]),
                      "color": "?",
                      "x_near": None if x_near is None
                      else round(x_near, 1),
                      "side": _side_from_x(x_near),
                      "fits": fits})
    return lines


# ---- HUD 距离徽章剔除（2026-11-12）------------------------------------------
# 微调权重（实验区 speedrush_lane_model，74 帧目检集）会把画面中央的 HUD 距离
# 徽章——红环 + **随机头像** + 下方「XX米」——画成一条竖直假线，整段复核实测连续
# 30+ 帧稳定存在，且位置正好落在贴脸真线的目标带内（x≈300 vs 真线 258~282），
# 消费端无法靠位置区分，所以必须在提取层剔掉。
#
# 为什么用「米」字做锚点：头像是随机的，整图模板跨会话必然失效（换一局掉到 0.4
# 以下）；红环检测会被车尾灯/广告牌干扰（562 帧只检出 6%）。而「米」是固定字体、
# 固定位置渲染：含徽章帧匹配得分 0.74~1.00、纵坐标恒为 0.890H，不含徽章帧只有
# 0.36~0.52 且纵坐标对不上。实测 0.43ms/帧（1280×720 CPU）。
#
# 为什么按线剔而不是清零像素：贴脸真线正好从徽章旁边穿过，清零会把真线一起挖掉。
# 规则：线的纵向支撑有 >=85% 的行落在徽章框内 → 只长在徽章上，丢弃。真线纵向跨
# 90+ 行、远超徽章高度，比例上不去。实测（微调权重、843 帧）：近带 669 条线剔除
# 51 条（7.6%），其中误剔真线 2 条（0.3%）。
HUD_BADGE_GATE = True          # 置 False 即整道门回退（A/B 隔离用）
HUD_BADGE_TPL_REL = "resources/policy/hud_badge_mi.png"
HUD_BADGE_SCORE_MIN = 0.70     # 「米」字匹配下限（含徽章最低实测 0.745）
HUD_BADGE_ROW_TOL = 3.0        # 「米」字纵坐标容差（固定 0.890H）
HUD_BADGE_INSIDE_MAX = 0.85    # 线支撑落在徽章框内的比例上限
_HUD_BADGE_TPL: dict = {"tried": False, "tpl": None}


def _hud_badge_template():
    """懒加载「米」字模板；缺失时返回 None，整道门静默跳过（行为与旧版一致）。"""
    if not _HUD_BADGE_TPL["tried"]:
        _HUD_BADGE_TPL["tried"] = True
        try:
            import cv2
            path = Path(__file__).resolve().parent / HUD_BADGE_TPL_REL
            tpl = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if tpl is not None:
                _HUD_BADGE_TPL["tpl"] = tpl.astype(np.float32)
        except Exception:
            _HUD_BADGE_TPL["tpl"] = None
    return _HUD_BADGE_TPL["tpl"]


def _hud_badge_box(frame: np.ndarray):
    """定位 HUD 距离徽章，返回全幅像素框 (x0, y0, x1, y1)；无徽章返回 None。

    用通道均值做灰度：调用方传 RGB 还是 BGR 都不影响结果（本模块上游口径
    历史上变过一次，不再依赖通道顺序）。
    """
    tpl = _hud_badge_template()
    if tpl is None or frame is None or getattr(frame, "ndim", 0) != 3:
        return None
    import cv2
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.855), int(h * 0.930)
    x0, x1 = int(w * 0.28), int(w * 0.66)
    # 先裁 ROI 再做通道均值：全图浮点均值实测要 12.9ms，裁到 155×50 后 0.7ms。
    roi = frame[y0:y1, x0:x1].astype(np.float32).mean(axis=2)
    th, tw = tpl.shape[:2]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return None
    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < HUD_BADGE_SCORE_MIN:
        return None
    mi_y = loc[1] + y0 + th / 2.0
    if abs(mi_y - h * 0.890) > HUD_BADGE_ROW_TOL:
        return None
    mi_x = loc[0] + x0 + tw / 2.0
    # 「米」在徽章下方：徽章中心约 (米x-6, 米y-24)、半径约 21；框向下含箭头。
    cx, cy, rad = mi_x - 6.0, mi_y - 24.0, 21.0
    return (cx - rad - 6.0, cy - rad - 4.0, cx + rad + 6.0, mi_y + 44.0)


def _line_in_badge(line: dict, box) -> bool:
    """这条线是否只长在徽章上：纵向支撑 >=HUD_BADGE_INSIDE_MAX 落在徽章框内。

    只看**实际采用的那段拟合**（``fits[0]``，即 `_lines_from_prob` 里
    ``f = near or far`` 选中的那段）：近远两段都算会把远段的行当成「框外」，
    让徽章假线的比例被稀释（f0204 实测因此漏剔）。
    """
    if box is None:
        return False
    fits = line.get("fits") or ()
    if not fits:
        return False
    x0, y0, x1, y1 = box
    # 框是模板反推的，边界有 2~4px 误差；不留容差会让「首行差半像素」把比例从
    # 1.0 拉到 0.83（f0204 实测），刚好卡在门限下面漏剔。
    t = 4.0
    x0, y0, x1, y1 = x0 - t, y0 - t, x1 + t, y1 + t
    ft = fits[0]
    s, a = float(ft["s"]), float(ft["a"])
    lo, hi = float(ft["y0"]), float(ft["y1"])
    if hi - lo < 20.0:
        return False
    total = inside = 0
    for yy in np.arange(lo, hi + 1.0, 8.0):
        xx = s * yy + a
        total += 1
        if x0 <= xx <= x1 and y0 <= yy <= y1:
            inside += 1
    if total < 3:
        return False
    return inside / total >= HUD_BADGE_INSIDE_MAX


def _drop_badge_lines(lines: list[dict], frame: np.ndarray) -> list[dict]:
    """剔除只长在 HUD 距离徽章上的假线；模板缺失或本帧无徽章时原样返回。"""
    if not HUD_BADGE_GATE or not lines:
        return lines
    box = _hud_badge_box(frame)
    if box is None:
        return lines
    return [ln for ln in lines if not _line_in_badge(ln, box)]


def _vanish_row(lines: list[dict]) -> float | None:
    """线候选 → 逐帧灭点行（地平线）。每条线取近段拟合（缺近段回退远段），
    两两交点过三级门后取中位；证据不足返回 None（调用方保留上一值/回退标定）。

    这是合成点云的地平线来源：相对深度是视差向仿射不变量，地面模型里 y_h 与
    偏移项共线，**深度本身给不出地平线**（y_h 扫值残差恒定，2026-11-11 探针）。"""
    fits = []
    for c in lines:
        fts = c.get("fits") or ()
        if not fts:
            continue
        ft = fts[0]                     # (near, far)：近段优先
        s, a = ft.get("s"), ft.get("a")
        if s is None or a is None:
            continue
        fits.append((float(s), float(a)))
    if len(fits) < 2:
        return None
    yvs = []
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            s1, a1 = fits[i]
            s2, a2 = fits[j]
            if abs(s1 - s2) < 5e-3:
                continue
            yv = (a2 - a1) / (s1 - s2)
            if not (VANISH_Y_LO <= yv <= VANISH_Y_HI):
                continue
            d = abs((s1 - s2) * VANISH_PROBE_Y + (a1 - a2))
            if not (VANISH_SPAN_LO <= d <= VANISH_SPAN_HI):
                continue
            yvs.append(float(yv))
    if not yvs:
        return None
    med = float(np.median(yvs))
    mad = float(np.median(np.abs(np.asarray(yvs) - med)) * 1.4826)
    keep = [y for y in yvs if abs(y - med) <= 2.5 * mad] if mad > 0.5 else yvs
    return float(np.median(keep))


def _smooth_vanish(hist: deque, raw: float, now: float, last_t: float
                   ) -> tuple[float, float]:
    """单帧灭点行 → 发布值（近 YV_WIN 个检测拍的中位）。返回 (发布值, 入史时刻)。

    长间隔（>YV_GAP_S）先清史：中间断过检就说明这段的俯仰证据不连续，拿旧
    样本掺进中位会把一次真实的姿态变化当成噪声压掉。单帧离群由中位天然吸收，
    不需要额外的跳变门（2 个样本时取中位=均值，余量另由物理门 341~381 兜）。"""
    if hist and now - last_t > YV_GAP_S:
        hist.clear()
    hist.append(float(raw))
    return float(np.median(hist)), now


def _cam_h_from_lines(lines: list[dict], y_v: float | None) -> float | None:
    """车道线反解相机高（像素间距法，与深度链完全无关）。

    地面线满足 ``x = vpx + L·LANE_W·(y−y_v)/cam_h``，两条线在同一行的像素间距
    ``Δx = |Δk|·LANE_W·(y−y_v)/cam_h``，于是 ``cam_h = |Δk|·LANE_W·(y−y_v)/Δx``。
    只用 accepted 且已定道号（``k``）的线；每对取两段拟合的行窗交集的中点行，
    两两结果取中位（≥3 个时先按 MAD 剔一次）。

    这是视角守卫的**唯一诚实判据**：合成点图把贴地像素的高度钉在 Y≡cam_h，平面
    系数 c 恒等于 1.25，读它等于读一个常数（2026-11-12 实机 18 帧逐帧核对）。
    证据不足（<2 条可用线 / 行窗不重叠 / 间距过小）返回 None——调用方按「无判据」
    处理，不盲判。"""
    if y_v is None:
        return None
    fs: list[tuple[int, float, float, float, float]] = []
    for c in lines:
        if not c.get("accepted"):
            continue
        k = c.get("k")
        fts = c.get("fits") or ()
        if k is None or not fts:
            continue
        ft = fts[0]                     # (near, far)：近段优先，行窗更贴近车
        s, a = ft.get("s"), ft.get("a")
        y0, y1 = ft.get("y0"), ft.get("y1")
        if s is None or a is None or y0 is None or y1 is None:
            continue
        fs.append((int(k), float(s), float(a), float(y0), float(y1)))
    if len(fs) < 2:
        return None
    est: list[float] = []
    for i in range(len(fs)):
        for j in range(i + 1, len(fs)):
            k1, s1, a1, lo1, hi1 = fs[i]
            k2, s2, a2, lo2, hi2 = fs[j]
            dk = abs(k1 - k2)
            if dk < 1:
                continue            # 同一条道（两段拟合重叠/碎片）
            lo = max(lo1, lo2, y_v + 20.0)
            hi = min(hi1, hi2)
            if hi - lo < CAM_H_EST_MIN_ROWS:
                continue
            y = 0.5 * (lo + hi)
            dx = abs((s1 - s2) * y + (a1 - a2))
            if dx < CAM_H_EST_MIN_DX:
                continue
            h = dk * LANE_W * (y - y_v) / dx
            if CAM_H_EST_LO <= h <= CAM_H_EST_HI:
                est.append(float(h))
    if not est:
        return None
    med = float(np.median(est))
    if len(est) >= 3:
        mad = float(np.median(np.abs(np.asarray(est) - med)) * 1.4826)
        if mad > 0.02:
            kept = [v for v in est if abs(v - med) <= 2.5 * mad]
            if kept:
                med = float(np.median(kept))
    return med


class LineTracker:
    """线观测状态机：等间距约束 + 深度路心定整数道号（2026-10-24 重写）。

    颜色不再是道号身份。跨帧只按拟合横位 L 关联；每条线当前帧的整数道号由
    **深度路心**给出：``k = round(anchor + L)``，残差 ``r = anchor + L - k``
    是这条线的子道偏差。多条线取残差中值还原自车横位（等间距由整数分配 +
    残差门共同保证），重复道号/离群线淘汰。深度空窗时锚点退回上一拍横位
    （模型/线），线只做子道细化，不再自己漂绝对位；大跳变需连续
    ``ANCHOR_CONFIRM`` 拍同向一致才换锚。
    """

    ANCHOR_AGREE_TOL = 0.5
    ANCHOR_CONFIRM = 2
    RESID_MAX = 0.45
    # 多线共同证实锚点时的收紧门：0.5 道级的一拍深度噪声不能同时拟合进
    # 两根线，避免整道翻转；超过就退回单线两拍确认。
    STRONG_RESID = 0.30
    OUTLIER_TOL = 0.35
    SPREAD_MAX = 0.55

    def __init__(self, off_max: float = OFF_MAX) -> None:
        self.OFF_MAX = float(off_max)
        self.tracks: list[_Track] = []
        self.state = "RELOCK"
        self.x_last: float | None = None
        self.x_prev: float | None = None
        self.x_vel = 0.0
        self.lost = 0
        self.locked = 0            # 连续 TRACK 稳定性拍数（线为主的门）
        self.lock_strong = False   # 曾以双侧严格条件建锁（此后单侧也作原点）
        self.grid_reject = 0
        self.side_reject = 0
        self.gross_hist: list[bool] = []
        self.gross_reanchor = 0    # 整道级复核触发次数（trace 审计列）
        self.last_theta: float | None = None
        self.last_lines: list[dict] = []
        self.anchor_src = "none"

    @staticmethod
    def _side_bad(c: dict) -> bool:
        """黄线拟合符号与画面侧别矛盾：这条线不能当绝对参照。"""
        side = c.get("side")
        return (c.get("color") == "y" and side in (-1, 1)
                and ((side < 0 and float(c["L"]) > 0)
                     or (side > 0 and float(c["L"]) < 0)))

    def _neighbor_k(self, c: dict, fid: int) -> int | None:
        """无深度锚时用所有在期线投票给新线定道号；有分歧就不定。

        只看最近一条邻居时，长空窗里那条邻居自己漂了一道，新线就会跟着
        错一道，并一直保持到下一次深度重锚。等间距下每条在期线都能独立给
        出一票，全部同票才采用。
        """
        L = float(c["L"])
        votes: list[int] = []
        for t in self.tracks:
            if t.k is None:
                continue
            gap_now = fid - t.idx
            if gap_now > TRACK_GAP:
                continue
            gap = L - (t.L + t.v * gap_now)
            if abs(gap - round(gap)) > self.RESID_MAX:
                continue
            votes.append(int(t.k + int(round(gap))))
        if not votes:
            return None
        if any(v != votes[0] for v in votes[1:]):
            return None
        return votes[0]

    def _assign_k(self, c: dict, x_anchor: float | None,
                  fid: int, strong_anchor: bool = False,
                  grid_frozen: bool = False) -> bool:
        """给一条线定/重锚持久道号；False=本拍不能参选横位。

        网格已冻结时（``grid_frozen``：线已连续锁定且未 RELOCK）：道号只跟
        持久轨迹，新线按**网格自身横位**接号（``k=round(x_last+L)``，残差门
        拒半道）——不靠邻居相对票，也不能整道改判。实测（164953 局 p2）：新线
        靠邻居票拿号会随线组换新整道偏移；但 170923 局把新线一律拒收后可用率
        从 45% 掉到 18%、盲窗拉长到 8s，执行层在盲窗里按旧转杆把车带过整条路。
        接号只接现有网格，不重定网格，覆盖与身份两不误。

        网格未冻结（冷启/重锁）：深度新鲜拍是重锚来源——新线直接初始化 k；
        老线与锚点冲突时，多条线同拍支持新网格（``strong_anchor``）就立即改
        k，否则连续 ``ANCHOR_CONFIRM`` 拍同向一致才改，未确认前本拍横位用深度
        兜底。深度空窗时保留持久 k（线随画面移动继续给绝对横位），新线先由
        所有在期线的整数间距票定号；票分歧或没有在期线时，再退回网格横位接号
        （重锁后深度仍断供也能靠上一横位重新上线，不再永久卡 RELOCK）。没通过
        门的新线不留 k，被拒线也不更新已有关联轨迹的身份，避免野线把错道号传给
        下一帧。
        """
        L = float(c["L"])
        tk = c.get("_track")
        if grid_frozen:
            if tk is not None and tk.k is not None:
                c["k"] = tk.k
                return True
            return self._assign_k_from_grid(c, L)
        if x_anchor is not None:
            k_a = int(round(x_anchor + L))
            resid = x_anchor + L - k_a
            if tk is None or tk.k is None:
                if abs(resid) > self.RESID_MAX:
                    c["reject"] = "resid"
                    self.grid_reject += 1
                    return False
                c["k"] = k_a
                return True
            if k_a == tk.k:
                tk.k_pending = None
                tk.k_pending_n = 0
                c["k"] = tk.k
                return True
            if tk.k_pending == k_a:
                tk.k_pending_n += 1
            else:
                tk.k_pending, tk.k_pending_n = k_a, 1
            if tk.k_pending_n >= self.ANCHOR_CONFIRM or strong_anchor:
                # 多线同拍支持这个锚点：网格已被线自身证实，不必再等第二拍；
                # 单线锚点仍等连续确认，防一拍深度噪声翻道。
                tk.k = k_a
                tk.k_pending = None
                tk.k_pending_n = 0
                c["k"] = k_a
                return True
            c["k"] = tk.k
            c["reanchor"] = True
            return False
        if tk is not None and tk.k is not None:
            c["k"] = tk.k
            return True
        k = self._neighbor_k(c, fid)
        if k is not None:
            c["k"] = k
            return True
        return self._assign_k_from_grid(c, L)

    def _assign_k_from_grid(self, c: dict, L: float) -> bool:
        """用网格自身的横位给新线接号（``k=round(x_last+L)``，残差门拒半道）。

        x_last 是锁定的线横位或短盲窗外推值，不是深度读数：这里只把新线接到
        现有网格上，不重定网格。没有在期线可投票时（长盲窗后轨迹已过期）也走
        这条，避免 RELOCK 在深度断供时永久卡死——170923 局 p2 曾整段 8s 无线
        可用。车在盲窗里真的移动了，残差门会让新线继续无号，等深度重锚。"""
        if self.x_last is None:
            return False
        k_a = int(round(self.x_last + L))
        if abs(self.x_last + L - k_a) > self.RESID_MAX:
            return False
        c["k"] = k_a
        return True

    def update(self, lines: list[dict], fid: int, dt: float,
               v_lat: float = 0.0, fid_jump: bool = False,
               anchor: float | None = None) -> dict:
        # 线锁：上一拍起线已连续稳定 LINE_LOCK_TICKS 拍 → 本拍道号为权威，
        # 深度锚只能用于「线丢失后的重建」，不能改判/否决。冻结覆盖短 COAST
        # （locked 保留、未 RELOCK）：断一拍不代表身份可改，只有跳变/RELOCK
        # 才重置 locked。
        grid_frozen = (self.locked >= LINE_LOCK_TICKS
                       and self.state != "RELOCK")
        # ① 关联：颜色只作软提示，横位 L 才是身份
        cur = []
        for d in lines:
            c = dict(d)
            for key in ("k", "resid", "accepted", "reject", "_init_k",
                        "_xi", "reanchor"):
                c.pop(key, None)
            c["k"], c["resid"], c["accepted"] = None, None, False
            cur.append(c)
        for c in cur:
            best, bd = None, 1e9
            for t in self.tracks:
                gap = fid - t.idx
                if gap > TRACK_GAP:
                    continue
                pred = t.L + t.v * gap
                d = abs(c["L"] - pred)
                if (t.color != "?" and c.get("color") != "?"
                        and t.color != c.get("color")):
                    d += 0.10          # 换色不拆线：只加距离惩罚
                if d < TRACK_TOL + 0.05 * min(gap, TRACK_GAP) and d < bd:
                    best, bd = t, d
            if best is not None:
                gap = max(1, fid - best.idx)
                dv = (c["L"] - best.L) / gap
                c["v"] = float(np.clip(
                    0.7 * best.v + 0.3 * np.clip(dv, -0.15, 0.15),
                    -0.15, 0.15))
                c["seen"] = best.seen + 1
                c["_track"] = best
            else:
                c["v"], c["seen"] = 0.0, 0

        # ② 锚点：只认新鲜深度；空窗靠持久 k 的线继续给绝对横位
        x_anchor = None
        if anchor is not None and math.isfinite(anchor) \
                and abs(anchor) <= self.OFF_MAX:
            x_anchor = float(anchor)
        self.anchor_src = ("depth" if x_anchor is not None
                           else ("lines" if self.x_last is not None
                                 else "none"))
        if x_anchor is None and self.x_last is None:
            self.lost += 1
            self.locked = 0
            self.lock_strong = False
            self.state = "RELOCK"
            self._commit_tracks(cur, fid)
            return self._out(None, 0, cur, self.anchor_src,
                             relock_src=None, pred=False)

        # ②b 整道级复核：锁定期深度不许改判道号，但"错得离谱"要能纠正。
        # 只在**新鲜**锚上累积（复用拍是同一份旧证据，不重复计数），且要求
        # 连续差超一整道——半道级分歧是常态噪声（实测路心相位 p50 0.15 道、
        # p90 0.43 道），拿它复核会把网格摇散。触发后解冻一次，道号仍走
        # ③ 的正常重锚流程（每线 ANCHOR_CONFIRM 拍确认），复核完重新累积锁定。
        if x_anchor is not None and self.x_last is not None:
            self.gross_hist.append(
                abs(x_anchor - self.x_last) > GROSS_LANE)
            del self.gross_hist[:-GROSS_WIN]
            if (grid_frozen and len(self.gross_hist) >= GROSS_WIN
                    and sum(self.gross_hist) >= GROSS_HITS):
                # 一次性整道修正：只挪道号（整数道），不重置锁定、不留解冻窗。
                # 解冻窗里每根线各自重锚会连着跳好几拍（离线回放：每次触发
                # 带出 3~4 次横位跳），而整道错判只需要挪一次整数。
                delta = int(round(x_anchor - self.x_last))
                if delta:
                    for tk in self.tracks:
                        if tk.k is not None:
                            tk.k += delta
                    self.x_last += delta
                    self.gross_reanchor += 1
                self.gross_hist.clear()

        # ③ 颜色/侧别质量门 + 持久道号分配/重锚
        strong_anchor = False
        if not grid_frozen and x_anchor is not None and self.x_last is not None \
                and abs(x_anchor - self.x_last) <= self.ANCHOR_AGREE_TOL:
            # 只有离当前横位不远的锚点才享受当拍重锚：整道级深度跳变时
            # 两根线会整齐地支持错误网格（残差照样很小），必须仍走两拍确认。
            ks = set()
            for c in cur:
                if self._side_bad(c):
                    continue
                L = float(c["L"])
                k_a = int(round(x_anchor + L))
                if abs(x_anchor + L - k_a) <= self.STRONG_RESID:
                    ks.add(k_a)
            strong_anchor = len(ks) >= 2
        cand = []
        for c in cur:
            L = float(c["L"])
            if self._side_bad(c):
                c["reject"] = "side"
                self.side_reject += 1
                continue
            if not self._assign_k(c, x_anchor, fid, strong_anchor,
                                  grid_frozen):
                continue
            xi = float(c["k"]) - L
            c["_xi"] = xi
            if not grid_frozen and x_anchor is not None \
                    and abs(xi - x_anchor) > self.ANCHOR_AGREE_TOL:
                # 持久 k 与新鲜深度冲突：本拍用深度兜底，线不参选
                c["reject"] = "anchor"
                self.grid_reject += 1
                continue
            cand.append(c)

        # ④ 同 k 重复：留点数多的（等间距下同一道不应有两条线）
        byk: dict[int, dict] = {}
        for c in cand:
            k = c["k"]
            if k not in byk:
                byk[k] = c
            elif int(c.get("n") or 0) > int(byk[k].get("n") or 0):
                byk[k]["reject"] = "dup"
                self.grid_reject += 1
                byk[k] = c
            else:
                c["reject"] = "dup"
                self.grid_reject += 1
        keep = list(byk.values())

        # ⑤ 离群：相对横位中值偏超 OUTLIER_TOL 的线不参选
        if len(keep) >= 2:
            med = float(np.median([c["_xi"] for c in keep]))
            inl = [c for c in keep if abs(c["_xi"] - med)
                   <= self.OUTLIER_TOL]
            for c in keep:
                if c not in inl:
                    c["reject"] = "outlier"
                    self.grid_reject += 1
            keep = inl
            if keep and max(c["_xi"] for c in keep) \
                    - min(c["_xi"] for c in keep) > self.SPREAD_MAX:
                best = min(keep, key=lambda c: abs(c["_xi"] - med))
                for c in keep:
                    if c is not best:
                        c["reject"] = "spread"
                        self.grid_reject += 1
                keep = [best]

        # ⑥ 输出：有可用线=线横位；无可用线但深度新鲜=跟深度；否则滑行
        if keep:
            for c in keep:
                c["accepted"] = True
            x_raw = float(np.median([c["_xi"] for c in keep]))
            if len(keep) < 2 and self.x_last is not None \
                    and x_anchor is None:
                # 单线只做子道细化，单拍步长封顶（2026-11-12 实机 p2 定案）：
                # 一条线的 L 误差会直接写进 x_last，下一拍 k=round(x_last+L)
                # 就可能整道错判，错判后的 x 又反过来喂 k——闭环正反馈。
                # 实测 220717 局 p2 idx316：线质量崩到只剩一条 accepted，L 从
                # −0.52 抖到 −0.32，x 跳 0.19；随后 k 从 {−1,0} 变 {−1,−2}、
                # 再到 {−5,−4,−3}，横位漂到 −3.4（同拍真实 ro 只有 −0.4），
                # 决策目标整道错位。该局单线拍的横位跳变率 18%，双线 2.4%。
                # 封顶不冻结：单侧线仍能做子道细化（深度空窗靠它保持连续）。
                d = float(np.clip(x_raw - self.x_last,
                                  -LINE_SINGLE_STEP, LINE_SINGLE_STEP))
                x_out = self.x_last + d
            else:
                x_out = x_raw
            # 稳定性计数：只有横位连续（单拍不跳 >LINE_JUMP_RESET）才累加；
            # 跳变=连续性中断，从 1 重新数（线锁的要件）。
            step = (abs(x_out - self.x_last)
                    if self.x_last is not None else 0.0)
            if step > LINE_JUMP_RESET:
                self.lock_strong = False      # 横位跳变=换线/换帧，强锁作废
            self.locked = (self.locked + 1
                           if step <= LINE_JUMP_RESET else 1)
            if self.x_prev is not None and dt > 1e-3:
                assert self.x_last is not None   # x_prev 就是上一拍的 x_last
                dvx = float(np.clip((x_out - self.x_last) / dt, -1.5, 1.5))
                self.x_vel = 0.6 * self.x_vel + 0.4 * dvx
            self.x_prev, self.x_last = self.x_last, x_out
            self.lost = 0
            self.state = "TRACK"
            ths = [(float(c["theta"]), max(1, int(c.get("n") or 1)))
                   for c in keep if c.get("theta") is not None]
            if ths:
                w = sum(w for _, w in ths)
                self.last_theta = sum(t * w for t, w in ths) / w
            relock_src = "line"
            pred = False
        elif x_anchor is not None:
            if self.x_prev is not None and self.x_last is not None \
                    and dt > 1e-3:
                dvx = float(np.clip((x_anchor - self.x_last) / dt,
                                    -1.5, 1.5))
                self.x_vel = 0.6 * self.x_vel + 0.4 * dvx
            self.x_prev, self.x_last = self.x_last, x_anchor
            self.lost += 1
            # 短暂 COAST 保留线锁：轨迹的 k 还在（TRACK_GAP 内），一两拍线
            # 不合格就清零会让原点在「线/深度」之间来回切，正是残留整道跳
            # 的来源。k 若在空窗里被深度改判，回来的横位跳变会作废强锁。
            self.state = ("RELOCK" if (fid_jump
                                       or self.lost > COAST_MAX_TICKS)
                          else "COAST")
            if self.state == "RELOCK":
                self.locked = 0
                self.lock_strong = False
            x_out = x_anchor
            relock_src = None
            pred = False
        else:
            self.lost += 1
            # 盲窗封顶（2026-10-31）：线不可用又没有深度锚时，横位只剩「上次
            # 横速」外推。短断链（≤COAST_MAX_TICKS）这么走是对的；再长就没有
            # 可积的运动证据了——旧行为会以恒速一路外推到整道外（实测两秒
            # 飘 2 道后随新鲜证据整道跳回）。超过即冻住：位置保持、状态标
            # RELOCK，让上游知道这是外推值而不是测量值。
            # 锁/强锁与有锚空窗同一口径：断一拍不清零，只有跳帧或外推预算
            # 耗尽才作废——否则恢复拍的新线会立刻按相对票补号，身份在断链
            # 前后被悄悄改写（164953 局 p2 的整道漂移）。
            # 无深度锚且无历史横位的拍已在 ② 提前返回，本分支 x_last 必有值
            assert self.x_last is not None
            if self.lost <= COAST_MAX_TICKS and not fid_jump:
                vp = 0.5 * self.x_vel + 0.5 * float(v_lat)
                self.x_last = (self.x_last + float(np.clip(vp, -1.5, 1.5))
                               * dt)
                self.state = "COAST"
            else:
                self.locked = 0
                self.lock_strong = False
                self.state = "RELOCK"
            x_out = self.x_last
            relock_src = None
            pred = True
        # 线残差统一按最终横位记（accepted 线应接近 0；被拒线留偏差可审计）
        for c in cur:
            xi = c.get("_xi")
            if xi is not None:
                c["resid"] = round(float(xi) - x_out, 3)
        self._commit_tracks(cur, fid)
        return self._out(x_out, len(keep), cur, self.anchor_src,
                         relock_src=relock_src, pred=pred)

    def _commit_tracks(self, cur: list[dict], fid: int) -> None:
        for c in cur:
            c.pop("_init_k", None)
            c.pop("_xi", None)
            tk = c.pop("_track", None)
            if tk is not None:
                # 没通过门的线不更新身份：野线被关联到真线上时若照写 L/k，
                # 下一帧真线会接上这条被带偏的轨迹，继承错道号。
                if c.get("accepted"):
                    tk.L, tk.color, tk.idx = c["L"], c.get("color", "?"), fid
                    tk.seen, tk.v = c["seen"], c["v"]
                    tk.k, tk.resid = c.get("k"), c.get("resid")
            else:
                # 新线本拍没通过门（离群/跨度/残差）就不留道号：它可能是一条
                # 野线，下一帧真线接上这条轨迹时会继承错 k，连带整对线偏一道。
                k = c.get("k") if c.get("accepted") else None
                self.tracks.append(_Track(
                    c["L"], c.get("color", "?"), fid, c["v"], 0,
                    k, c.get("resid")))
        self.tracks = [t for t in self.tracks if fid - t.idx <= TRACK_GAP]

    def _out(self, x, n_locked, lines, anchor_src,
             relock_src, pred) -> dict:
        if x is not None:
            x = round(float(x), 3)
        self.last_lines = [dict(c) for c in lines]
        # 位置原点可用性（严格）：线连续锁定 + 至少两条高质量线 + 左右两侧
        # 都在 + 线角不过分。上游据此把线当绝对位置；不满足时深度才重建。
        good = [c for c in lines if c.get("accepted")
                and (c.get("rms") is None
                     or float(c["rms"]) <= LINE_RMS_MAX)
                and abs(float(c.get("theta") or 0.0)) <= LINE_THETA_MAX]
        sides = {c.get("side") for c in good}
        both = len(good) >= 2 and sides == {-1, 1}
        if self.state == "TRACK" and self.locked >= LINE_LOCK_TICKS and both:
            # 双侧严格条件建立了强锁：此后单侧/一条线也继续作绝对位置
            self.lock_strong = True
        usable = (self.state == "TRACK" and self.locked >= LINE_LOCK_TICKS
                  and bool(good) and (both or self.lock_strong))
        return {"state": self.state, "x": x, "theta": self.last_theta,
                "gross_reanchor": int(self.gross_reanchor),
                "n_locked": int(n_locked), "relock_src": relock_src,
                "n_lines": len(lines), "grid_reject": self.grid_reject,
                "side_reject": self.side_reject, "lost": int(self.lost),
                "anchor_src": anchor_src, "lines": lines, "pred": pred,
                "usable": bool(usable), "locked": int(self.locked)}

    def predict(self, dt: float, v_lat: float = 0.0) -> dict:
        """非检测帧：只推进横位；上一帧线保留但标 pred，不再参与绝对位。"""
        if self.x_last is not None:
            self.lost += 1
            # 与 update 的盲窗同一口径：超过 COAST_MAX_TICKS 的外推不再积分
            if self.lost <= COAST_MAX_TICKS:
                vp = 0.5 * self.x_vel + 0.5 * float(v_lat)
                self.x_last += float(np.clip(vp, -1.5, 1.5)) * dt
            self.state = "COAST"
            return {"state": self.state, "x": round(self.x_last, 3),
                    "theta": self.last_theta, "n_locked": 0,
                    "relock_src": None, "n_lines": 0,
                    "grid_reject": self.grid_reject,
                    "side_reject": self.side_reject,
                    "lost": int(self.lost),
                    "anchor_src": self.anchor_src,
                    "usable": False, "locked": int(self.locked),
                    "lines": [dict(c) for c in self.last_lines], "pred": True}
        return {"state": "RELOCK", "x": None, "theta": self.last_theta,
                "n_locked": 0, "relock_src": None, "n_lines": 0,
                "grid_reject": self.grid_reject,
                "side_reject": self.side_reject, "lost": int(self.lost),
                "anchor_src": "none", "usable": False,
                "locked": int(self.locked),
                "lines": [], "pred": True}


class LaneObsWorker:
    """后台线观测线程；`latest()` 只读，失败静默返回 None。"""

    def __init__(self, cfg: LaneObsConfig, off_max: float = OFF_MAX) -> None:
        self.cfg = cfg
        self._q: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._last_det_t: float | None = None
        self._last_det_fid: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sess = None
        self._tracker = LineTracker(off_max)
        self._last_fid = -1
        self._last_t = 0.0
        self._n = 0
        self._use_dml = False
        self._focal: float | None = None   # 逐帧全幅焦距（深度读数同源）
        self._yv: float | None = None      # 灭点行（地平线）·发布值=近 N 个检测拍中位
        self._yv_raw: float | None = None  # 最近一次检测拍的单帧估计（未平滑，诊断用）
        self._yv_hist: deque = deque(maxlen=YV_WIN)
        self._yv_t: float = 0.0            # 上一次入史时刻（长间隔后清史，见 _push_vanish）
        self.busy_skips = 0
        self.hold_ms = 0.0
        self.cam_h_fb = 0
        # 节拍记账：真实检测频率=det_n/时长，不受推送节流口径误导
        self.det_n = 0
        self.pred_n = 0
        self.push_n = 0
        self.drop_n = 0
        self.error: str | None = None
        self.note: str | None = None
        # 可选模型包未安装（不是故障）：模块按 INFO 记录，不按 WARNING 刷屏
        self.optional_missing = False

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        if not self.cfg.weights:
            self.error = "未配置线观测权重"
            return
        raw = Path(self.cfg.weights)
        if raw.is_absolute():
            weights: Path | None = raw
            if not weights.is_file():
                self.error = f"权重缺失：{weights}"
                self.optional_missing = True
                return
        else:
            weights, err = model_assets.select(self.cfg.weights)
            if weights is None:
                self.error = err
                self.optional_missing = bool(err and "缺少" in err)
                return
        try:
            import onnxruntime as ort
            avail = set(ort.get_available_providers())
            if self.cfg.provider == "dml":
                if "DmlExecutionProvider" in avail:
                    providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
                else:
                    providers = ["CPUExecutionProvider"]
                    self.note = "DML 不可用，影子回退 CPU"
            elif self.cfg.provider == "cpu":
                providers = ["CPUExecutionProvider"]
            else:
                self.error = f"未知 provider：{self.cfg.provider}"
                return
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(self.cfg.threads)
            so.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(
                str(weights), sess_options=so, providers=providers)
            self._use_dml = providers[0] == "DmlExecutionProvider"
        except Exception as exc:  # noqa: BLE001 —— 影子故障不碰控制
            self.error = repr(exc)
            return
        self._thread = threading.Thread(target=self._loop, name="lane-obs",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def push(self, rgb: np.ndarray, fid: int, cam_h: float | None,
             v_lat: float, dt: float, fid_jump: bool,
             focal: float | None = None,
             anchor: float | None = None) -> None:
        """入队一帧；``focal`` 是同帧深度读数的全幅焦距（线角用），``anchor``
        是同帧新鲜深度路心（None=复用/缺供）。

        两者都随帧一起入队，保证线观测用的绝对参照/角度尺度与这条帧同拍——
        不拿后一帧的深度去锚或定标前一帧的线。"""
        if self._sess is None:
            return
        now = time.perf_counter()
        if self.cfg.hz > 0 and now - self._last_t < 1.0 / self.cfg.hz:
            return
        self._last_t = now
        self.push_n += 1
        used_h, fb = _lane_cam_h(cam_h)
        if fb:
            self.cam_h_fb += 1
        try:
            self._q.get_nowait()
            self.drop_n += 1
        except queue.Empty:
            pass
        try:
            self._q.put_nowait((rgb, int(fid), used_h,
                                float(v_lat), float(dt), bool(fid_jump),
                                None if focal is None else float(focal),
                                None if anchor is None else float(anchor)))
        except queue.Full:
            pass

    def _set_focal(self, focal: float | None) -> None:
        """逐帧焦距：有效值轻平滑；缺值/异常保持上一次有效估计。

        焦距只影响线角（横位不依赖它）；用上次的**测量**值补缺，不退回常量。"""
        if focal is None or not math.isfinite(focal) \
                or not (FOCAL_LO <= focal <= FOCAL_HI):
            return
        self._focal = (float(focal) if self._focal is None
                       else self._focal + FOCAL_EMA * (float(focal)
                                                       - self._focal))

    def latest(self) -> dict | None:
        """最新一帧的完整快照（横位/状态/线/锚点同源）。

        2026-10-24 重写后不再把「最新预测横位」和「上一次检测的线」拼在一起：
        线和横位必须来自同一拍，否则调试图会画出与横位不对应的线，离线也
        无法判断线锚是否算错。检测龄 ``det_age_ms`` 单列最近一次真实检测的
        年龄；``pred=true`` 表示这一拍横位来自滑行预测（无可用线或跳帧），
        不是线观测结果。"""
        with self._lock:
            if self._latest is None:
                return None
            out = dict(self._latest)
        t_done = out.pop("t_done", None)
        t_det = out.pop("t_det", None)
        out["det_fid"] = self._last_det_fid
        out["det_age_ms"] = (None if t_det is None
                             else round((time.perf_counter() - t_det) * 1000.0,
                                        1))
        out["age_ms"] = (0.0 if t_done is None
                         else round((time.perf_counter() - t_done) * 1000.0, 1))
        out["provider"] = "dml" if self._use_dml else "cpu"
        out["busy_skips"] = self.busy_skips
        out["hold_ms"] = self.hold_ms
        out["det_n"] = self.det_n
        out["pred_n"] = self.pred_n
        out["push_n"] = self.push_n
        out["drop_n"] = self.drop_n
        out["cam_h_fb"] = self.cam_h_fb
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rgb, fid, cam_h, v_lat, dt, jump, focal, anchor = \
                    self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._set_focal(focal)
            beat()          # 心跳：性能面板的「每秒几拍」由 core 差分得出
            t0 = time.perf_counter()
            try:
                self._n += 1
                every = max(1, int(self.cfg.detect_every))
                pred = every > 1 and bool(self._n % every)
                if pred:
                    self.pred_n += 1
                    res = self._tracker.predict(dt, v_lat)
                else:
                    self.det_n += 1
                    rec = self._infer(rgb, cam_h, self._focal)
                    if rec is None:      # DML 被占：本帧让路，下一帧再来
                        continue
                    yv = _vanish_row(rec["lines"])
                    if yv is not None:
                        self._yv_raw = float(yv)
                        self._yv, self._yv_t = _smooth_vanish(
                            self._yv_hist, yv, time.perf_counter(), self._yv_t)
                    res = self._tracker.update(rec["lines"], fid, dt, v_lat,
                                               jump, anchor)
                t_done = time.perf_counter()
                if not pred:
                    self._last_det_t = t_done
                    self._last_det_fid = int(fid)
                out = self._snapshot(res, fid, cam_h, t0, t_done, pred)
                with self._lock:
                    self._latest = out
            except Exception as exc:  # noqa: BLE001 —— 单帧失败只记错误
                self.error = repr(exc)

    def _snapshot(self, res: dict, fid: int, cam_h: float, t0: float,
                  t_done: float, pred_fallback: bool) -> dict:
        """tracker 输出 → 线程对外快照。

        字段必须逐个显式搬运（不是 ``dict(res)``）：上游新增的位置原点判据
        （usable/locked）若在这里漏搬，消费方永远读到 None——线为主会静默
        失效且看不出错误。"""
        is_pred = bool(res.get("pred", pred_fallback))
        # 相机高反解只在真实检测拍上算：预测拍的线是上一拍的，重复发布等于把
        # 同一个样本数两遍（守卫按「新样本」计数）。
        h_lane = None if is_pred else _cam_h_from_lines(res["lines"], self._yv)
        return {"fid": fid, "cam_h": round(cam_h, 3),
                "pred": is_pred,
                "ms": round((t_done - t0) * 1000.0, 1),
                "n_lines": res["n_lines"],
                "x": None if res["x"] is None else round(res["x"], 3),
                "theta": None if res["theta"] is None
                else round(res["theta"], 4),
                "state": res["state"],
                "n_locked": res["n_locked"],
                "relock_src": res["relock_src"],
                "anchor_src": res.get("anchor_src"),
                "grid_reject": res["grid_reject"],
                "side_reject": res["side_reject"],
                "usable": bool(res.get("usable", False)),
                "locked": int(res.get("locked", 0)),
                "focal": None if self._focal is None
                else round(self._focal, 1),
                # 灭点行（地平线）：合成点图与逐车距离的米制骨架。``y_v`` 是
                # 近 YV_WIN 个检测拍的中位（发布值，深度链吃这个）；``y_v_raw``
                # 是本拍单帧估计，只作诊断——两者的差就是平滑吃掉的噪声。
                "y_v": None if self._yv is None else round(self._yv, 2),
                "y_v_raw": None if self._yv_raw is None
                else round(self._yv_raw, 2),
                # 相机高反解（线间距法）：视角守卫的唯一诚实判据，证据不足=None
                "cam_h_lane": None if h_lane is None else round(h_lane, 3),
                "t_done": t_done, "t_det": self._last_det_t,
                # 线与横位同拍：annotated（k/resid/accepted）由 tracker 给出
                "lines": res["lines"]}

    def _run(self, rgb: np.ndarray):
        """DML 走共享锁：只包 run 本体；抢不到即弃帧（latest-only，不排队）。"""
        assert self._sess is not None   # 线程只在 session 建好后才起
        inp = self._sess.get_inputs()[0].name
        feed = {inp: _letterbox(rgb)}
        if not self._use_dml:
            return _prob_from_out(self._sess.run(None, feed))
        if not dml_lock.LOCK.acquire(blocking=False):
            self.busy_skips += 1
            return None
        dml_lock.RUN_WATCH.enter("lane")
        try:
            t = time.perf_counter()
            out = self._sess.run(None, feed)
            self.hold_ms = round((time.perf_counter() - t) * 1000.0, 1)
        finally:
            dml_lock.RUN_WATCH.exit()
            dml_lock.LOCK.release()
        return _prob_from_out(out)

    def _infer(self, rgb: np.ndarray, cam_h: float,
               focal: float | None) -> dict | None:
        prob = self._run(rgb)
        if prob is None:
            return None
        # 先剔 HUD 距离徽章上的假线，再进 tracker：徽章假线位置正好落在贴脸真线
        # 的目标带内，若进 tracker 会被当成真线锁道号，事后无法区分。
        lines = _drop_badge_lines(_lines_from_prob(prob, cam_h, focal), rgb)
        return {"lines": lines}
