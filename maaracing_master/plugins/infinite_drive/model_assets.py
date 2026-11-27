# -*- coding: utf-8 -*-
"""车道线驾驶模型包：用户数据目录优先、安装包兜底，带完整性校验。

发布包默认不带 ONNX 权重；要用车道线驾驶时，把模型包解压到
``<用户数据>/data/infinite_drive/models/``（布局与本目录 ``resources/models/`` 同构，
去掉 ``resources/models/`` 前缀）。源码运行或显式全量包仍可直接读包内权重。
车道线驾驶要求全精度车道线模型，模型包同时分发物品感知和半精度线观测权重。

校验口径：尺寸 + SHA256，按（路径、大小、mtime）缓存，同一进程只哈希一次。
模型文件更新时必须同步改 ``MODEL_MANIFEST``；旧下载包会被拒绝并提示重新下载。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from maaracing_master.core.paths import data_dir, module_data_dir
from maaracing_master.plugins.infinite_drive import (
    LANE_FP16_MODEL_REL,
    LANE_MODEL_REL,
    MODELS_DIR,
    PERCEPTION_MODEL_REL,
)

PACK_PREFIX = "resources/models/"
MODULE_ID = "infinite_drive"

# 已安装的旧版受管包仍可提供其中通过现行校验的车道线与街车模型。
_PREVIOUS_PACK_DIGEST = "fc71dcab51857e7c9967fa45e21183549535a12744d12277b27c8cb8c9d72154"

DRIVE_MANIFEST: dict[str, tuple[int, str]] = {
    LANE_MODEL_REL: (
        543156,
        "bd35423f6a8a0071f908b74c1a7592210479e24c3714c7091a708eef95cb4bcc",
    ),
}

MODEL_MANIFEST: dict[str, tuple[int, str]] = {
    **DRIVE_MANIFEST,
    PERCEPTION_MODEL_REL: (
        10665412,
        "0ffc20ae13b334bfeccb74e0eeef00c3e6ba98378ab482170c17f33a13ef06af",
    ),
    LANE_FP16_MODEL_REL: (
        314482,
        "abdf3d46706c0dc259d178df8a27ae23b9c3ce03f658e75aadc391b035bd9e26",
    ),
}

_VERIFIED: dict[tuple[str, int, int], bool] = {}


def model_pack_dir() -> Path:
    """用户手动解压的旧模型包根目录（兼容既有安装，不再作为一键安装落点）。"""
    return data_dir() / MODULE_ID / "models"


def manifest_digest() -> str:
    """模型清单摘要：排序后的 (路径, 尺寸, SHA256) 再取 SHA256。

    清单不变则摘要不变，跨程序版本复用同一安装目录；清单变化才换目录。
    """
    canonical = "\n".join(
        f"{rel}|{size}|{sha}" for rel, (size, sha) in sorted(MODEL_MANIFEST.items())
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def managed_pack_dir() -> Path:
    """一键安装的受管目录：data/<module>/models/<manifest-digest>/。"""
    return module_data_dir(MODULE_ID, "models", manifest_digest())


def previous_pack_dir() -> Path:
    """兼容读旧版受管包的目录；删除模型时与当前受管目录一并清理。"""
    return module_data_dir(MODULE_ID, "models", _PREVIOUS_PACK_DIGEST)


def packed_rel(rel: str) -> str:
    """插件相对路径 → 模型包内相对路径。"""
    return rel[len(PACK_PREFIX):] if rel.startswith(PACK_PREFIX) else rel


def pack_path(rel: str) -> Path:
    return model_pack_dir() / packed_rel(rel)


def installed_path(rel: str) -> Path:
    """一键安装时的目标路径（受管目录内）。"""
    return managed_pack_dir() / packed_rel(rel)


def candidates(rel: str) -> tuple[Path, ...]:
    """候选文件，按优先级：现行受管包、旧受管包、手动解压和包内权重。"""
    relative = packed_rel(rel)
    return (managed_pack_dir() / relative, previous_pack_dir() / relative,
            pack_path(rel), MODELS_DIR / relative)


def resolve(rel: str) -> Path | None:
    """只查存在性（启动期轻量检查，不哈希）。"""
    for path in candidates(rel):
        if path.is_file():
            return path
    return None


def missing() -> tuple[str, ...]:
    """车道线驾驶缺失或大小不符的必需权重。"""
    out: list[str] = []
    for rel, (want_size, _sha) in DRIVE_MANIFEST.items():
        ok = False
        for path in candidates(rel):
            try:
                if path.is_file() and path.stat().st_size == want_size:
                    ok = True
                    break
            except OSError:
                continue
        if not ok:
            out.append(rel)
    return tuple(out)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path | None, rel: str) -> str | None:
    """返回错误文本；None 表示通过。未知 rel 不做校验。"""
    if path is None:
        return f"缺少 {rel}（应放在 {pack_path(rel)}）"
    expected = MODEL_MANIFEST.get(rel)
    if expected is None:
        return None
    want_size, want_hash = expected
    try:
        st = path.stat()
    except OSError as exc:
        return f"无法读取 {path}：{exc}"
    if st.st_size != want_size:
        return (f"{path.name} 大小不符（{st.st_size} != {want_size}），"
                f"请重新下载模型包")
    cache_key = (str(path), st.st_size, st.st_mtime_ns)
    ok = _VERIFIED.get(cache_key)
    if ok is None:
        try:
            ok = _hash_file(path) == want_hash
        except OSError as exc:
            return f"无法读取 {path}：{exc}"
        _VERIFIED[cache_key] = ok
    if not ok:
        return f"{path.name} 校验失败（文件损坏或版本不符），请重新下载模型包"
    return None


def select(rel: str) -> tuple[Path | None, str | None]:
    """按候选顺序选第一个通过校验的文件；返回 (路径, 错误文本)。"""
    errors: list[str] = []
    for path in candidates(rel):
        if not path.is_file():
            continue
        err = verify(path, rel)
        if err is None:
            return path, None
        errors.append(err)
    if errors:
        return None, "；".join(errors)
    return None, f"缺少 {rel}（应放在 {pack_path(rel)}）"


def check_pack() -> str | None:
    """完整校验车道线驾驶的必需权重。"""
    errors = []
    for rel in DRIVE_MANIFEST:
        _path, err = select(rel)
        if err:
            errors.append(err)
    return "；".join(errors) if errors else None


def clear_cache() -> None:
    """测试/热更新用：清空哈希缓存。"""
    _VERIFIED.clear()
