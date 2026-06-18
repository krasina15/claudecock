#!/usr/bin/env python3
"""
Cockpit web UI server for Claude Code sessions.

Local-only (binds 127.0.0.1). Serves a single-page graph + search over all
git working copies and their Claude Code sessions.

  python3 tools/cockpit/serve.py [--port 8787] [--no-open]

Endpoints:
  GET  /                 → static/index.html
  GET  /static/<file>    → static assets (cytoscape, etc.)
  GET  /api/model        → cached JSON model (?refresh=1 rebuilds, ~15s)
  POST /api/open         → {"path": ...}  reveal a directory in Finder
  POST /api/terminal     → {"path": ...}  open Terminal at a directory

Zero external dependencies — stdlib only.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

_cache = {"model": None, "ts": 0}
_lock = threading.Lock()

# conversation-text cache: {jsonl_path: (mtime, text)}
_text_cache = {}
_text_lock = threading.Lock()


def _session_text(jsonl_path):
    mt = os.path.getmtime(jsonl_path)
    with _text_lock:
        cached = _text_cache.get(jsonl_path)
        if cached and cached[0] == mt:
            return cached[1]
    text = scan.conversation_text(jsonl_path)
    with _text_lock:
        _text_cache[jsonl_path] = (mt, text)
    return text


def search_conversations(q, limit=60, snippets=3):
    """Substring search across full conversation text of every session."""
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    model = get_model()
    by_id = {s["id"]: s for s in model["sessions"]}
    results = []
    for s in model["sessions"]:
        text = _session_text(s["jsonl"])
        tl = text.lower()
        if ql not in tl:
            continue
        hits = tl.count(ql)
        snips, start = [], 0
        for _ in range(snippets):
            i = tl.find(ql, start)
            if i < 0:
                break
            a = max(0, i - 70)
            b = min(len(text), i + len(q) + 70)
            frag = text[a:b].replace("\n", " ").strip()
            snips.append(frag)
            start = i + len(q)
        meta = by_id.get(s["id"], {})
        results.append({**meta, "hits": hits, "snippets": snips})
    results.sort(key=lambda r: r["hits"], reverse=True)
    return results[:limit]


def get_model(refresh=False):
    with _lock:
        if refresh or _cache["model"] is None:
            _cache["model"] = scan.build_model()
            _cache["ts"] = time.time()
        return _cache["model"]


def _safe_dir(path):
    """Only allow opening directories the cockpit actually indexed."""
    if not path:
        return False
    m = get_model()
    known = {d["path"] for d in m["directories"]}
    known |= {s["cwd"] for s in m["sessions"] if s.get("cwd")}
    known = {os.path.realpath(p) for p in known if p}
    p = os.path.realpath(path)
    return os.path.isdir(p) and p in known


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._serve_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            fname = os.path.basename(path)
            fp = os.path.join(STATIC, fname)
            ctype = ("text/javascript" if fname.endswith(".js")
                     else "text/css" if fname.endswith(".css")
                     else "application/octet-stream")
            self._serve_file(fp, ctype)
        elif path == "/api/model":
            refresh = "refresh=1" in self.path
            self._send(200, get_model(refresh=refresh))
        elif path == "/api/search":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            self._send(200, {"q": q, "results": search_conversations(q)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = {}
        path = data.get("path", "")
        if self.path == "/api/open":
            if _safe_dir(path):
                subprocess.Popen(["open", path])
                self._send(200, {"ok": True})
            else:
                self._send(400, {"error": "invalid path"})
        elif self.path == "/api/terminal":
            if _safe_dir(path):
                subprocess.Popen([
                    "osascript", "-e",
                    f'tell application "Terminal" to do script "cd {path}"',
                    "-e", 'tell application "Terminal" to activate',
                ])
                self._send(200, {"ok": True})
            else:
                self._send(400, {"error": "invalid path"})
        else:
            self._send(404, {"error": "not found"})

    def _serve_file(self, fp, ctype):
        try:
            with open(fp, "rb") as fh:
                body = fh.read()
            self._send(200, body, ctype)
        except FileNotFoundError:
            self._send(404, {"error": "not found"})


def main():
    port = 8787
    do_open = True
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if "--no-open" in sys.argv:
        do_open = False
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"cockpit → {url}  (Ctrl-C to stop)")
    # warm the cache in the background so the first page load is instant-ish
    threading.Thread(target=get_model, daemon=True).start()
    if do_open:
        subprocess.Popen(["open", url])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
