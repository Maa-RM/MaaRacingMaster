# -*- coding: utf-8 -*-
"""带主机白名单、进度与取消的 HTTPS 传输（纯搬字节，不含安装语义）。

只做一件事：把 URL 下载到目标文件。校验、解压、落点提交仍归调用方——
插件资产安装用自己的 SHA256/解压/原子提交，宿主运行时更新用微软签名与安装器，
两者只有「安全地把字节搬到临时文件」这一段相同。

安全边界：
- 默认只接受 https；http 仅对回环地址放行，供测试服务使用；
- 跳转逐跳校验，重定向到白名单外的主机立即中止；
- 不接受带用户名/密码的 URL；
- 失败时删除半成品文件，不在磁盘上留残片。
"""
from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable, Collection
from pathlib import Path
from urllib.parse import urlsplit

CHUNK_SIZE = 1024 * 1024


class DownloadError(OSError):
    """下载失败：网络、体积不符或超时。"""


class DownloadCancelled(DownloadError):
    """调用方取消。"""


def is_allowed_url(url: object, allowed_hosts: Collection[str]) -> bool:
    """URL 是否落在允许的主机集合内；http 只对回环地址放行。"""
    parts = urlsplit(str(url))
    if parts.username or parts.password:
        return False
    if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}:
        return True
    return parts.scheme == "https" and parts.hostname in allowed_hosts


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """只接受白名单主机与 https（或回环 http），拒绝跨到未知 CDN 或代理。"""

    def __init__(self, allowed_hosts: Collection[str]):
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_url(newurl, self._allowed_hosts):
            raise DownloadError(f"拒绝下载重定向到未知地址: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_file(
    url: str,
    dest: Path,
    *,
    allowed_hosts: Collection[str],
    expected_size: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout_s: float = 20.0,
    deadline_s: float = 600.0,
    chunk_size: int = CHUNK_SIZE,
    user_agent: str = "MaaRacingMaster/download",
) -> int:
    """把 url 下载到 dest，返回字节数。

    expected_size 非空时要求字节数与声明完全一致（超出或不足都失败）。
    on_progress(done, total) 在开始与每个分块后回调；total 优先取 Content-Length。
    cancelled() 返回 True 时抛 DownloadCancelled。
    """
    if not is_allowed_url(url, allowed_hosts):
        raise DownloadError(f"下载源不合法: {url}")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    should_cancel = cancelled or (lambda: False)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    opener = urllib.request.build_opener(_RedirectGuard(allowed_hosts))
    deadline = time.monotonic() + deadline_s

    try:
        with opener.open(request, timeout=timeout_s) as response, dest.open("wb") as out:
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            if on_progress is not None:
                on_progress(0, total)
            done = 0
            while True:
                if should_cancel():
                    raise DownloadCancelled("下载已取消")
                if time.monotonic() > deadline:
                    raise DownloadError("下载超时")
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                done += len(chunk)
                if expected_size is not None and done > expected_size:
                    raise DownloadError("下载字节数超过声明大小")
                out.write(chunk)
                if on_progress is not None:
                    on_progress(done, total)
            if expected_size is not None and done != expected_size:
                raise DownloadError("下载字节数与声明不符")
            return done
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
