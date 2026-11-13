# -*- coding: utf-8 -*-
"""权重移植：现役 ONNX → torch（微调起点），含输出对齐校验。

官方 + 家族 .pth 直链已死（见 README 调研节），现役权重只有 ONNX 一份。本脚本
不引入新依赖：ONNX 是 protobuf，按线格式直接解析出 initializer 张量与图节点，
按「节点名 = 模块路径」映射进官方 TwinLiteNetPlus(small) 的 state_dict：

- 名字直配（多数张量保留原名）+ 路径角色映射（/encoder/level1/conv/Conv →
  encoder.level1.conv.{weight,bias}；重复段自动收敛，如 Sequential 的
  /caam/conv_out/conv_out.0/Conv → caam.conv_out.0）；
- 导出时被折叠进卷积的 BatchNorm：卷积偏置改道为 bn.bias，γ=√(1+ε)、μ=0、
  σ²=1 合成恒等——与折叠结果严格等价（bn(Conv(x)) = W'x + b'）；
- 线性层按 MatMul(+Add) 拆开导出的，按名直配接住。

最后用真实帧对比 torch 与 onnxruntime 的双头 softmax 输出（/255 口径，与生产
一致），逐位对齐才算移植成功。

用法：python transplant.py [--save]
  --save 时把移植后的权重存到 weights/plus_small_ft_init.pth（gitignored）
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
UP = HERE.parent / "upstream"
WEIGHTS_ONNX = HERE.parents[3] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"

# model/model.py 顶层 import 了 matplotlib（仅 import 未使用）；桩掉避免装依赖
_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import cv2  # noqa: E402
import onnxruntime as ort  # noqa: E402
import torch  # noqa: E402

from model.model import TwinLiteNetPlus  # noqa: E402  # 上游官方定义（锁定 90f1b86）


# ---------- ONNX protobuf 线格式极简解析（只认本仓导出图所需字段） ----------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, i
        s += 7


def _fields(buf: bytes):
    """yield (field_number, wire_type, value)；wt0=varint，其余为 bytes。"""
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"wire_type={wt}")
        yield f, wt, v


def parse_initializers(onnx_path: Path) -> dict[str, np.ndarray]:
    """ModelProto.graph(7).initializer(5) → {名字: fp32 数组}。"""
    model = onnx_path.read_bytes()
    graph = next(v for f, _, v in _fields(model) if f == 7 and isinstance(v, bytes))
    out: dict[str, np.ndarray] = {}
    for f, _, v in _fields(graph):
        if f != 5 or not isinstance(v, bytes):
            continue
        name, dims, raw, float_data = None, [], None, None
        for tf, tw, tv in _fields(v):
            if tf == 1:
                if tw == 0:
                    dims.append(tv)
                else:
                    i = 0
                    while i < len(tv):
                        d, i = _varint(tv, i)
                        dims.append(d)
            elif tf == 2:
                assert tv == 1, f"非 fp32 张量（data_type={tv}）"
            elif tf == 8:
                name = tv.decode()
            elif tf == 9:
                raw = tv
            elif tf == 4:
                float_data = tv
        if name is None:
            continue
        data = raw if raw is not None else float_data
        out[name] = np.frombuffer(data, dtype=np.float32).copy().reshape(dims)
    return out


def parse_nodes(onnx_path: Path) -> list[tuple[str, str, list[str]]]:
    """graph.node(1) → [(op_type, 节点名, [输入张量名…])]，按图顺序。"""
    model = onnx_path.read_bytes()
    graph = next(v for f, _, v in _fields(model) if f == 7 and isinstance(v, bytes))
    nodes = []
    for f, _, v in _fields(graph):
        if f != 1 or not isinstance(v, bytes):
            continue
        op = name = None
        ins = []
        for nf, _, nv in _fields(v):
            if nf == 4:
                op = nv.decode()
            elif nf == 3:
                name = nv.decode()
            elif nf == 1 and isinstance(nv, bytes):
                ins.append(nv.decode())
        nodes.append((op, name or "", ins))
    return nodes


# ---------- 映射 ----------

_ROLE = {"Conv": {1: "weight", 2: "bias"},
         "ConvTranspose": {1: "weight", 2: "bias"},
         "BatchNormalization": {1: "weight", 2: "bias", 3: "running_mean",
                                4: "running_var"},
         "PRelu": {1: "weight"},
         "Gemm": {1: "weight", 2: "bias"},
         "MatMul": {1: "weight"}}


def transplant(model: torch.nn.Module, inits: dict[str, np.ndarray],
               nodes: list[tuple[str, str, list[str]]]) -> tuple[list, list]:
    """确定性映射。返回（缺失 state 键, 未分配的 initializer 名）。"""
    sd = model.state_dict()
    modules = dict(model.named_modules())
    assign: dict[str, str] = {}
    transpose_keys: set[str] = set()   # MatMul 权重：图内存为 [in,out]，需转置
    fused: set[str] = set()

    def resolve(parts: list[str]) -> str | None:
        cand = ".".join(parts)
        if cand in modules:
            return cand
        for i in range(len(parts)):        # Sequential 数字子模块的重复段收敛
            alt = ".".join(parts[:i] + parts[i + 1:])
            if alt in modules:
                return alt
        return None

    def redirect_bias(conv_path: str, bias_name: str) -> None:
        """折叠 BN：卷积偏置 → 相邻 BatchNorm2d 的 β（同体 .bn 或 Sequential 后继）。"""
        parent_path, _, leaf = conv_path.rpartition(".")
        parent = modules.get(parent_path)
        bn = None
        if parent is not None and isinstance(getattr(parent, "bn", None),
                                             torch.nn.BatchNorm2d):
            bn = parent_path + ".bn"                       # ConvBatchnormRelu 同体
        elif leaf.isdigit():
            sib = f"{parent_path}.{int(leaf) + 1}"         # Sequential 相邻
            if isinstance(modules.get(sib), torch.nn.BatchNorm2d):
                bn = sib
        if bn is not None:
            key = f"{bn}.bias"
            if key in sd and key not in assign:
                assign[key] = bias_name
                fused.add(bn)

    with torch.no_grad():
        # ① 按名直配（任意节点引用的 initializer 名 == state 键；接住 MatMul+Add 的线性层）
        for op, name, ins in nodes:
            for n in ins:
                if n in sd and n not in assign:
                    assign[n] = n
        # ② 路径角色映射 + 折叠偏置改道
        for op, name, ins in nodes:
            roles = _ROLE.get(op)
            if not roles or not name:
                continue
            mod_path = resolve(name.strip("/").split("/")[:-1])
            if mod_path is None:
                continue
            for idx, role in roles.items():
                if len(ins) <= idx or not ins[idx] or ins[idx] not in inits:
                    continue
                key = f"{mod_path}.{role}"
                if key in sd:
                    if key not in assign:
                        assign[key] = ins[idx]
                        if op == "MatMul":
                            transpose_keys.add(key)
                elif role == "bias":
                    redirect_bias(mod_path, ins[idx])
        # ③ 折叠 BN 其余参数 → 恒等变换
        for bn in fused:
            eps = modules[bn].eps
            sd[f"{bn}.weight"].copy_(
                torch.full_like(sd[f"{bn}.weight"], float(np.sqrt(1.0 + eps))))
            sd[f"{bn}.running_mean"].zero_()
            sd[f"{bn}.running_var"].fill_(1.0)
        # ④ 落盘（允许转置/形状重排）
        for key, iname in assign.items():
            tensor = sd[key]
            arr = inits[iname]
            if arr.shape != tuple(tensor.shape):
                if arr.T.shape == tuple(tensor.shape):
                    arr = np.ascontiguousarray(arr.T)
                else:
                    assert arr.size == tensor.numel(), \
                        f"形状不符 {key} ← {iname}: {tuple(tensor.shape)} vs {tuple(arr.shape)}"
                    arr = arr.reshape(tensor.shape)
            elif key in transpose_keys:     # 方阵 MatMul 权重：形状不变但语义为转置
                arr = np.ascontiguousarray(arr.T)
            tensor.copy_(torch.from_numpy(arr))
    missing = [k for k in sd if k not in assign
               and not k.endswith("num_batches_tracked")
               and not any(k == m or k.startswith(m + ".") for m in fused)]
    unused = [n for n in inits if n not in set(assign.values())
              and any(n in ins for _, _, ins in nodes)]
    return missing, unused


# ---------- 对齐校验 ----------

def letterbox(rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def _softmax1(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(0, keepdims=True))
    return (e / e.sum(0, keepdims=True))[1]


def parity_check(model: torch.nn.Module, n_frames: int = 8) -> float:
    sess = ort.InferenceSession(str(WEIGHTS_ONNX), providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name
    names = [o.name for o in sess.get_outputs()]
    model.eval()
    worst = 0.0
    for skey in ("20261109_104823_manual_p1", "20261109_104823_manual_p2"):
        fs = sorted((RAW_ROOT / skey / "raw").glob("*.jpg"))[::300][: n_frames // 2]
        for fp in fs:
            rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            x = letterbox(rgb)
            ref = dict(zip(names, sess.run(names, {in_name: x})))
            with torch.no_grad():
                da_t, ll_t = model(torch.from_numpy(x))
            for refv, tt in ((ref["da"], da_t), (ref["ll"], ll_t)):
                d = float(np.abs(_softmax1(refv[0]) - _softmax1(tt[0].numpy())).max())
                worst = max(worst, d)
    return worst


def main():
    save = "--save" in sys.argv
    inits = parse_initializers(WEIGHTS_ONNX)
    nodes = parse_nodes(WEIGHTS_ONNX)
    print(f"ONNX initializer 数: {len(inits)}，节点数: {len(nodes)}")
    model = TwinLiteNetPlus(types.SimpleNamespace(config="small"))
    missing, unused = transplant(model, inits, nodes)
    print(f"state_dict 缺键: {len(missing)} {missing[:6] or ''}")
    print(f"ONNX 未用张量: {len(unused)} {unused[:6] or ''}")
    worst = parity_check(model)
    print(f"对齐校验（softmax 后最大绝对差）: {worst:.3e}")
    ok = not missing and not unused and worst < 1e-4
    print("移植判定:", "PASS" if ok else "FAIL")
    if ok and save:
        out = HERE.parent / "weights/plus_small_ft_init.pth"
        out.parent.mkdir(exist_ok=True)
        torch.save(model.state_dict(), out)
        print("已存:", out)


if __name__ == "__main__":
    main()
