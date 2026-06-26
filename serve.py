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
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

# Linux terminal emulators that support "open a shell in this dir", in order of
# preference (KDE/konsole first — that's the common desktop here). Each maps a
# target dir to an argv. Override with CLAUDECOCK_TERMINAL='myterm --cd {dir}'.
_LINUX_TERMINALS = [
    ("konsole", lambda p: ["konsole", "--workdir", p]),
    ("gnome-terminal", lambda p: ["gnome-terminal", "--working-directory=" + p]),
    ("kitty", lambda p: ["kitty", "--directory", p]),
    ("alacritty", lambda p: ["alacritty", "--working-directory", p]),
    ("wezterm", lambda p: ["wezterm", "start", "--cwd", p]),
    ("foot", lambda p: ["foot", "--working-directory=" + p]),
    ("xfce4-terminal", lambda p: ["xfce4-terminal", "--working-directory=" + p]),
    ("tilix", lambda p: ["tilix", "--working-directory=" + p]),
    ("x-terminal-emulator", lambda p: ["x-terminal-emulator"]),
    ("xterm", lambda p: ["xterm", "-e", "sh", "-c", f"cd {p!r}; exec ${{SHELL:-sh}}"]),
]


def _opener_argv(target):
    """argv to reveal a file/dir or open a URL in the OS default handler."""
    if IS_MACOS:
        return ["open", target]
    if IS_WINDOWS:
        return ["cmd", "/c", "start", "", target]
    return ["xdg-open", target]


def _open_external(target):
    """Reveal a dir / open a URL via the OS default handler. Best-effort."""
    try:
        subprocess.Popen(_opener_argv(target))
        return True
    except Exception:
        return False


def _open_terminal(path):
    """Open a terminal at `path`. Returns True if a launcher was found."""
    if IS_MACOS:
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script "cd {path}"',
            "-e", 'tell application "Terminal" to activate',
        ])
        return True
    if IS_WINDOWS:
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", f"cd /d {path}"])
        return True
    override = os.environ.get("CLAUDECOCK_TERMINAL")
    if override:
        argv = [a.replace("{dir}", path) for a in override.split()]
        subprocess.Popen(argv)
        return True
    for name, build in _LINUX_TERMINALS:
        if shutil.which(name):
            subprocess.Popen(build(path))
            return True
    return False

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
    model = get_model()
    by_id = {s["id"]: s for s in model["sessions"]}
    results = []
    for s in model["sessions"]:
        text = _session_text(s["jsonl"])
        hits, snips = scan.fulltext_match(text, q, snippets)
        if not hits:
            continue
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
                _open_external(path)
                self._send(200, {"ok": True})
            else:
                self._send(400, {"error": "invalid path"})
        elif self.path == "/api/terminal":
            if not _safe_dir(path):
                self._send(400, {"error": "invalid path"})
            elif _open_terminal(path):
                self._send(200, {"ok": True})
            else:
                self._send(400, {"error": "no terminal emulator found"})
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
        _open_external(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
