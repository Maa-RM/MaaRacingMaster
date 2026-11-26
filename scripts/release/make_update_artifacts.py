#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便携版更新清单与文件级增量的生成、自验工具。

职责边界（首版）：只处理程序树这一层的字节——扫描、哈希、差分、打包、反向自验；
不做下载、签名和安装事务。签名由发布端决定密钥后另行接入，安装由壳与原生启动器负责。

树的规范（Python 与 C# 必须逐字节一致；改动前先同步两端的测试向量）：
- 相对路径统一用 ``/`` 分隔；
- 文件摘要为文件内容的 SHA-256 小写十六进制；
- 树摘要 = SHA-256(按 UTF-8 字节序对每个相对路径依次拼接
  ``rel.encode("utf-8") + b"\\0" + hex.encode("ascii") + b"\\n"``)；
- ``__pycache__/`` 目录与 ``*.pyc`` 属于运行期缓存，不进发行文件清单，也不参与树摘要；
- 空目录不进清单（目录由文件路径推导）。

命令：
    python scripts/release/make_update_artifacts.py pack --tree <dir> --out <zip>
    python scripts/release/make_update_artifacts.py build --new-tree <dir> --new-zip <zip> \\
        --version X --tag vX [--prev-zip <zip> ...] --out <release_dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA = 1
PLATFORM = "win-x64"
# 安装布局与启动器契约：布局一旦变化（例如把可替换内容收进独立子目录）必须同步升版，
# 客户端的「基线兼容」判据与发布端的起点评选都读这个字段。
COMPAT = {"layout": "program-v1", "launcher_api": 2}
CACHE_DIR = "__pycache__"
CACHE_SUFFIX = ".pyc"
MAX_DELTA_RATIO = 0.30
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

_INVALID_CHARS = set('<>:"|?*')
_RESERVED_NAMES = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


@dataclass(frozen=True)
class FileEntry:
    size: int
    sha256: str


# --------------------------------------------------------------------------- 路径与摘要


def is_runtime_cache(rel: str) -> bool:
    """运行期缓存不参与发行文件清单（发行树里本不该出现，出现也不当成可管理文件）。"""
    parts = rel.lower().split("/")
    return CACHE_DIR in parts or rel.lower().endswith(CACHE_SUFFIX)


def normalize_rel(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def validate_rel_path(rel: str) -> str | None:
    """校验 Windows 相对路径；返回错误说明，None 表示合法。"""
    if not rel:
        return "空路径"
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return f"绝对路径不允许: {rel}"
    if len(rel) > 32000:
        return f"路径超过 32000 字符: {rel[:80]}…"
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            return f"非法路径段 {part!r}: {rel}"
        if part != part.rstrip(" ."):
            return f"路径段以空格或点结尾: {rel}"
        if any(ch in _INVALID_CHARS or ord(ch) < 32 for ch in part):
            return f"路径含非法字符: {rel}"
        if len(part) > 255:
            return f"单个路径段超过 255 字符: {rel}"
        if part.split(".")[0].lower() in _RESERVED_NAMES:
            return f"使用 Windows 保留名: {rel}"
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(files: Mapping[str, FileEntry]) -> str:
    """规范树摘要：按 UTF-8 字节序，逐条拼接 rel\\0hex\\n 后做 SHA-256。"""
    digest = hashlib.sha256()
    for rel in sorted(files, key=lambda item: item.encode("utf-8")):
        digest.update(rel.encode("utf-8") + b"\0" + files[rel].sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------- 扫描


def scan_tree(root: Path) -> dict[str, FileEntry]:
    root = root.resolve()
    files: dict[str, FileEntry] = {}
    case_seen: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name.lower() != CACHE_DIR]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise SystemExit(f"程序树含符号链接，拒绝打包: {path}")
            rel = normalize_rel(path.relative_to(root).as_posix())
            if is_runtime_cache(rel):
                continue
            problem = validate_rel_path(rel)
            if problem:
                raise SystemExit(problem)
            lowered = rel.lower()
            if lowered in case_seen:
                raise SystemExit(f"大小写冲突: {case_seen[lowered]} 与 {rel}")
            case_seen[lowered] = rel
            files[rel] = FileEntry(path.stat().st_size, _sha256_file(path))
    return files


def scan_zip(zip_path: Path, content_subdir: str = "") -> dict[str, FileEntry]:
    prefix = content_subdir.strip("/") + "/" if content_subdir.strip("/") else ""
    files: dict[str, FileEntry] = {}
    case_seen: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = normalize_rel(info.filename)
            if prefix:
                if not rel.startswith(prefix):
                    continue  # 稳定入口等包根文件不属于可替换内容
                rel = rel[len(prefix):]
            if is_runtime_cache(rel):
                continue
            problem = validate_rel_path(rel)
            if problem:
                raise SystemExit(problem)
            lowered = rel.lower()
            if rel in files or lowered in case_seen:
                raise SystemExit(f"ZIP 内重复条目: {case_seen.get(lowered, rel)} 与 {rel}")
            case_seen[lowered] = rel
            digest = hashlib.sha256()
            with archive.open(info) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[rel] = FileEntry(info.file_size, digest.hexdigest())
    return files


# --------------------------------------------------------------------------- 差分与打包


def diff_trees(old: Mapping[str, FileEntry],
               new: Mapping[str, FileEntry]) -> tuple[list[str], list[str], list[str]]:
    added = sorted((rel for rel in new if rel not in old),
                   key=lambda item: item.encode("utf-8"))
    changed = sorted((rel for rel in new if rel in old and new[rel].sha256 != old[rel].sha256),
                     key=lambda item: item.encode("utf-8"))
    removed = sorted((rel for rel in old if rel not in new),
                     key=lambda item: item.encode("utf-8"))
    return added, changed, removed


def _write_zip_member(archive: zipfile.ZipFile, name: str, source: Path) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    with source.open("rb") as src, archive.open(info, "w") as dst:
        shutil.copyfileobj(src, dst)


def write_tree_zip(root: Path, paths: Iterable[str], out_path: Path) -> None:
    """可复现归档：固定成员顺序、时间戳与压缩级别。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(set(paths), key=lambda item: item.encode("utf-8"))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for rel in ordered:
            _write_zip_member(archive, rel, root / rel)


def extract_zip(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = normalize_rel(info.filename)
            problem = validate_rel_path(rel)
            if problem:
                raise SystemExit(f"ZIP 路径不合法，拒绝解包: {problem}")
            target = (dest / rel).resolve()
            if dest not in target.parents and target != dest:
                raise SystemExit(f"ZIP 路径越界，拒绝解包: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def apply_delta(old_root: Path, delta_zip: Path, remove: Iterable[str],
                expected: Mapping[str, FileEntry]) -> list[str]:
    """把增量应用到旧树并逐文件对拍；返回差异说明（空列表表示等价）。"""
    with zipfile.ZipFile(delta_zip) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            rel = normalize_rel(info.filename)
            if rel not in expected:
                return [f"增量含目标清单之外的文件: {rel}"]
            problem = validate_rel_path(rel)
            if problem:
                return [problem]
            target = old_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    for rel in remove:
        path = old_root / rel
        if path.is_file():
            path.unlink()

    actual = scan_tree(old_root)
    errors: list[str] = []
    missing = sorted(set(expected) - set(actual), key=lambda item: item.encode("utf-8"))
    extra = sorted(set(actual) - set(expected), key=lambda item: item.encode("utf-8"))
    if missing:
        errors.append("缺少文件: " + ", ".join(missing[:5]) + (f" 等 {len(missing)} 个" if len(missing) > 5 else ""))
    if extra:
        errors.append("多出文件: " + ", ".join(extra[:5]) + (f" 等 {len(extra)} 个" if len(extra) > 5 else ""))
    mismatched = [rel for rel in expected if rel in actual and actual[rel].sha256 != expected[rel].sha256]
    if mismatched:
        errors.append("内容不一致: " + ", ".join(mismatched[:5])
                      + (f" 等 {len(mismatched)} 个" if len(mismatched) > 5 else ""))
    return errors


# --------------------------------------------------------------------------- 版本


def derive_version(zip_name: str) -> str:
    match = re.fullmatch(r"MaaRacingMaster-(.+)-win-x64\.zip", Path(zip_name).name)
    if not match:
        raise SystemExit(f"无法从资产名解析版本: {zip_name}")
    return match.group(1)


def version_key(version: str) -> tuple:
    """SemVer 口径的排序键：正式版高于同号预发布，数字段按数值比较。"""
    value = version.strip()
    if value.startswith("v"):
        value = value[1:]
    value = value.split("+", 1)[0]
    core, _, pre = value.partition("-")
    numbers = tuple(int(part) for part in core.split(".") if part != "")
    if len(numbers) < 3:
        numbers = numbers + (0,) * (3 - len(numbers))
    if not pre:
        return (numbers, 1, ())
    identifiers: tuple = tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in pre.split(".") if part != ""
    )
    return (numbers, 0, identifiers)


# --------------------------------------------------------------------------- 构建


def _zip_meta(zip_path: Path) -> dict:
    return {
        "file": zip_path.name,
        "size": zip_path.stat().st_size,
        "sha256": _sha256_file(zip_path),
    }


def build(new_tree: Path, new_zip: Path, version: str, tag: str,
          out_dir: Path, prev_zips: list[Path], channel: str,
          max_delta_ratio: float = MAX_DELTA_RATIO,
          max_deltas: int = 3,
          content_subdir: str = "") -> dict:
    subdir = content_subdir.strip("/")
    content_root = new_tree / subdir if subdir else new_tree
    if not content_root.is_dir():
        raise SystemExit(f"可替换内容目录不存在: {content_root}")
    new_files = scan_tree(content_root)
    zip_files = scan_zip(new_zip, subdir)
    new_digest = tree_digest(new_files)
    if tree_digest(zip_files) != new_digest:
        raise SystemExit("完整 ZIP 的内容与程序树不一致——不允许按不同字节发布")

    full = _zip_meta(new_zip)
    full["tree_sha256"] = new_digest
    full["files"] = [
        [rel, new_files[rel].size, new_files[rel].sha256]
        for rel in sorted(new_files, key=lambda item: item.encode("utf-8"))
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    deltas: list[dict] = []
    skipped: list[str] = []
    ordered = sorted(prev_zips, key=lambda item: version_key(derive_version(item.name)),
                     reverse=True)
    for prev_zip in ordered:
        if len(deltas) >= max_deltas:
            break
        prev_zip = prev_zip.resolve()
        if not prev_zip.is_file():
            raise SystemExit(f"基线 ZIP 不存在: {prev_zip}")
        from_version = derive_version(prev_zip.name)
        if version_key(from_version) >= version_key(version):
            skipped.append(f"{from_version}: 不低于目标版本，跳过")
            continue
        with tempfile.TemporaryDirectory(prefix="maarm-delta-") as tmp:
            old_root = Path(tmp) / "old"
            old_root.mkdir()
            extract_zip(prev_zip, old_root)
            old_content = old_root / subdir if subdir else old_root
            if not old_content.is_dir():
                skipped.append(f"{from_version}: 旧包不含 {subdir}/，布局不兼容，跳过")
                continue
            old_files = scan_tree(old_content)
            added, changed, removed = diff_trees(old_files, new_files)
            payload = added + changed

            delta_name = f"MaaRacingMaster-{from_version}-to-{version}-delta.zip"
            delta_path = out_dir / delta_name
            write_tree_zip(content_root, payload, delta_path)
            ratio = delta_path.stat().st_size / full["size"] if full["size"] else 1.0
            if ratio > max_delta_ratio:
                delta_path.unlink()
                skipped.append(f"{from_version}: 增量占整包 {ratio:.1%}，超过门槛 "
                               f"{max_delta_ratio:.0%}，改为整包")
                continue
            errors = apply_delta(old_content, delta_path, removed, new_files)
            if errors:
                raise SystemExit(f"增量自验失败（{from_version} → {version}）: " + "; ".join(errors))

        meta = _zip_meta(delta_path)
        deltas.append({
            "from_version": from_version,
            "from_tree_sha256": tree_digest(old_files),
            "file": meta["file"],
            "size": meta["size"],
            "sha256": meta["sha256"],
            "tree_sha256": new_digest,
            "remove": removed,
            "ratio": round(ratio, 6),
        })

    manifest = {
        "schema": SCHEMA,
        "tag": tag,
        "version": version,
        "channel": channel,
        "platform": PLATFORM,
        "compat": dict(COMPAT),
        "full": full,
        "deltas": deltas,
        "limits": {"max_delta_ratio": max_delta_ratio},
    }
    manifest_path = out_dir / f"MaaRacingMaster-{version}-update.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return {"manifest": str(manifest_path), "deltas": deltas, "skipped": skipped,
            "full": {"file": full["file"], "size": full["size"], "sha256": full["sha256"],
                     "tree_sha256": new_digest}}


def pack(tree: Path, out_zip: Path) -> None:
    files = scan_tree(tree)
    write_tree_zip(tree, files.keys(), out_zip)
    print(f"[update-artifacts] zip: {out_zip}")
    print(f"[update-artifacts] sha256: {_sha256_file(out_zip)}")
    print(f"[update-artifacts] tree_sha256: {tree_digest(files)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="便携版更新清单与文件级增量")
    sub = parser.add_subparsers(dest="command", required=True)

    pack_parser = sub.add_parser("pack", help="把程序树打成可复现 ZIP（演练用）")
    pack_parser.add_argument("--tree", required=True)
    pack_parser.add_argument("--out", required=True)

    build_parser = sub.add_parser("build", help="生成更新清单与直接增量并自验")
    build_parser.add_argument("--new-tree", required=True, help="新版本程序树目录")
    build_parser.add_argument("--new-zip", required=True, help="新版本完整 ZIP")
    build_parser.add_argument("--version", required=True)
    build_parser.add_argument("--tag", default="")
    build_parser.add_argument("--channel", default="")
    build_parser.add_argument("--prev-zip", action="append", default=[], help="可重复：基线完整 ZIP")
    build_parser.add_argument("--out", required=True)
    build_parser.add_argument("--max-delta-ratio", type=float, default=MAX_DELTA_RATIO)
    build_parser.add_argument("--max-deltas", type=int, default=3,
                              help="最多生成的直接增量个数（默认 3）")
    build_parser.add_argument("--content-subdir", default="",
                              help="可替换内容在包内的子目录（如 program）；清单与增量路径相对它")

    args = parser.parse_args(argv)
    if args.command == "pack":
        pack(Path(args.tree), Path(args.out))
        return 0

    version = args.version[1:] if args.version.startswith("v") else args.version
    tag = args.tag or f"v{version}"
    channel = args.channel or ("preview" if "-" in version else "stable")
    result = build(
        new_tree=Path(args.new_tree),
        new_zip=Path(args.new_zip),
        version=version,
        tag=tag,
        out_dir=Path(args.out),
        prev_zips=[Path(item) for item in args.prev_zip],
        channel=channel,
        max_delta_ratio=args.max_delta_ratio,
        max_deltas=args.max_deltas,
        content_subdir=args.content_subdir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
