#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YOLO11n 微调 → ONNX 候选产物（**不自动覆盖生产权重**）。

**与旧脚本的差别**
1. 可指定初始权重（官方 ``yolo11n.pt`` 或旧 ``best.pt``），做双基线对照；
2. 数据划分读 ``build_dataset_index.py`` 的清单（会话级、无连续帧泄漏），
   不再直接用仓库里的随机切分；
3. 训练产物落在 ``build/train_output/<name>/``；只有显式 ``--promote`` 才复制到
   插件权重位，并打印新的尺寸与 SHA256——``model_assets.py`` 的清单必须同步改，
   否则运行期拒绝加载。

**默认超参是"可信基线"起点，不是调好的最优值**：小数据集微调用 AdamW、低学习率、
偏保守的增强（关闭水平翻转——本玩法画面有固定车道语义）。

用法：
    python .../train.py --name mixed640_v1（缺省读插件 data/index/dataset_index.json）
    python .../train.py --weights build/old/best.pt --name finetune_v2 --epochs 60
    python .../train.py --export-only --weights build/train_output/mixed640_v1/weights/best.pt
    python .../train.py --promote --weights .../best.pt
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[4]
sys.path.insert(0, str(REPO))

from maaracing_master.plugins.speedrush.tools.training.splits import (  # noqa: E402
    write_data_yaml,
    write_split_lists,
)


def _names_from_dataset_yaml() -> dict[int, str]:
    import yaml

    cfg = yaml.safe_load((SCRIPT_DIR / "dataset.yaml").read_text(encoding="utf-8"))
    return {int(k): str(v) for k, v in cfg["names"].items()}


def _parse_imgsz(text: str) -> int | tuple[int, int]:
    if "x" in text.lower():
        height, width = text.lower().split("x", 1)
        return int(height), int(width)
    return int(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="YOLO11n 微调并导出 ONNX 候选")
    parser.add_argument("--weights", default="yolo11n.pt",
                        help="初始权重：官方 yolo11n.pt 或旧 best.pt")
    parser.add_argument("--manifest",
                        default=str(REPO / "maaracing_master" / "plugins" / "speedrush"
                                     / "data" / "index" / "dataset_index.json"),
                        help="数据集索引；与 --data 二选一")
    parser.add_argument("--data", default=None, help="直接给 Ultralytics data.yaml")
    parser.add_argument("--name", default="run", help="实验名（输出目录后缀）")
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--imgsz", default="640", help="训练尺寸；导出另用 --export-imgsz")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--patience", type=int, default=30)
    parser.add_argument("--device", default="0")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--optimizer", default="AdamW")
    parser.add_argument("--lr0", type=float, default=5e-4)
    parser.add_argument("--lrf", type=float, default=0.1)
    parser.add_argument("--mosaic", type=float, default=0.3)
    parser.add_argument("--close-mosaic", type=int, default=15)
    parser.add_argument("--scale", type=float, default=0.35)
    parser.add_argument("--translate", type=float, default=0.10)
    parser.add_argument("--fliplr", type=float, default=0.0)
    parser.add_argument("--export-only", action="store_true", help="跳过训练，只导出 ONNX")
    parser.add_argument("--export-imgsz", default=None,
                        help="导出形状：640 或 384x640；缺省同 --imgsz")
    parser.add_argument("--promote", action="store_true",
                        help="显式把候选 ONNX 复制到生产权重位（默认不复制）")
    args = parser.parse_args()

    project_dir = REPO / "build" / "train_output" / args.name
    project_dir.mkdir(parents=True, exist_ok=True)

    if not args.export_only:
        from ultralytics import YOLO

        train_size = _parse_imgsz(args.imgsz)
        if isinstance(train_size, tuple):
            print("训练只接受方形边长（官方接口约束）；矩形形状请用 --export-imgsz",
                  file=sys.stderr)
            return 2
        if args.data:
            data_cfg = str(Path(args.data).resolve())
        else:
            lists = write_split_lists(Path(args.manifest), project_dir / "splits",
                                      splits=("train", "val"))
            data_cfg = str(write_data_yaml(_names_from_dataset_yaml(), lists,
                                           project_dir / "data.yaml"))
        model = YOLO(args.weights)
        model.train(
            data=data_cfg,
            epochs=args.epochs,
            imgsz=train_size,
            batch=args.batch,
            patience=args.patience,
            device=args.device,
            workers=args.workers,
            seed=args.seed,
            optimizer=args.optimizer,
            lr0=args.lr0,
            lrf=args.lrf,
            cos_lr=True,
            warmup_epochs=3,
            mosaic=args.mosaic,
            close_mosaic=args.close_mosaic,
            scale=args.scale,
            translate=args.translate,
            perspective=0.0,
            fliplr=args.fliplr,
            project=str(project_dir.parent),
            name=project_dir.name,
            exist_ok=True,
        )

    weights = Path(args.weights)
    if not args.export_only:
        weights = project_dir / "weights" / "best.pt"
    elif not weights.is_file():
        print(f"权重不存在: {weights}", file=sys.stderr)
        return 2

    from ultralytics import YOLO

    export_size = _parse_imgsz(args.export_imgsz or args.imgsz)
    best = YOLO(str(weights))
    onnx_path = Path(best.export(format="onnx", imgsz=export_size,
                                 simplify=True, opset=17))
    print(f"候选 ONNX: {onnx_path}")
    print(f"尺寸 {onnx_path.stat().st_size}  sha256 {_sha256(onnx_path)}")
    print("验收请用 eval_detector.py（生产链路），不要用训练日志的 mAP。")

    if args.promote:
        dst = (REPO / "maaracing_master" / "plugins" / "speedrush"
               / "resources" / "models" / "perception" / "model.onnx")
        shutil.copy2(onnx_path, dst)
        print(f"已复制到生产权重位: {dst}")
        print("⚠ 必须同步更新 plugins/speedrush/model_assets.py 的尺寸与 SHA256，"
              "否则运行期拒绝加载；发布包与模型说明同批更新。")
    else:
        print("未提升为生产权重（需要时加 --promote）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
