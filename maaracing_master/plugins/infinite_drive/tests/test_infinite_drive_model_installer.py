# -*- coding: utf-8 -*-
"""模型一键安装器：本地 HTTP 真链路、故障与取消回归锁。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from maaracing_master.plugins.infinite_drive import model_assets, model_installer

REL = "resources/models/test/model.bin"
PACKED = "test/model.bin"
GOOD = b"good"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(model_assets, "MODEL_MANIFEST", {REL: (len(GOOD), _sha(GOOD))})
    monkeypatch.setattr(model_assets, "DRIVE_MANIFEST", {REL: (len(GOOD), _sha(GOOD))})
    managed = tmp_path / "data" / "infinite_drive" / "models" / "digest"
    monkeypatch.setattr(model_assets, "managed_pack_dir", lambda: managed)
    monkeypatch.setattr(
        model_installer, "module_data_dir",
        lambda module_id, *parts: tmp_path / "data" / module_id / Path(*parts),
    )
    model_assets.clear_cache()
    return managed


def _make_zip(path: Path, members: dict[str, bytes] | None = None) -> Path:
    payload = members or {PACKED: GOOD, "README.md": b"readme",
                          "LICENSE-BDD100K.txt": b"license", "SHA256SUMS": b"sums"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return path


def _descriptor(archive: Path, tmp_path: Path, url: str, **changes) -> Path:
    data = {
        "tag": "v0.0.0-test", "asset": archive.name, "sha256": _sha(archive.read_bytes()),
        "size": archive.stat().st_size, "unpacked_size": 4096,
        "manifest_digest": model_assets.manifest_digest(), "sources": {"local": url},
    }
    data.update(changes)
    path = tmp_path / "download.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _Server:
    def __init__(self, archive: Path, block: threading.Event | None = None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.started.set()
                if outer.block is not None:
                    outer.block.wait(timeout=5)
                body = archive.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/model.zip"

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def _wait(installer, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        status = installer.status()
        if status.phase in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.01)
    raise AssertionError(f"安装任务未在 {limit}s 内结束: {installer.status()}")


def test_install_from_local_source(monkeypatch, tmp_path):
    managed = _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "model.zip")
    with _Server(archive) as server:
        installer = model_installer.ModelInstaller(_descriptor(archive, tmp_path, server.url))
        accepted, _ = installer.start()
        assert accepted
        status = _wait(installer)
    assert status.phase == "done", status.error
    assert (managed / PACKED).read_bytes() == GOOD
    assert model_assets.check_pack() is None


def test_existing_valid_model_skips_download(monkeypatch, tmp_path):
    managed = _patch(monkeypatch, tmp_path)
    target = managed / PACKED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(GOOD)
    model_assets.clear_cache()
    installer = model_installer.ModelInstaller(tmp_path / "missing.json")
    installer.start()
    status = _wait(installer)
    assert status.phase == "done"
    assert target.read_bytes() == GOOD


def test_wrong_zip_hash_fails_without_touching_models(monkeypatch, tmp_path):
    managed = _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "model.zip")
    with _Server(archive) as server:
        descriptor = _descriptor(archive, tmp_path, server.url, sha256="0" * 64)
        installer = model_installer.ModelInstaller(descriptor)
        installer.start()
        status = _wait(installer)
    assert status.phase == "failed" and "SHA256" in (status.error or "")
    assert not (managed / PACKED).exists()


def test_path_traversal_member_is_rejected(monkeypatch, tmp_path):
    managed = _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "evil.zip", {"../evil.txt": b"evil", PACKED: GOOD})
    with _Server(archive) as server:
        installer = model_installer.ModelInstaller(_descriptor(archive, tmp_path, server.url))
        installer.start()
        status = _wait(installer)
    assert status.phase == "failed" and "非法" in (status.error or "")
    assert not (managed / PACKED).exists()
    assert not (tmp_path / "evil.txt").exists()


def test_cancel_stops_download_and_commits_nothing(monkeypatch, tmp_path):
    managed = _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "model.zip")
    blocking = threading.Event()
    with _Server(archive, block=blocking) as server:
        installer = model_installer.ModelInstaller(_descriptor(archive, tmp_path, server.url))
        installer.start()
        assert server.started.wait(timeout=5)
        installer.cancel()
        blocking.set()
        status = _wait(installer)
    assert status.phase == "cancelled", status.error
    assert not (managed / PACKED).exists()


def test_single_flight_reuses_running_task(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "model.zip")
    blocking = threading.Event()
    with _Server(archive, block=blocking) as server:
        installer = model_installer.ModelInstaller(_descriptor(archive, tmp_path, server.url))
        _, first = installer.start()
        _, second = installer.start()
        assert isinstance(first, model_installer.InstallStatus)
        assert isinstance(second, model_installer.InstallStatus)
        assert first.task_id == second.task_id
        assert second.revision == first.revision
        blocking.set()
        _wait(installer)


def test_release_shelf_asset_cdn_redirects_are_allowlisted():
    """发行货架的资产一跳必须放行：字节终点在货架自家的 CDN 上。

    github.com → release-assets.githubusercontent.com（签名查询串）；
    cnb.cool   → asset.cnb.cool/assets/t/<一次性令牌>。
    真实事故：白名单只列货架主机，两源都在 302 处被拒，一键安装对任何用户都失败。
    """
    assert model_installer._url_allowed(
        "https://github.com/owner/repo/releases/download/v1.0.0/pack.zip")
    assert model_installer._url_allowed(
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?x=1")
    assert model_installer._url_allowed(
        "https://cnb.cool/org/repo/-/releases/download/v1.0.0/pack.zip")
    assert model_installer._url_allowed("https://asset.cnb.cool/assets/t/abc123")
    # 白名单之外的跳转目标仍然拒绝（防代理或第三方 CDN 介入）。
    assert not model_installer._url_allowed("https://example.com/pack.zip")
    assert not model_installer._url_allowed("http://asset.cnb.cool/assets/t/abc123")


def test_all_source_errors_are_reported(monkeypatch, tmp_path):
    """两源都失败时逐源留原因，不能只报最后一个——否则前一个源的真实失败被吞。"""
    _patch(monkeypatch, tmp_path)
    archive = _make_zip(tmp_path / "model.zip")

    def _boom(*_args, **_kwargs):
        raise OSError("拒绝下载重定向到未知地址: https://asset.cnb.cool/assets/t/x")

    monkeypatch.setattr(model_installer, "download_file", _boom)
    descriptor = _descriptor(
        archive, tmp_path, "",
        sources={
            "github": "https://github.com/owner/repo/releases/download/v1.0.0/model.zip",
            "cnb": "https://cnb.cool/org/repo/-/releases/download/v1.0.0/model.zip",
        })
    installer = model_installer.ModelInstaller(descriptor)
    installer.start()
    status = _wait(installer)
    assert status.phase == "failed"
    assert "github:" in (status.error or "") and "cnb:" in (status.error or "")
