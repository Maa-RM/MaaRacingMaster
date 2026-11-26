# -*- coding: utf-8 -*-
"""发布后冒烟：两个发行货架的模型包直链，按「运行时判据」真走一遍。

判据只有一条来源——生产代码本身：每一跳都用 `core.download.is_allowed_url` 对
`ModelInstaller.ALLOWED_HOSTS` 判定。冒烟里再抄一份主机名单，等于给同一事实
建第二份真相，改一边漏一边。

只取 Range 前 16 字节，不下载整包：本脚本回答「能不能走通、两个源报的字节数是否
一致」；跨源字节同一性由 cnb-release 上传前的 sha256 比对负责。

    python scripts/release/smoke_model_sources.py \
        --tag v0.32.4-dev.2 \
        --github-repo Maa-RM/MaaRacingMaster \
        --cnb-path MaaRacingMaster/MAIN
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from maaracing_master.core.download import is_allowed_url  # noqa: E402
from maaracing_master.plugins.infinite_drive import model_installer  # noqa: E402

ASSET = "MaaRacingMaster-infinite_drive-models-{ver}.zip"
UA = "MaaRacingMaster/model-install"
TIMEOUT_S = 25
DEFAULT_ATTEMPTS = 3


class HopOutsideAllowlist(RuntimeError):
    """跳转落到白名单外的主机：与客户端同一条判据，不另立标准。"""


class _Guard(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.hops: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append(newurl)
        if not is_allowed_url(newurl, model_installer.ALLOWED_HOSTS):
            raise HopOutsideAllowlist(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_sources(tag: str, github_repo: str, cnb_path: str,
                  asset: str | None = None) -> dict[str, str]:
    name = asset or ASSET.format(ver=tag.lstrip("v"))
    sources: dict[str, str] = {}
    if cnb_path:
        sources["cnb"] = f"https://cnb.cool/{cnb_path}/-/releases/download/{tag}/{name}"
    if github_repo:
        sources["github"] = f"https://github.com/{github_repo}/releases/download/{tag}/{name}"
    return sources


def _total_bytes(response) -> int | None:
    content_range = response.headers.get("Content-Range") or ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def probe(url: str, attempts: int = DEFAULT_ATTEMPTS) -> dict:
    """按运行时判据走一遍；传输层抖动退避重试，策略拒绝不重试。"""
    last: dict = {}
    for attempt in range(attempts):
        guard = _Guard()
        opener = urllib.request.build_opener(guard)
        request = urllib.request.Request(
            url, headers={"User-Agent": UA, "Range": "bytes=0-15"})
        result = {"status": None, "bytes": None, "hops": [], "error": ""}
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                result.update(status=int(response.status),
                              bytes=_total_bytes(response),
                              hops=list(guard.hops))
                response.read(16)
        except HopOutsideAllowlist as exc:
            result.update(hops=list(guard.hops), error=f"跳转不在白名单: {exc}")
            return result  # 策略拒绝：重试没有意义
        except urllib.error.HTTPError as exc:
            result.update(status=int(exc.code), hops=list(guard.hops),
                          error=f"HTTP {exc.code}")
        except Exception as exc:
            result.update(hops=list(guard.hops),
                          error=f"{type(exc).__name__}: {exc}")
        last = result
        if result["status"] is not None and result["status"] not in (429, 500, 502, 503, 504):
            return result
        time.sleep(1.0 * (attempt + 1))
    return last


def judge(results: dict[str, dict], expect_size: int | None = None) -> list[str]:
    """判定并返回问题清单；空列表 = 冒烟通过。"""
    problems: list[str] = []
    for name, row in results.items():
        error = row.get("error") or ""
        if "跳转不在白名单" in error:
            problems.append(f"{name}: {error}")
            continue
        status = row.get("status")
        if status in (404, 410):
            problems.append(f"{name}: 资产不存在（HTTP {status}）")
            continue
        if status not in (200, 206):
            problems.append(f"{name}: 未拿到字节（{error or f'HTTP {status}'}）")
            continue
        if not row.get("bytes"):
            problems.append(f"{name}: 响应没有总字节数")
            continue
        if expect_size is not None and row["bytes"] != expect_size:
            problems.append(
                f"{name}: 字节数 {row['bytes']} != 本地 {expect_size}")
    sizes = {row["bytes"] for row in results.values()
             if row.get("bytes") and row.get("status") in (200, 206)}
    if len(sizes) > 1:
        problems.append(f"两个源报的字节数不一致: {sorted(sizes)}")
    return problems


def run(tag: str, github_repo: str, cnb_path: str, attempts: int,
        expect_size: int | None) -> int:
    sources = build_sources(tag, github_repo, cnb_path)
    if not sources:
        print("[smoke] 没有可探测的源（--github-repo/--cnb-path 至少给一个）")
        return 2
    results: dict[str, dict] = {}
    for name, url in sources.items():
        results[name] = probe(url, attempts=attempts)
        row = results[name]
        chain = " -> ".join(h.split("?")[0] for h in row["hops"]) or "（无跳转）"
        print(f"[smoke] {name:6s} status={row['status']} bytes={row['bytes']} "
              f"hops={chain} {row['error']}")
    problems = judge(results, expect_size)
    if problems:
        print("[smoke] FAIL")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("[smoke] OK：两源都按运行时判据走通，字节数一致")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="形如 v0.32.4-dev.2")
    parser.add_argument("--github-repo", default="", help="owner/repo")
    parser.add_argument("--cnb-path", default="", help="org/repo")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--expect-size", type=int, default=None,
                        help="本地模型包字节数；给了就要求与远端一致")
    args = parser.parse_args(argv)
    return run(args.tag, args.github_repo, args.cnb_path, args.attempts, args.expect_size)


if __name__ == "__main__":
    raise SystemExit(main())
