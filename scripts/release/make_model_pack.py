#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成车道线驾驶模型包。

内容：模型权重组（含线观测 FP16 加速件）+ 许可/安装说明 README + SHA256SUMS，打成一个 zip。
发布包默认不含权重；用户下载本包后解压到
``%APPDATA%\\MaaRacingMaster\\data\\infinite_drive\\models\\`` 即可使用车道线驾驶。
线观测受 BDD100K 非商业/科研许可约束，构建时会一并打入
``LICENSE-BDD100K.txt``；源文件或许可缺失时本脚本直接失败。

用法：
    python scripts/release/make_model_pack.py --version 0.30.0-dev.1
    python scripts/release/make_model_pack.py --version X --out build/release

生成前会用 ``model_assets.MODEL_MANIFEST`` 校验源文件（尺寸 + SHA256），
不匹配直接失败，避免把损坏/错版权重发给用户。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "maaracing_master" / "plugins" / "infinite_drive" / "resources" / "models"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from maaracing_master.plugins.infinite_drive import model_assets  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(version: str, out_dir: Path, github_repo: str = "",
          cnb_path: str = "") -> tuple[Path, Path, Path]:
    version = version[1:] if version.startswith("v") else version
    rels = sorted(model_assets.MODEL_MANIFEST)
    files: list[tuple[str, Path]] = []
    sums: list[str] = []
    for rel in rels:
        src = model_assets.MODELS_DIR / model_assets.packed_rel(rel)   # 仓库内真源
        packed = model_assets.packed_rel(rel)
        err = model_assets.verify(src, rel)
        if err:
            raise SystemExit(f"模型包源文件不可用：{err}")
        files.append((packed, src))
        sums.append(f"{_sha256(src)}  {packed}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"MaaRacingMaster-infinite_drive-models-{version}.zip"
    readme = MODELS_DIR / "README.md"
    license_file = MODELS_DIR / "LICENSE-BDD100K.txt"
    if not license_file.is_file():
        raise SystemExit(f"模型包许可文件缺失：{license_file}")

    # 可复现归档：固定成员顺序、时间戳、权限与压缩级别；源文件的 mtime/权限不进包。
    def add_member(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o644 << 16
        zf.writestr(info, data)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for packed, src in files:
            add_member(zf, packed, src.read_bytes())
        if readme.is_file():
            add_member(zf, "README.md", readme.read_bytes())
        add_member(zf, "LICENSE-BDD100K.txt", license_file.read_bytes())
        add_member(zf, "SHA256SUMS", ("\n".join(sums) + "\n").encode("utf-8"))

    digest = _sha256(zip_path)
    sha_path = Path(str(zip_path) + ".sha256")
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")

    # 下载描述随主包分发，不随模型 ZIP。前端不传 URL，运行时只消费本描述。
    # 这里的先后只是描述的稳定默认顺序，不决定运行时首选：安装器按宿主
    # update-prefs.json 的 source（默认 cnb）重排（model_installer._ordered_sources），
    # 同一个「首选下载源」参数只维护在宿主一处。
    tag = version if version.startswith("v") else f"v{version}"
    sources: dict[str, str] = {}
    if cnb_path:
        sources["cnb"] = (
            f"https://cnb.cool/{cnb_path}/-/releases/download/{tag}/{zip_path.name}")
    if github_repo:
        sources["github"] = (
            f"https://github.com/{github_repo}/releases/download/{tag}/{zip_path.name}")
    unpacked = sum(src.stat().st_size for _packed, src in files)
    for extra in (readme, license_file):
        if extra.is_file():
            unpacked += extra.stat().st_size
    descriptor = {
        "tag": tag,
        "asset": zip_path.name,
        "sha256": digest,
        "size": zip_path.stat().st_size,
        "unpacked_size": unpacked,
        "manifest_digest": model_assets.manifest_digest(),
        "sources": sources,
    }
    desc_path = out_dir / "model-download.json"
    desc_path.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return zip_path, sha_path, desc_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成车道线驾驶模型包")
    parser.add_argument("--version", required=True, help="版本号（可带 v 前缀）")
    parser.add_argument("--out", default=str(REPO_ROOT / "build" / "release"),
                        help="输出目录（默认 build/release）")
    parser.add_argument("--github-repo", default="",
                        help="GitHub 仓库（owner/repo），用于生成同版本下载描述")
    parser.add_argument("--cnb-path", default="",
                        help="CNB 仓库路径（org/repo），用于生成同版本下载描述")
    args = parser.parse_args(argv)
    zip_path, sha_path, desc_path = build(
        args.version, Path(args.out), args.github_repo, args.cnb_path)
    print(f"[model-pack] zip: {zip_path}")
    print(f"[model-pack] sha256: {sha_path.read_text(encoding='ascii').strip()}")
    print(f"[model-pack] descriptor: {desc_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
