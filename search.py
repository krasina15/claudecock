#!/usr/bin/env python3
"""
CLI search over Claude Code sessions — no browser needed.

Two modes, mirroring the web cockpit's two search boxes:

  python3 search.py "deploy nginx"      # full-text across every transcript
  python3 search.py -t "odoo"           # by title/metadata only (instant)

Full-text reads each session's conversation and ranks by hit count, with
highlighted snippets. Title mode filters session metadata (title, first
prompt, branch, workspace, id) — no transcript reads, so it is instant.

  -t / --titles     metadata-only search (default: full-text)
  -n / --limit N    cap results (default: 20)
  -s / --snippets N snippets per full-text hit (default: 2)
  --json            machine-readable output
  --no-color        disable ANSI colors

Zero external dependencies — stdlib only. Read-only.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402


def _c(code, s, on):
    return f"\033[{code}m{s}\033[0m" if on else s


def _highlight(text, q, on):
    """Case-insensitive highlight of every q in text."""
    if not on or not q:
        return text
    low, ql = text.lower(), q.lower()
    out, i = [], 0
    while True:
        j = low.find(ql, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(_c("1;33", text[j:j + len(q)], on))
        i = j + len(q)
    return "".join(out)


def _rel(ts):
    """Compact relative age, e.g. '3d', '5h', '2m', 'now'."""
    if not ts:
        return "?"
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return "now"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def _render(row, q, color, fulltext):
    title = row.get("title") or row.get("first_prompt") or "(untitled)"
    ws = row.get("workspace") or "?"
    branch = row.get("branch") or "?"
    sid = row.get("id", "")
    head = _c("1", _highlight(title, q if not fulltext else "", color), color)
    meta = _c("2", f"{ws} · {branch} · {_rel(row.get('mtime'))} · {sid[:8]}", color)
    if row.get("pr_number"):
        meta += _c("36", f" · PR #{row['pr_number']}", color)
    lines = [f"{head}", f"  {meta}"]
    if fulltext:
        lines[0] = f"{_c('33', str(row['hits']) + '×', color)} {head}"
        for snip in row.get("snippets", []):
            lines.append(f"  {_c('2', '…', color)}{_highlight(snip, q, color)}{_c('2', '…', color)}")
    if row.get("cwd"):
        cmd = f"cd {row['cwd']} && claude --resume {sid}"
        lines.append(f"  {_c('2', cmd, color)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        prog="search.py",
        description="Search Claude Code sessions from the CLI (full-text or by title).",
    )
    ap.add_argument("query", help="search string")
    ap.add_argument("-t", "--titles", action="store_true",
                    help="search title/metadata only (instant, no transcript reads)")
    ap.add_argument("-n", "--limit", type=int, default=20, help="max results (default 20)")
    ap.add_argument("-s", "--snippets", type=int, default=2,
                    help="snippets per full-text hit (default 2)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = ap.parse_args()

    color = sys.stdout.isatty() and not args.no_color and not args.json
    # Search needs only session metadata/transcripts — skip the git working-copy
    # scan (scan_directories), which is ~30s of subprocess and unused here.
    sessions = scan.scan_sessions()

    if args.titles:
        rows = scan.search_titles(sessions, args.query)[:args.limit]
        fulltext = False
    else:
        rows = []
        for s in sessions:
            hits, snips = scan.fulltext_match(
                scan.conversation_text(s["jsonl"]), args.query, args.snippets)
            if hits:
                rows.append({**s, "hits": hits, "snippets": snips})
        rows.sort(key=lambda r: r["hits"], reverse=True)
        rows = rows[:args.limit]
        fulltext = True

    if args.json:
        json.dump({"query": args.query, "mode": "titles" if args.titles else "fulltext",
                   "results": rows}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    if not rows:
        print(_c("2", "no matches", color))
        return
    for r in rows:
        print(_render(r, args.query, color, fulltext))
        print()
    mode = "title" if args.titles else "full-text"
    print(_c("2", f"{len(rows)} result(s) · {mode}", color))


if __name__ == "__main__":
    main()
