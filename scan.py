#!/usr/bin/env python3
"""
Cockpit scanner — builds the JSON model for the Claude Code session web UI.

Read-only. Joins two sources:
  1. Claude Code sessions: ~/.claude/projects/<encoded-dir>/<session-id>.jsonl
     → sessionId, aiTitle, gitBranch, cwd, PR link, mtime, first user prompt
  2. Git working copies: repos where those sessions ran (derived from cwd),
     plus their sibling repos → branch, dirty, ahead/behind, merged, last commit

Emits {"directories": [...], "sessions": [...], "generated": <epoch>} to stdout
(or via build_model() for the server).

Zero external dependencies — stdlib only.
"""
import json
import os
import glob
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
# Roots are derived from where Claude sessions actually ran. Optionally add
# extra workspace roots to also surface their sibling git repos (os.pathsep-sep).
EXTRA_ROOTS = [p for p in os.environ.get("CLAUDECOCK_ROOTS", "").split(os.pathsep) if p]
MAIN_BRANCH = os.environ.get("CLAUDECOCK_MAIN_BRANCH", "main")


def _git(cwd, *args, timeout=10):
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _dir_state(path):
    """Git state for a single working copy, or None if not a git repo."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(path, "status", "--porcelain")
    dirty = len([l for l in status.splitlines() if l.strip()]) if status else 0
    ahead = behind = None
    counts = _git(path, "rev-list", "--left-right", "--count",
                  f"{MAIN_BRANCH}...HEAD")
    if counts and "\t" in counts:
        b, a = counts.split("\t")
        behind, ahead = int(b), int(a)
    merged = None
    if branch and branch != MAIN_BRANCH:
        mb = _git(path, "merge-base", "--is-ancestor", "HEAD", MAIN_BRANCH)
        merged = mb is not None  # _git returns "" on success, None on fail
    last_ts = _git(path, "log", "-1", "--format=%ct")
    return {
        "name": os.path.basename(path),
        "path": path,
        "root": os.path.dirname(path),
        "branch": branch,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "merged": merged,
        "last_rel": _git(path, "log", "-1", "--format=%cr"),
        "last_ts": int(last_ts) if last_ts and last_ts.isdigit() else None,
    }


def scan_directories(session_cwds):
    """Git repos that host sessions, plus their git-repo siblings.

    Derives directories from where Claude sessions actually ran (no hardcoded
    project glob): the parent of every session cwd becomes a "root", and every
    git-repo child of those roots is reported. Generalizes to all projects and
    auto-discovers new workspace roots.
    """
    candidates = set()
    roots = set()
    for cwd in session_cwds:
        if cwd and os.path.isdir(cwd):
            candidates.add(cwd)            # the session's own dir
            roots.add(os.path.dirname(cwd))
    for extra in EXTRA_ROOTS:
        if os.path.isdir(extra):
            roots.add(extra)
    for root in roots:
        try:
            for entry in os.scandir(root):
                if entry.is_dir():
                    candidates.add(entry.path)
        except OSError:
            continue
    rows = []
    for path in sorted(candidates):
        st = _dir_state(path)
        if st:
            rows.append(st)
    return rows


def _parse_session(jsonl_path):
    """Extract session metadata from a Claude Code transcript."""
    sid = os.path.basename(jsonl_path)[:-6]  # strip .jsonl
    title = branch = cwd = None
    pr_number = pr_url = None
    first_prompt = None
    msg_count = 0
    try:
        with open(jsonl_path, "r", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                t = e.get("type")
                if t == "ai-title":
                    title = e.get("aiTitle") or title
                elif t == "pr-link":
                    pr_number = e.get("prNumber") or pr_number
                    pr_url = e.get("prUrl") or pr_url
                elif t in ("user", "assistant"):
                    msg_count += 1
                if e.get("gitBranch"):
                    branch = e["gitBranch"]
                if e.get("cwd"):
                    cwd = e["cwd"]
                if first_prompt is None and t == "user":
                    m = e.get("message", {})
                    c = m.get("content") if isinstance(m, dict) else None
                    if isinstance(c, str):
                        first_prompt = c
                    elif isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get("type") == "text":
                                first_prompt = p.get("text")
                                break
    except Exception:
        pass
    if first_prompt:
        first_prompt = " ".join(first_prompt.split())[:200]
    return {
        "id": sid,
        "title": title,
        "branch": branch,
        "cwd": cwd,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "first_prompt": first_prompt,
        "messages": msg_count,
        "mtime": int(os.path.getmtime(jsonl_path)),
    }


def conversation_text(jsonl_path):
    """Concatenated user+assistant message text of a session (no tool noise)."""
    parts = []
    try:
        with open(jsonl_path, "r", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") not in ("user", "assistant"):
                    continue
                m = e.get("message", {})
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and p.get("type") == "text":
                            parts.append(p.get("text", ""))
    except Exception:
        pass
    return "\n".join(parts)


def fulltext_match(text, q, snippets=3):
    """Substring search of q in text → (hit_count, [snippet, ...]).

    Case-insensitive. Snippets are ±70 chars around the first `snippets`
    occurrences, newlines flattened. Returns (0, []) when q is absent.
    Shared by the web server (/api/search) and the CLI (search.py).
    """
    q = (q or "").strip()
    if not q:
        return 0, []
    ql, tl = q.lower(), text.lower()
    if ql not in tl:
        return 0, []
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
    return hits, snips


def search_titles(sessions, q):
    """Metadata filter over sessions: title, first prompt, branch, workspace, id.

    Case-insensitive substring; the CLI/UI "by title" mode. Instant — no
    transcript reads. Preserves the model's (recency) order.
    """
    ql = (q or "").strip().lower()
    if not ql:
        return []
    out = []
    for s in sessions:
        fields = (s.get("title"), s.get("first_prompt"), s.get("branch"),
                  s.get("workspace"), s.get("id"))
        hay = " ".join(f for f in fields if f).lower()
        if ql in hay:
            out.append(s)
    return out


def scan_sessions():
    """Every Claude Code session across all projects in ~/.claude/projects."""
    rows = []
    for proj in sorted(glob.glob(os.path.join(CLAUDE_PROJECTS, "*"))):
        if not os.path.isdir(proj):
            continue
        base = os.path.basename(proj)
        for jsonl in glob.glob(os.path.join(proj, "*.jsonl")):
            s = _parse_session(jsonl)
            # workspace = real dir basename (from cwd), fallback to encoded tail
            if s["cwd"]:
                s["workspace"] = os.path.basename(s["cwd"].rstrip("/"))
            else:
                s["workspace"] = base.split("-")[-1] or base
            s["jsonl"] = jsonl
            rows.append(s)
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def build_model():
    sessions = scan_sessions()
    cwds = {s["cwd"] for s in sessions if s.get("cwd")}
    return {
        "directories": scan_directories(cwds),
        "sessions": sessions,
        "generated": int(time.time()),
    }


if __name__ == "__main__":
    model = build_model()
    if "--stats" in sys.argv:
        d, s = model["directories"], model["sessions"]
        print(f"directories: {len(d)}  ({sum(1 for x in d if x['dirty'])} dirty, "
              f"{sum(1 for x in d if x['merged'])} merged)")
        print(f"sessions:    {len(s)}  ({sum(1 for x in s if x['title'])} titled, "
              f"{sum(1 for x in s if x['pr_number'])} with PR)")
    else:
        json.dump(model, sys.stdout, ensure_ascii=False, indent=2)
