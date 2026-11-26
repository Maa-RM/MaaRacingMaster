# -*- coding: utf-8 -*-
"""极简前端服务：一个页面 + 一个 JSON 接口，数据来自 probe.py 的真实探测。

    python tools/experiments/model_source_drift/server.py --port 8765

首次启动即探测（每源一次 Range 请求），结果缓存在内存；`?refresh=1` 重探。
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
import sys  # noqa: E402

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import probe  # noqa: E402

_LOCK = threading.Lock()
_CACHE: dict | None = None


def _report(tags: list[str] | None, refresh: bool) -> dict:
    global _CACHE
    with _LOCK:
        if refresh or _CACHE is None:
            _CACHE = probe.build_report(tags or probe.default_tags())
        return _CACHE


class _Handler(BaseHTTPRequestHandler):
    tags: list[str] | None = None

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parts.path == "/api/matrix":
            refresh = "refresh=1" in (parts.query or "")
            try:
                data = _report(self.tags, refresh)
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, *_args):
        pass


def _self_check() -> None:
    """一边起服务一边自检：页面与接口都要真拿到，否则算实验没跑成。"""
    tags = probe.default_tags(limit=2) or ["v0.32.4-dev.2"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.tags = tags
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        import urllib.request

        html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
        data = json.loads(urllib.request.urlopen(base + "/api/matrix", timeout=60).read())
        print(f"[self-check] index.html {len(html)} bytes, MATRIX 注入点: "
              f"{'有' if 'window.MATRIX = null;' in html else '缺'}")
        print(f"[self-check] /api/matrix rows={len(data['rows'])} "
              f"p0={data['summary']['p0_pass']} p1={data['summary']['p1_pass']} "
              f"p2={data['summary']['p2_pass']}")
        assert data["rows"] and len(html) > 1000
        print("[self-check] OK")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tags", nargs="*", default=None)
    parser.add_argument("--self-check", action="store_true",
                        help="起临时实例跑页面+接口自检后退出")
    args = parser.parse_args(argv)
    if args.self_check:
        _Handler.tags = args.tags or None
        _self_check()
        return 0

    _Handler.tags = args.tags or None
    report = _report(_Handler.tags, False)
    print(f"[server] 探测完成：{report['summary']}")
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"[server] http://127.0.0.1:{args.port}/  (Ctrl+C 结束)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
