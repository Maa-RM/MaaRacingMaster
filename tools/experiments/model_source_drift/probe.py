# -*- coding: utf-8 -*-
"""后端探针：把「每个 tag × 每个源」的真实跳转链抓下来，再拿三套判据去判。

只读网络（Range 取前 16 字节拿响应头），不写任何生产状态、不落盘到实验区以外。
输出一份 JSON；`--write-report DIR` 另存一份自包含 HTML 快照供离线看。

用法：
    python tools/experiments/model_source_drift/probe.py
    python tools/experiments/model_source_drift/probe.py --tags v0.32.4-dev.2 v0.32.3-dev.1
    python tools/experiments/model_source_drift/probe.py --write-report tools/experiments/model_source_drift/report_out
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for path in (REPO, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import source_policy  # noqa: E402
from maaracing_master.plugins.infinite_drive import model_installer  # noqa: E402

ASSET = "MaaRacingMaster-infinite_drive-models-{ver}.zip"
SHELVES = {
    "cnb": "https://cnb.cool/MaaRacingMaster/MAIN/-/releases/download/{tag}/{asset}",
    "github": "https://github.com/Maa-RM/MaaRacingMaster/releases/download/{tag}/{asset}",
}
UA = "MaaRacingMaster/model-install"
MAX_HOPS = 6
TIMEOUT_S = 25

# 合成用例：明确标注为「不改网络也能判」的假设场景，用来回答「发版时地址变了会怎样」。
DRIFT_CASES = [
    ("货架直链（今天第一跳）", "https://github.com/Maa-RM/MaaRacingMaster/releases/download/v1.0.0/pack.zip"),
    ("github 当前资产 CDN", "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sig=x"),
    ("cnb 当前资产 CDN", "https://asset.cnb.cool/assets/t/abc123"),
    ("未来换成未知 CDN", "https://cdn.maa-release.example/assets/pack.zip"),
    ("跳转被降级成 http", "http://asset.cnb.cool/assets/t/abc123"),
    ("跳转指向内网元数据", "https://169.254.169.254/latest/meta-data/"),
    ("URL 里带口令", "https://user:pw@asset.cnb.cool/assets/t/abc123"),
]


class _Tracer(urllib.request.HTTPRedirectHandler):
    """只记录每一跳，不做任何放行判断——判据由策略层事后决定。"""

    def __init__(self):
        self.hops: list[dict] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append({"code": code, "url": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _total_bytes(response) -> int | None:
    content_range = response.headers.get("Content-Range") or ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def probe_url(url: str, attempts: int = 3) -> dict:
    """取响应头；传输层抖动按 1s 退避重试，仍失败则记失败原因，不当成「资产缺失」。"""
    last: dict = {}
    for attempt in range(attempts):
        tracer = _Tracer()
        opener = urllib.request.build_opener(tracer)
        request = urllib.request.Request(
            url, headers={"User-Agent": UA, "Range": "bytes=0-15"})
        status: int | None = None
        total: int | None = None
        error = ""
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                status = int(response.status)
                total = _total_bytes(response)
                response.read(16)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            error = f"HTTP {exc.code}"
        except Exception as exc:  # 网络类异常：DNS、TLS、连接被重置
            error = f"{type(exc).__name__}: {exc}"
        last = {
            "status": status,
            "hops": tracer.hops,
            "total_bytes": total,
            "error": error,
            "attempts": attempt + 1,
        }
        if status is not None:
            break
        time.sleep(1.0 * (attempt + 1))
    return last


def _chain(url: str, hops: list[dict]) -> list[str]:
    return [url] + [hop["url"] for hop in hops]


def evaluate(tag: str, source: str, url: str, probed: dict) -> dict:
    chain = _chain(url, probed["hops"])
    policies = {
        "p0": source_policy.chain_verdict(chain, lambda u: source_policy.policy_static(u, source_policy.P0_HOSTS)),
        "p1": source_policy.chain_verdict(chain, lambda u: source_policy.policy_static(u, model_installer.ALLOWED_HOSTS)),
        "p2": source_policy.policy_public_https(chain[0])
        and source_policy.chain_verdict(chain, source_policy.policy_public_https),
    }
    asset_ok = probed["status"] in (200, 206) and bool(probed["total_bytes"])
    if asset_ok:
        asset = "ok"
    elif probed["status"] in (404, 410):
        asset = "missing"
    else:
        # 连不上 / 被重置 / 403：不能当成发行缺件，只能记「没验成」。
        asset = "unknown"
    return {
        "tag": tag,
        "source": source,
        "url": url,
        "status": probed["status"],
        "error": probed["error"],
        "attempts": probed.get("attempts", 1),
        "asset": asset,
        "hops": probed["hops"],
        "total_bytes": probed["total_bytes"],
        "policies": policies,
    }


def _has_one_click(tag: str) -> bool:
    """该 tag 的发行流水线是否注入了模型下载描述（= 这一版真的会提供一键安装）。

    判据取自身仓库：release.yml 里传 -ModelDescriptor 才把 download.json 打进主包；
    v0.30.0~v0.32.0 只产模型 ZIP 不注入描述，那一代只走手动安装，拿它们的 Release
    缺件当故障是在栽赃——先分代，再统计。
    """
    try:
        out = subprocess.run(
            ["git", "show", f"{tag}:.github/workflows/release.yml"],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
    except Exception:
        return False
    return "ModelDescriptor" in out


def default_tags(limit: int = 8) -> list[str]:
    """默认探测「出厂过一键安装」的 tag，按版本倒序取前 limit 个。"""
    try:
        out = subprocess.run(
            ["git", "tag", "--sort=-v:refname"],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
    except Exception:
        return []
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return [tag for tag in tags if _has_one_click(tag)][:limit]


def drift_rows() -> list[dict]:
    rows = []
    for name, url in DRIFT_CASES:
        rows.append({
            "case": name,
            "url": url,
            "policies": {
                "p0": source_policy.policy_static(url, source_policy.P0_HOSTS),
                "p1": source_policy.policy_static(url, model_installer.ALLOWED_HOSTS),
                "p2": source_policy.policy_public_https(url),
            },
        })
    return rows


def build_report(tags: list[str]) -> dict:
    rows = []
    for tag in tags:
        asset = ASSET.format(ver=tag.lstrip("v"))
        for source, template in SHELVES.items():
            url = template.format(tag=tag, asset=asset)
            rows.append(evaluate(tag, source, url, probe_url(url)))
    summary = {
        "rows": len(rows),
        "p0_pass": sum(1 for r in rows if r["policies"]["p0"]),
        "p1_pass": sum(1 for r in rows if r["policies"]["p1"]),
        "p2_pass": sum(1 for r in rows if r["policies"]["p2"]),
        "assets_missing": [
            f"{r['tag']}/{r['source']}" for r in rows if r["asset"] == "missing"],
        "assets_unknown": [
            f"{r['tag']}/{r['source']}" for r in rows if r["asset"] == "unknown"],
    }
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tags": tags,
        "rows": rows,
        "drift": drift_rows(),
        "summary": summary,
    }


def write_snapshot(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "matrix.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html = (HERE / "index.html").read_text(encoding="utf-8")
    inline = ("<script>window.MATRIX = "
              + json.dumps(report, ensure_ascii=False)
              + ";</script>")
    html = html.replace("<script>window.MATRIX = null;</script>", inline)
    html_path = out_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    return html_path, json_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", nargs="*", default=None,
                        help="要探测的 tag（默认取最近 8 个 git tag）")
    parser.add_argument("--json", default="", help="把结果写到指定 JSON 路径")
    parser.add_argument("--write-report", default="",
                        help="另存自包含 HTML 快照与 matrix.json 的目录")
    args = parser.parse_args(argv)

    tags = args.tags or default_tags()
    if not tags:
        print("[probe] 没有可探测的 tag", file=sys.stderr)
        return 2

    report = build_report(tags)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
        print(f"[probe] json -> {args.json}")
    else:
        print(text)
    if args.write_report:
        html_path, json_path = write_snapshot(report, Path(args.write_report))
        print(f"[probe] html -> {html_path}")
        print(f"[probe] json -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
