# -*- coding: utf-8 -*-
"""深度模型包（可选 DLC）的解析与校验回归锁。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from maaracing_master.plugins.infinite_drive import model_assets


REL = "resources/models/test/model.bin"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch_dirs(monkeypatch, tmp_path):
    pack = tmp_path / "pack"
    packaged = tmp_path / "packaged"
    managed = tmp_path / "managed"
    monkeypatch.setattr(model_assets, "model_pack_dir", lambda: pack)
    monkeypatch.setattr(model_assets, "managed_pack_dir", lambda: managed)
    monkeypatch.setattr(model_assets, "MODELS_DIR", packaged)
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {REL: (4, _sha(b"good"))})
    monkeypatch.setattr(model_assets, "DEPTH_MANIFEST", {REL: (4, _sha(b"good"))})
    model_assets.clear_cache()
    return pack, packaged, managed


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_pack_layout_strips_resources_prefix(monkeypatch, tmp_path):
    pack = tmp_path / "models"
    monkeypatch.setattr(model_assets, "model_pack_dir", lambda: pack)
    assert model_assets.packed_rel("resources/models/perception/model.onnx") == \
        "perception/model.onnx"
    assert model_assets.pack_path("resources/models/perception/model.onnx") == \
        pack / "perception" / "model.onnx"


def test_resolve_prefers_user_pack(monkeypatch, tmp_path):
    pack, packaged, _managed = _patch_dirs(monkeypatch, tmp_path)
    user_file = _write(model_assets.pack_path(REL), b"good")
    _write(packaged / "test" / "model.bin", b"good")
    assert model_assets.resolve(REL) == user_file


def test_resolve_falls_back_to_packaged(monkeypatch, tmp_path):
    pack, packaged, _managed = _patch_dirs(monkeypatch, tmp_path)
    packed_file = _write(packaged / "test" / "model.bin", b"good")
    assert model_assets.resolve(REL) == packed_file


def test_missing_and_check_pack_report_install_path(monkeypatch, tmp_path):
    pack, _packaged, _managed = _patch_dirs(monkeypatch, tmp_path)
    assert model_assets.missing() == (REL,)
    err = model_assets.check_pack()
    assert err and str(pack / "test" / "model.bin") in err


def test_verify_accepts_good_file_and_rejects_size_and_hash(monkeypatch, tmp_path):
    pack, _packaged, _managed = _patch_dirs(monkeypatch, tmp_path)
    good = _write(model_assets.pack_path(REL), b"good")
    assert model_assets.verify(good, REL) is None

    wrong_size = _write(model_assets.pack_path(REL), b"too-long")
    assert "大小不符" in (model_assets.verify(wrong_size, REL) or "")

    wrong_hash = _write(model_assets.pack_path(REL), b"evil")
    model_assets.clear_cache()      # 同尺寸重写可能命中同一 mtime 刻度
    assert "校验失败" in (model_assets.verify(wrong_hash, REL) or "")


def test_select_skips_corrupt_user_pack_and_uses_packaged(monkeypatch, tmp_path):
    pack, packaged, _managed = _patch_dirs(monkeypatch, tmp_path)
    _write(model_assets.pack_path(REL), b"evil")
    good = _write(packaged / "test" / "model.bin", b"good")
    path, err = model_assets.select(REL)
    assert err is None and path == good


def test_lane_model_is_optional_pack_member():
    """线观测随可选模型包分发，但不参与深度档的启动门禁。"""
    assert model_assets.LANE_MODEL_REL in model_assets.LANE_MANIFEST
    assert model_assets.LANE_MODEL_REL not in model_assets.DEPTH_MANIFEST
    assert set(model_assets.MODEL_MANIFEST) == (
        set(model_assets.DEPTH_MANIFEST) | set(model_assets.LANE_MANIFEST))


def test_manifest_digest_stable_for_same_manifest_and_changes_with_it(monkeypatch):
    base = dict(model_assets.MODEL_MANIFEST)
    digest = model_assets.manifest_digest()
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", dict(reversed(list(base.items()))))
    assert model_assets.manifest_digest() == digest
    rel, (size, sha) = next(iter(base.items()))
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {**base, rel: (size, "0" * 64)})
    assert model_assets.manifest_digest() != digest


def test_managed_dir_is_module_scoped_by_digest(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        model_assets, "module_data_dir",
        lambda *parts: (calls.append(parts), tmp_path.joinpath(*parts))[1],
    )
    path = model_assets.managed_pack_dir()
    assert calls == [("infinite_drive", "models", model_assets.manifest_digest())]
    assert path == tmp_path / "infinite_drive" / "models" / model_assets.manifest_digest()


def test_candidates_prefer_managed_then_legacy_then_packaged(monkeypatch, tmp_path):
    pack, packaged, managed = _patch_dirs(monkeypatch, tmp_path)
    legacy_file = _write(pack / "test" / "model.bin", b"good")
    packaged_file = _write(packaged / "test" / "model.bin", b"good")
    assert model_assets.resolve(REL) == legacy_file

    managed_file = _write(managed / "test" / "model.bin", b"good")
    model_assets.clear_cache()
    assert model_assets.resolve(REL) == managed_file

    managed_file.unlink()
    model_assets.clear_cache()
    assert model_assets.resolve(REL) == legacy_file

    legacy_file.unlink()
    model_assets.clear_cache()
    assert model_assets.resolve(REL) == packaged_file


def test_select_falls_back_to_legacy_when_managed_copy_is_corrupt(monkeypatch, tmp_path):
    pack, _packaged, managed = _patch_dirs(monkeypatch, tmp_path)
    _write(managed / "test" / "model.bin", b"evil")
    legacy = _write(pack / "test" / "model.bin", b"good")
    path, err = model_assets.select(REL)
    assert err is None and path == legacy
