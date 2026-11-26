# -*- coding: utf-8 -*-
"""便携版更新清单与文件级增量的回归锁。

锁住跨语言协议里最容易漂的几件事：
1. 树摘要的规范化（UTF-8 字节序、分隔与换行、缓存排除）——C# 端要逐字节复刻；
2. 差分的三种变化（新增 / 修改 / 删除）能应用到旧树并与新树逐文件相等；
3. 增量 ZIP 可复现（同样输入同样字节），体积门槛触发时不出增量；
4. 发布清单不自引用、ZIP 与程序树不一致时拒绝发布；
5. 版本排序：预发布按数值比较，正式版高于同号预发布。
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release" / "make_update_artifacts.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_update_artifacts_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclass 解析注解时需要模块已登记
    spec.loader.exec_module(module)
    return module


art = _load()


def _put(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        _put(root, rel, data)
    return root


# --------------------------------------------------------------------------- 树摘要


def test_tree_digest_matches_cross_language_vector():
    """固定向量：路径顺序、分隔符、换行和摘要编码都必须与 C# 端一致。"""
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    files = {
        "lib/b.bin": art.FileEntry(3, sha(bytes([0, 1, 2]))),
        "a.txt": art.FileEntry(6, sha(b"hello\n")),
    }
    assert art.tree_digest(files) == \
        "bd9f746716f48e74ae8c7f97dc10566de195d3e92daae999dfd722b19a3de05f"


def test_tree_digest_sorts_by_utf8_bytes_not_locale(tmp_path: Path):
    first = _tree(tmp_path / "one", {"a.txt": b"1", "b.txt": b"2"})
    second = _tree(tmp_path / "two", {"b.txt": b"2", "a.txt": b"1"})
    assert art.tree_digest(art.scan_tree(first)) == art.tree_digest(art.scan_tree(second))


def test_runtime_cache_is_not_a_release_file(tmp_path: Path):
    root = _tree(tmp_path / "t", {
        "a.py": b"print(1)",
        "__pycache__/a.cpython-311.pyc": b"\x00",
        "pkg/__pycache__/b.pyc": b"\x00",
        "pkg/c.pyc": b"\x00",
    })
    files = art.scan_tree(root)
    assert set(files) == {"a.py"}


# --------------------------------------------------------------------------- 差分与自验


def test_delta_roundtrip_adds_changes_and_removes(tmp_path: Path):
    old_root = _tree(tmp_path / "old", {
        "keep.txt": b"same",
        "change.txt": b"old",
        "remove.txt": b"gone",
    })
    new_root = _tree(tmp_path / "new", {
        "keep.txt": b"same",
        "change.txt": b"new",
        "add.txt": b"added",
    })
    old, new = art.scan_tree(old_root), art.scan_tree(new_root)
    added, changed, removed = art.diff_trees(old, new)
    assert (added, changed, removed) == (["add.txt"], ["change.txt"], ["remove.txt"])

    delta = tmp_path / "delta.zip"
    art.write_tree_zip(new_root, added + changed, delta)
    errors = art.apply_delta(old_root, delta, removed, new)
    assert errors == []
    assert art.tree_digest(art.scan_tree(old_root)) == art.tree_digest(new)


def test_apply_delta_reports_content_mismatch(tmp_path: Path):
    old_root = _tree(tmp_path / "old", {"change.txt": b"old", "keep.txt": b"keep"})
    new_root = _tree(tmp_path / "new", {"change.txt": b"new", "keep.txt": b"keep"})
    new = art.scan_tree(new_root)
    delta = tmp_path / "delta.zip"
    art.write_tree_zip(new_root, ["change.txt"], delta)
    assert art.apply_delta(old_root, delta, [], new) == []
    (old_root / "keep.txt").write_bytes(b"locally modified")
    errors = art.apply_delta(old_root, delta, [], new)
    assert errors and "内容不一致" in errors[0]


def test_delta_zip_is_reproducible(tmp_path: Path):
    new_root = _tree(tmp_path / "new", {"b.txt": b"2", "a.txt": b"1"})
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    art.write_tree_zip(new_root, ["b.txt", "a.txt"], one)
    art.write_tree_zip(new_root, ["a.txt", "b.txt"], two)
    assert one.read_bytes() == two.read_bytes()


# --------------------------------------------------------------------------- 发布清单


def _release(tmp_path: Path, version: str = "1.1.0",
             payload: bytes = b"payload") -> tuple[Path, Path]:
    tree = _tree(tmp_path / f"tree-{version}", {
        "MaaRacingMaster.exe": b"stub",
        "program/app.dll": payload,
    })
    zip_path = tmp_path / f"MaaRacingMaster-{version}-win-x64.zip"
    art.write_tree_zip(tree, art.scan_tree(tree).keys(), zip_path)
    return tree, zip_path


def test_build_manifest_shape_and_no_self_reference(tmp_path: Path):
    tree, zip_path = _release(tmp_path)
    old_tree, old_zip = _release(tmp_path, "1.0.0", payload=b"older payload")
    out = tmp_path / "out"
    result = art.build(tree, zip_path, "1.1.0", "v1.1.0", out, [old_zip], "stable",
                       max_delta_ratio=1.0)

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema"] == art.SCHEMA
    assert manifest["tag"] == "v1.1.0"
    assert manifest["channel"] == "stable"
    assert manifest["compat"] == art.COMPAT
    assert manifest["full"]["file"] == zip_path.name
    assert manifest["full"]["tree_sha256"] == art.tree_digest(art.scan_tree(tree))
    assert [item[0] for item in manifest["full"]["files"]] == ["MaaRacingMaster.exe", "program/app.dll"]
    assert "sha256" not in manifest  # 清单不自引用自身哈希

    assert len(manifest["deltas"]) == 1
    delta = manifest["deltas"][0]
    assert delta["from_version"] == "1.0.0"
    assert delta["from_tree_sha256"] == art.tree_digest(art.scan_tree(old_tree))
    assert delta["tree_sha256"] == manifest["full"]["tree_sha256"]
    assert delta["remove"] == []
    assert (out / delta["file"]).is_file()


def test_manifest_carries_release_notes_only_when_provided(tmp_path: Path):
    """更新说明随清单下发；未提供时不写这个键（空串与缺省在客户端是同一件事）。"""
    tree, zip_path = _release(tmp_path)
    with_notes = art.build(tree, zip_path, "1.1.0", "v1.1.0", tmp_path / "out-notes", [],
                           "preview", notes="**提速**\n\n- 修复登出后再登录丢配置")
    manifest = json.loads(Path(with_notes["manifest"]).read_text(encoding="utf-8"))
    assert manifest["notes"] == "**提速**\n\n- 修复登出后再登录丢配置"

    without = art.build(tree, zip_path, "1.1.0", "v1.1.0", tmp_path / "out-plain", [],
                        "preview")
    manifest = json.loads(Path(without["manifest"]).read_text(encoding="utf-8"))
    assert "notes" not in manifest


def test_build_rejects_zip_that_differs_from_tree(tmp_path: Path):
    tree, zip_path = _release(tmp_path, "1.1.0")
    _other_tree, other_zip = _release(tmp_path, "9.9.9", payload=b"something else")
    with pytest.raises(SystemExit):
        art.build(tree, other_zip, "1.1.0", "v1.1.0", tmp_path / "out", [], "stable")


def test_delta_over_threshold_is_not_emitted(tmp_path: Path):
    old_tree = _tree(tmp_path / "old", {"blob.bin": b"A" * 4096})
    new_tree = _tree(tmp_path / "new", {"blob.bin": b"B" * 4096})
    old_zip = tmp_path / "MaaRacingMaster-1.0.0-win-x64.zip"
    new_zip = tmp_path / "MaaRacingMaster-1.1.0-win-x64.zip"
    art.write_tree_zip(old_tree, ["blob.bin"], old_zip)
    art.write_tree_zip(new_tree, ["blob.bin"], new_zip)

    out = tmp_path / "out"
    result = art.build(new_tree, new_zip, "1.1.0", "v1.1.0", out, [old_zip], "stable")
    assert result["deltas"] == []
    assert any("超过门槛" in item for item in result["skipped"])
    assert not list(out.glob("*delta.zip"))


def test_build_skips_baseline_not_older_than_target(tmp_path: Path):
    tree, zip_path = _release(tmp_path, "1.1.0")
    same_tree, same_zip = _release(tmp_path, "1.1.0")
    result = art.build(tree, zip_path, "1.1.0", "v1.1.0", tmp_path / "out", [same_zip], "stable")
    assert result["deltas"] == []
    assert any("不低于目标版本" in item for item in result["skipped"])


def test_build_keeps_only_latest_baselines(tmp_path: Path):
    releases = {}
    for version in ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]:
        tree = _tree(tmp_path / f"tree-{version}", {"app.bin": version.encode()})
        zip_path = tmp_path / f"MaaRacingMaster-{version}-win-x64.zip"
        art.write_tree_zip(tree, ["app.bin"], zip_path)
        releases[version] = (tree, zip_path)
    new_tree, new_zip = releases["1.4.0"]
    prev = [releases[version][1] for version in ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]]
    result = art.build(new_tree, new_zip, "1.4.0", "v1.4.0", tmp_path / "out", prev, "stable",
                       max_delta_ratio=1.0, max_deltas=3)
    assert [item["from_version"] for item in result["deltas"]] == ["1.3.0", "1.2.0", "1.1.0"]


def _packaged(tmp_path: Path, version: str, payload: bytes, *,
              subdir: str = "program") -> tuple[Path, Path]:
    """模拟正式发行包：根放稳定入口，可替换内容放子目录。"""
    stage = tmp_path / f"stage-{version}"
    _put(stage, "MaaRacingMaster.exe", b"launcher")
    _put(stage, f"{subdir}/app/a.dll", payload)
    zip_path = tmp_path / f"MaaRacingMaster-{version}-win-x64.zip"
    art.write_tree_zip(stage, art.scan_tree(stage).keys(), zip_path)
    return stage, zip_path


def test_build_with_content_subdir_keeps_launcher_out_of_manifest(tmp_path: Path):
    stage, zip_path = _packaged(tmp_path, "1.0.0", b"a")
    result = art.build(stage, zip_path, "1.0.0", "v1.0.0", tmp_path / "out", [], "stable",
                       content_subdir="program")
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert [item[0] for item in manifest["full"]["files"]] == ["app/a.dll"]
    assert "MaaRacingMaster.exe" not in json.dumps(manifest)


def test_delta_with_content_subdir_uses_content_relative_paths(tmp_path: Path):
    old_stage, old_zip = _packaged(tmp_path, "0.9.0", b"old")
    new_stage, new_zip = _packaged(tmp_path, "1.0.0", b"new")
    result = art.build(new_stage, new_zip, "1.0.0", "v1.0.0", tmp_path / "out",
                       [old_zip], "stable", max_delta_ratio=1.0, content_subdir="program")
    assert len(result["deltas"]) == 1
    delta_zip = tmp_path / "out" / result["deltas"][0]["file"]
    with zipfile.ZipFile(delta_zip) as archive:
        assert archive.namelist() == ["app/a.dll"]


def test_baseline_without_content_subdir_is_skipped(tmp_path: Path):
    flat = _tree(tmp_path / "flat", {"app/a.dll": b"old"})
    flat_zip = tmp_path / "MaaRacingMaster-0.9.0-win-x64.zip"
    art.write_tree_zip(flat, ["app/a.dll"], flat_zip)
    stage, zip_path = _packaged(tmp_path, "1.0.0", b"new")
    result = art.build(stage, zip_path, "1.0.0", "v1.0.0", tmp_path / "out",
                       [flat_zip], "stable", content_subdir="program")
    assert result["deltas"] == []
    assert any("布局不兼容" in item for item in result["skipped"])


# --------------------------------------------------------------------------- 路径与版本


@pytest.mark.parametrize("rel", [
    "../escape.txt",
    "/abs.txt",
    "C" + ":/drive.txt",
    "bad:name.txt",
    "trailingdot.",
    "trailing space ",
    "con.txt",
    "sub/aux.log",
    "nul",
])
def test_validate_rel_path_rejects_windows_hostile_names(rel: str):
    assert art.validate_rel_path(rel) is not None


def test_validate_rel_path_accepts_normal_names():
    assert art.validate_rel_path("runtime/python/packages/numpy/core.py") is None
    assert art.validate_rel_path("中文 目录/文件.txt") is None


def test_scan_zip_rejects_case_collision(tmp_path: Path):
    zip_path = tmp_path / "c.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("A.txt", b"1")
        archive.writestr("a.txt", b"2")
    with pytest.raises(SystemExit):
        art.scan_zip(zip_path)


def test_version_ordering():
    assert art.version_key("0.32.2-dev.2") < art.version_key("0.32.3-dev.1")
    assert art.version_key("0.32.3-dev.9") < art.version_key("0.32.3-dev.10")
    assert art.version_key("0.32.3-dev.1") < art.version_key("0.32.3")
    assert art.version_key("v0.32.3-dev.1") == art.version_key("0.32.3-dev.1")


def test_cli_pack_roundtrip(tmp_path: Path):
    tree = _tree(tmp_path / "tree", {"a.txt": b"1"})
    out = tmp_path / "out.zip"
    proc = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(SCRIPT), "pack",
         "--tree", str(tree), "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    assert "tree_sha256" in proc.stdout
