# -*- coding: utf-8 -*-
"""构建生成的模型下载描述：同 tag 资产绑定与运行时兼容性回归锁。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

from maaracing_master.plugins.infinite_drive import model_assets, model_installer

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release" / "make_model_pack.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("make_model_pack_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_descriptor_binds_tag_asset_hash_and_digest(monkeypatch, tmp_path):
    module = _load_script()
    rel = "resources/models/fake/model.bin"
    src = tmp_path / "src" / "fake" / "model.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"model")
    (tmp_path / "src" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "src" / "LICENSE-BDD100K.txt").write_text("license", encoding="utf-8")
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {rel: (5, hashlib.sha256(b"model").hexdigest())})
    monkeypatch.setattr(model_assets, "MODELS_DIR", tmp_path / "src")
    monkeypatch.setattr(model_assets, "verify", lambda path, rel: None)
    monkeypatch.setattr(module, "model_assets", model_assets)

    zip_path, sha_path, desc_path = module.build(
        "v1.2.3", tmp_path / "out", "owner/repo", "org/repo")
    data = json.loads(desc_path.read_text(encoding="utf-8"))

    assert data["tag"] == "v1.2.3"
    assert data["asset"] == zip_path.name
    assert data["sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert data["sha256"] in sha_path.read_text(encoding="ascii")
    assert data["size"] == zip_path.stat().st_size
    assert data["unpacked_size"] >= 5
    assert data["manifest_digest"] == model_assets.manifest_digest()
    assert data["sources"]["github"] == (
        f"https://github.com/owner/repo/releases/download/v1.2.3/{zip_path.name}")
    assert data["sources"]["cnb"] == (
        f"https://cnb.cool/org/repo/-/releases/download/v1.2.3/{zip_path.name}")
    # 顺序即运行时首选顺序：cnb 在前（国内直连稳定），github 兜底。
    assert list(data["sources"]) == ["cnb", "github"]


def test_declared_unpacked_size_covers_every_member(monkeypatch, tmp_path):
    """描述的展开尺寸必须覆盖包内全部成员（含 SHA256SUMS）。

    安装器对解压总量做严格上限判定，构建端少算一个成员，真实安装就会在解压
    末尾报「模型包展开尺寸超过下载描述声明」——这里直接走生产解压路径回归。
    """
    module = _load_script()
    rel = "resources/models/fake/model.bin"
    src = tmp_path / "src" / "fake" / "model.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"model")
    (tmp_path / "src" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "src" / "LICENSE-BDD100K.txt").write_text("license", encoding="utf-8")
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {rel: (5, hashlib.sha256(b"model").hexdigest())})
    monkeypatch.setattr(model_assets, "MODELS_DIR", tmp_path / "src")
    monkeypatch.setattr(model_assets, "verify", lambda path, rel: None)
    monkeypatch.setattr(module, "model_assets", model_assets)

    zip_path, _sha_path, desc_path = module.build("v1.2.3", tmp_path / "out")
    data = json.loads(desc_path.read_text(encoding="utf-8"))

    extracted = tmp_path / "extracted"
    model_installer.ModelInstaller._extract(zip_path, extracted, data)
    actual = sum(p.stat().st_size for p in extracted.rglob("*") if p.is_file())
    assert data["unpacked_size"] == actual


def test_archive_is_reproducible_across_source_mtime(monkeypatch, tmp_path):
    module = _load_script()
    rel = "resources/models/fake/model.bin"
    src = tmp_path / "src" / "fake" / "model.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"model")
    (tmp_path / "src" / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "src" / "LICENSE-BDD100K.txt").write_text("license", encoding="utf-8")
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {rel: (5, hashlib.sha256(b"model").hexdigest())})
    monkeypatch.setattr(model_assets, "MODELS_DIR", tmp_path / "src")
    monkeypatch.setattr(model_assets, "verify", lambda path, rel: None)
    monkeypatch.setattr(module, "model_assets", model_assets)

    first, _, _ = module.build("v1.2.3", tmp_path / "out1")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    second, _, _ = module.build("v1.2.3", tmp_path / "out2")
    assert first.read_bytes() == second.read_bytes()
