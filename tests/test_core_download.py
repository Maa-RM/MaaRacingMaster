# -*- coding: utf-8 -*-
"""core.download 传输层回归锁：白名单、跳转、体积、进度、取消与残片清理。

用本地 HTTP 真链路（回环 http 是传输层唯一放行的非 https 场景），不 mock urllib。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from maaracing_master.core.download import (
    DownloadCancelled,
    DownloadError,
    download_file,
    is_allowed_url,
)

PAYLOAD = b"download-payload"


class _Server:
    def __init__(self, body: bytes = PAYLOAD, redirect: str | None = None):
        outer = self
        self.redirect = redirect

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if outer.redirect is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect)
                    self.end_headers()
                    return
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
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/file"

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def test_url_whitelist_rules():
    allowed = {"github.com", "codeload.github.com"}
    assert is_allowed_url("https://github.com/a/b", allowed)
    assert not is_allowed_url("https://github.com.evil.tld/a", allowed)
    assert not is_allowed_url("https://evil.example/a", allowed)
    assert not is_allowed_url("http://github.com/a", allowed)
    assert is_allowed_url("http://127.0.0.1:1234/a", allowed)
    assert not is_allowed_url("https://user:pw@github.com/a", allowed)


def test_download_success_and_progress(tmp_path: Path):
    dest = tmp_path / "out.bin"
    seen: list[tuple[int, int]] = []
    with _Server() as server:
        done = download_file(
            server.url,
            dest,
            allowed_hosts=set(),
            expected_size=len(PAYLOAD),
            on_progress=lambda d, t: seen.append((d, t)),
        )
    assert done == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD
    assert seen[0] == (0, len(PAYLOAD))
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))


def test_size_mismatch_removes_partial_file(tmp_path: Path):
    dest = tmp_path / "out.bin"
    with _Server() as server:
        with pytest.raises(DownloadError):
            download_file(server.url, dest, allowed_hosts=set(), expected_size=3)
    assert not dest.exists()


def test_cancel_removes_partial_file(tmp_path: Path):
    dest = tmp_path / "out.bin"
    state = {"cancel": False}

    def progress(_done, _total):
        state["cancel"] = True

    with _Server() as server:
        with pytest.raises(DownloadCancelled):
            download_file(
                server.url,
                dest,
                allowed_hosts=set(),
                expected_size=len(PAYLOAD),
                on_progress=progress,
                cancelled=lambda: state["cancel"],
            )
    assert not dest.exists()


def test_redirect_to_unknown_host_is_rejected(tmp_path: Path):
    dest = tmp_path / "out.bin"
    with _Server(redirect="http://example.com/x") as server:
        with pytest.raises(DownloadError):
            download_file(server.url, dest, allowed_hosts=set())
    assert not dest.exists()


def test_disallowed_source_rejected_before_request(tmp_path: Path):
    with pytest.raises(DownloadError):
        download_file("http://example.com/x", tmp_path / "out.bin", allowed_hosts=set())


def test_model_installer_uses_core_transport():
    """策略留在插件，传输只在 core；避免同一个下载循环再抄一份。"""
    source = (
        Path(__file__).resolve().parent.parent
        / "maaracing_master" / "plugins" / "infinite_drive" / "model_installer.py"
    ).read_text(encoding="utf-8")
    assert "from maaracing_master.core.download import" in source
    assert "urllib.request" not in source
