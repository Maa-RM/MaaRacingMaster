"""离线复核焦距扫描的可见性选择偏差；不输出生产标定。

只读录制帧与 trace。输出包含全部候选的分数与样本身份，不把峰值当真值。
默认输入来自应用数据目录；可用 --base、--trace 指定同一局的素材。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np


def load(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def select(frames, trace):
    runs, cur = [], None
    for r in trace:
        ts = r["ts_ns"]
        if r.get("hud_rate") == 41:
            cur = [ts, ts] if cur is None else [cur[0], ts]
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    runs = sorted([r for r in runs if r[1]-r[0] >= 1e9], key=lambda r: -(r[1]-r[0]))
    # 精确复刻原报告的窗口、步距与时差筛选，包括无第二阶段帧的窗口。
    pairs = []
    for group, (a, b) in enumerate(runs[:4]):
        ids = [i for i, f in enumerate(frames) if a <= f["ts_ns"] <= b]
        for i in ids[::20]:
            if i+1 < len(frames) and frames[i+1]["ts_ns"] <= b:
                dt = (frames[i+1]["ts_ns"]-frames[i]["ts_ns"])/1e9
                if .035 <= dt <= .075:
                    pairs.append((group, i, dt))
    return pairs


def scan(a, b, pts, yh, distance, grid, warped=False):
    """逐点保存 NCC；固定点集合复用同一张得分表，不更换观测对象。"""
    pad = 7
    offy, offx = np.mgrid[-pad:pad+1, -pad:pad+1].astype(np.float32)
    ox, oy = offx.ravel(), offy.ravel()
    centers = np.rint(pts).astype(np.float32)
    ax, ay = centers[:, 0, None]+ox, centers[:, 1, None]+oy
    source = cv2.remap(a, ax, ay, cv2.INTER_NEAREST).astype(np.float32)
    source -= source.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(source, axis=1)
    scores = np.full((len(grid), len(pts)), np.nan)
    for gi, k in enumerate(grid):
        z = k/(pts[:, 1]-yh)-distance
        scale = (z+distance)/z
        px = 640.5+(pts[:, 0]-640.5)*scale
        py = yh+k/z
        valid = ((z > .3) & (px >= pad-1) & (px < a.shape[1]-pad+1)
                 & (py >= pad-1) & (py < a.shape[0]-pad+1) & (norm > 1e-6))
        best = np.full(len(pts), -1., np.float32)
        if warped:
            den = 1-distance*(ay-yh)/k
            mx = 640.5+(ax-640.5)/den
            my = yh+(ay-yh)/den
            valid &= ((den > 0).all(axis=1) & (mx.min(axis=1) >= 1)
                      & (mx.max(axis=1) < a.shape[1]-2) & (my.min(axis=1) >= 1)
                      & (my.max(axis=1) < a.shape[0]-2))
        for du in (-1, 0, 1):
            for dv in (-1, 0, 1):
                if warped:
                    bx, by = mx+du, my+dv
                else:
                    # 原脚本对预测中心取整、取等大方块，局部只允许平移。
                    bx = np.rint(px+du)[:, None]+ox
                    by = np.rint(py+dv)[:, None]+oy
                patch_ok = ((bx.min(axis=1) >= 0) & (bx.max(axis=1) < a.shape[1])
                            & (by.min(axis=1) >= 0) & (by.max(axis=1) < a.shape[0]))
                sample = cv2.remap(b, bx.astype(np.float32), by.astype(np.float32),
                                   cv2.INTER_LINEAR if warped else cv2.INTER_NEAREST).astype(np.float32)
                sample -= sample.mean(axis=1, keepdims=True)
                ns = np.linalg.norm(sample, axis=1)
                ncc = np.sum(sample*source, axis=1)/np.maximum(ns*norm, 1e-12)
                best = np.maximum(best, np.where(patch_ok & (ns > 1e-6), ncc, -1))
        scores[gi, valid & (best > -1)] = best[valid & (best > -1)]
    return scores


def peak(grid, scores, minimum=30):
    count = np.isfinite(scores).sum(axis=1)
    avg = np.nansum(scores, axis=1)/np.maximum(count, 1)
    avg[count < minimum] = -2
    j = int(np.argmax(avg))
    return {"k": float(grid[j]), "f": float(grid[j]/1.259), "n": int(count[j]),
            "score": float(avg[j]), "boundary": bool(j in (0, len(grid)-1))}


def direction_control(a):
    """已知几何图检验整图版的变换方向；与局部特征版分开记账。"""
    distance, yh, truth = 2.05, 361., 630*1.259

    def hom(k):
        q = distance/k
        return np.array([[1.,-640.5*q,640.5*q*yh],
                         [0.,1-yh*q,yh*yh*q],[0.,-q,1+q*yh]])

    def grad(g):
        g = cv2.GaussianBlur(g,(3,3),0)
        return cv2.magnitude(cv2.Sobel(g,cv2.CV_32F,1,0),cv2.Sobel(g,cv2.CV_32F,0,1))

    b = cv2.warpPerspective(a,hom(truth),(1280,720))
    aa, bb = grad(a),grad(b)
    ans = {"true_k":truth}
    for label, inverse in (("forward_applied_to_second",False),("inverse_applied_to_second",True)):
        curve = []
        for k in np.arange(600.,1401.,20.):
            h = np.linalg.inv(hom(k)) if inverse else hom(k)
            warped = cv2.warpPerspective(bb,h,(1280,720))
            mask = cv2.warpPerspective(np.ones_like(bb),h,(1280,720),flags=cv2.INTER_NEAREST)>0
            band = np.zeros_like(mask)
            band[395:610,80:1200] = True
            valid = band & mask
            x,y = aa[valid],warped[valid]
            x,y = x-x.mean(),y-y.mean()
            curve.append((float(k),float(np.mean(x*y)/(x.std()*y.std()))))
        k,score = max(curve,key=lambda t:t[1])
        ans[label] = {"k":k,"score":score}
    return ans


def marker_control(frames,trace,times):
    """人工看图确认同一对虚线端点；非机器金标，也不作为新焦距裁决。"""
    seq = [709,710,711,712,713]
    t = np.array([(frames[s-1]["ts_ns"]-frames[708]["ts_ns"])/1e9 for s in seq])
    yh = np.array([(trace[min(bisect.bisect_left(times,frames[s-1]["ts_ns"]),len(trace)-1)]
                    .get("lane_obs") or {}).get("y_v",361.) for s in seq])
    out = {"seq":seq,"t":t.tolist(),"yh":yh.tolist(),"tracks":[]}
    for label,ys in (("left",[411,421,436,450,514]),("right",[411,421,436,451,514])):
        slope,offset = np.polyfit(t,1/(np.array(ys)-yh),1)
        out["tracks"].append({"label":label,"y":ys,"f_assuming_v41":float(-41/slope/1.259),
                              "residual_inverse_row":(1/(np.array(ys)-yh)-(slope*t+offset)).tolist()})
    return out


def projection_control(a,b,pts,yh,dt,focal):
    """已知大焦距的正对照；只检验估计器是否内生地把该值压低。"""
    grid = np.arange(500.,max(1700.,np.ceil(focal*1.259/10)*10+200)+1,10.)
    distance = 41*dt
    q = distance/(focal*1.259)
    h = np.array([[1.,-640.5*q,640.5*q*yh],
                  [0.,1-yh*q,yh*yh*q],[0.,-q,1+q*yh]])
    synthetic = cv2.warpPerspective(a,h,(a.shape[1],a.shape[0]))
    synth_scores = scan(a,synthetic,pts,yh,distance,grid,warped=True)
    real_scores = scan(a,b,pts,yh,distance,grid,warped=True)
    hypotheses = (520.,630.,807.7,focal)
    ids = [int(np.argmin(abs(grid-f*1.259))) for f in hypotheses]
    real_common = np.isfinite(real_scores[ids]).all(axis=0)
    synth_common = np.isfinite(synth_scores[ids]).all(axis=0)
    if not real_common.any() or not synth_common.any():
        return {"synth_true":focal,"reason":"no_common_support"}
    return {"synth_true":focal,"candidate_f":list(hypotheses),
            "real_fixed":peak(grid,np.where(real_common[None,:],real_scores,np.nan),int(real_common.sum())),
            "synth_fixed":peak(grid,np.where(synth_common[None,:],synth_scores,np.nan),int(synth_common.sum())),
            "real_candidate_scores":np.nanmean(real_scores[ids][:,real_common],axis=1).tolist(),
            "n":int(real_common.sum())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(os.environ.get("APPDATA", "."))/"MaaRacingMaster"
    parser.add_argument("--base", type=Path, default=root/"debug/speedrush/20261117_223453_manual_p2")
    parser.add_argument("--trace", type=Path, default=root/"logs/20261117_223447/trace/speedrush.jsonl")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent/"focal_audit_out/audit.json")
    parser.add_argument("--controls", action="store_true", help="追加已知焦距合成对照与倒放阴性对照")
    parser.add_argument("--projection-focal",type=float,help="指定另一个已知合成焦距，检验是否存在固有尺度压低")
    args = parser.parse_args()
    cv2.setNumThreads(2)
    started = time.monotonic()
    frames = load(args.base/"frames.jsonl")
    trace = sorted([r for r in load(args.trace) if r.get("ts_ns") and not r.get("type")],
                   key=lambda r:r["ts_ns"])
    times = [r["ts_ns"] for r in trace]
    grid = np.arange(500., 1401., 10.)
    results = []
    for group, i, dt in select(frames, trace):
        if time.monotonic()-started > 220:
            raise TimeoutError("复核超过计时上限")
        a = cv2.imread(str(args.base/frames[i]["file"]), cv2.IMREAD_GRAYSCALE)
        b = cv2.imread(str(args.base/frames[i+1]["file"]), cv2.IMREAD_GRAYSCALE)
        r = trace[min(bisect.bisect_left(times, frames[i]["ts_ns"]), len(trace)-1)]
        lane = r.get("lane_obs") or {}
        yh = lane.get("y_v") or r.get("dgeo_yv") or 361.
        mask = np.zeros_like(a)
        mask[450:580,320:960] = 255
        ps = cv2.goodFeaturesToTrack(a, 160, .01, 12, blockSize=7, mask=mask)
        if ps is None or len(ps) < 40:
            continue
        pts = ps[:,0]
        scores = scan(a,b,pts,yh,41*dt,grid)
        original = peak(grid,scores)
        # 两种焦距在完全相同的点上比较，不依赖最低扫描边界。
        candidates = [int(np.argmin(abs(grid-k))) for k in (original["k"],630*1.259,807.7*1.259)]
        common = np.isfinite(scores[candidates]).all(axis=0)
        fixed = peak(grid,np.where(common[None,:],scores,np.nan),minimum=int(common.sum()))
        warped_scores = scan(a,b,pts,yh,41*dt,grid,warped=True)
        warped_common = np.isfinite(warped_scores[candidates]).all(axis=0)
        warped_peak = peak(grid,np.where(warped_common[None,:],warped_scores,np.nan),
                           minimum=int(warped_common.sum()))
        row = {"group":group,"i":i,"seq":frames[i]["seq"],"dt":dt,"yh":yh,
               "original":original,"fixed":fixed,"warped":warped_peak,
               "common_n":int(common.sum()),"warped_common_n":int(warped_common.sum()),
               "candidate_common_scores":np.nanmean(scores[candidates][:,common],axis=1).tolist(),
               "points":pts.tolist(),"common_ids":np.flatnonzero(common).tolist(),
               "curve":[{"k":float(k),"n":int(np.isfinite(s).sum()),
                         "mean":float(np.nanmean(s)) if np.isfinite(s).any() else None}
                        for k,s in zip(grid,scores)]}
        if args.controls:
            tests = {}
            for ftrue in (520.,630.,807.7):
                q = 41*dt/(ftrue*1.259)
                hom = np.array([[1.,-640.5*q,640.5*q*yh],
                                [0.,1-yh*q,yh*yh*q],[0.,-q,1+q*yh]])
                synth = cv2.warpPerspective(a,hom,(a.shape[1],a.shape[0]))
                tests[str(ftrue)] = peak(grid,scan(a,synth,pts,yh,41*dt,grid))
            tests["reverse"] = peak(grid,scan(b,a,pts,yh,41*dt,grid))
            row["controls"] = tests
        if args.projection_focal is not None:
            if not np.isfinite(args.projection_focal) or args.projection_focal <= 0:
                raise ValueError("合成焦距必须是有限正数")
            row["projection_control"] = projection_control(a,b,pts,yh,dt,args.projection_focal)
        results.append(row)
        print(json.dumps({k:v for k,v in row.items() if k not in ("curve","points","common_ids")}),flush=True)
    report = {"session":args.base.name,"pairs":results}
    if args.controls and args.base.name == "20261117_223453_manual_p2":
        report["direction_control"] = direction_control(cv2.imread(str(args.base/"raw/0709_raw.jpg"),0))
        report["manual_markers"] = marker_control(frames,trace,times)
        print("direction",json.dumps(report["direction_control"]),flush=True)
        print("manual_markers",json.dumps(report["manual_markers"]),flush=True)
    args.out.parent.mkdir(exist_ok=True,parents=True)
    args.out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("saved",args.out.name,"seconds",round(time.monotonic()-started,2),flush=True)


if __name__ == "__main__":
    main()
