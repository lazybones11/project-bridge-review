#!/usr/bin/env python3
"""Bounded, read-only Codex message and source excerpts. Standard library only."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit


def records(path, diagnostics=None):
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if diagnostics is not None:
                    diagnostics["skipped_jsonl_records"] = diagnostics.get("skipped_jsonl_records", 0) + 1
                continue  # A running thread can have a partial trailing line.
            if isinstance(row, dict):
                yield number, row


def database(home):
    numbered = []
    for path in home.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_([0-9]+)\.sqlite", path.name)
        if match and path.is_file():
            numbered.append((int(match.group(1)), path))
    if not numbered:
        return None
    _, latest = max(numbered, key=lambda item: item[0])
    conn = sqlite3.connect(latest.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def thread_rows(home, query="", project="", limit=10, thread=None):
    conn = database(home)
    if conn is not None:
        try:
            has_threads = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads' COLLATE NOCASE"
            ).fetchone()
            if has_threads:
                columns = {r["name"] for r in conn.execute("PRAGMA table_info(threads)")}
                label = "coalesce(nullif(name,''),title)" if "name" in columns else "title"
                sql = f"SELECT id,{label} AS title,cwd,updated_at,archived,rollout_path FROM threads WHERE 1=1"
                args = []
                if thread:
                    sql += " AND id=?"
                    args.append(thread)
                if query:
                    sql += f" AND (instr(lower(title),lower(?))>0 OR instr(lower({label}),lower(?))>0)"
                    args.extend([query, query])
                if project:
                    sql += " AND instr(lower(cwd),lower(?))>0"
                    args.append(project)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                args.append(limit)
                return [dict(r) for r in conn.execute(sql, args)], "local_database"
        finally:
            conn.close()
    index = home / "session_index.jsonl"
    if not index.exists():
        raise ValueError("No supported local Codex thread database or index found")
    latest = {}
    for _, row in records(index):
        identifier = row.get("id")
        if identifier:
            title, updated_at = row.get("thread_name"), row.get("updated_at")
            latest[identifier] = {"id": identifier, "title": title if isinstance(title, str) else "",
                                  "cwd": None, "updated_at": updated_at if isinstance(updated_at, str) else ""}
    rows = [r for r in latest.values() if (not thread or r["id"] == thread)
            and query.casefold() in r["title"].casefold()]
    if project:
        raise ValueError("Local index has no cwd; use built-in project listing or search a title")
    return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:limit], "local_index"


def plain_path(value):
    """Normalize a recorded path: the Windows extended-length prefix defeats containment checks."""
    if not isinstance(value, str):
        return Path(value)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


def verified_rollout(path, identifier, home):
    """Resolve one candidate; return it with session metadata only when it is
    inside the Codex home and its session_meta identity matches."""
    if not path.is_file():
        return None
    resolved = path.resolve()
    if not resolved.is_relative_to(home.resolve()):
        return None  # Recorded paths outside the Codex home are never read.
    first = next(records(resolved), None)
    meta = first[1].get("payload", {}) if first else {}
    if first and first[1].get("type") == "session_meta" and meta.get("id", meta.get("session_id")) == identifier:
        return resolved, meta
    return None


def rollout(home, identifier):
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", identifier):
        raise ValueError("Use the full UUID thread ID from the thread listing")
    rows, _ = thread_rows(home, thread=identifier, limit=1)
    candidates = []
    if rows and rows[0].get("rollout_path"):
        candidates.append(plain_path(rows[0]["rollout_path"]))
    for path in candidates:
        verified = verified_rollout(path, identifier, home)
        if verified:
            resolved, meta = verified
            return resolved, rows[0] if rows else {"id": identifier, "cwd": meta.get("cwd")}
    for directory in (home / "sessions", home / "archived_sessions"):
        if not directory.is_dir():
            continue
        for found in directory.rglob(f"*{identifier}.jsonl"):
            verified = verified_rollout(found, identifier, home)
            if verified:
                resolved, meta = verified
                return resolved, rows[0] if rows else {"id": identifier, "cwd": meta.get("cwd")}
    raise ValueError("Selected thread has no readable, identity-verified local rollout")


def text_blocks(payload):
    content = payload.get("content", [])
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") in ("input_text", "output_text", "text")
                     and isinstance(block.get("text"), str))


RUNTIME_TAGS = r"environment_context|external_codex_apps_open_page|INSTRUCTIONS"
RUNTIME_TAG_LINE = re.compile(rf" {{0,3}}<(/?)({RUNTIME_TAGS})>[ \t]*")
RUNTIME_BLOCK_LINE = re.compile(rf" {{0,3}}<({RUNTIME_TAGS})>(?:(?!</\1>).)*</\1>[ \t]*")
AGENTS_HEADER = re.compile(r" {0,3}#[ \t]*AGENTS\.md[ \t]+instructions[ \t]*", re.IGNORECASE)
MARKDOWN_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def clean_runtime_blocks(content):
    """Return the visible text and whether any runtime markup was removed."""
    kept = []
    injected = False
    block = None
    depth = 0
    fence = None
    for line in content.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        marker = MARKDOWN_FENCE.fullmatch(text)
        if fence is not None:
            if marker and marker[1].startswith(fence) and not marker[2].strip(" \t"):
                fence = None
        elif marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
        else:
            tag = RUNTIME_TAG_LINE.fullmatch(text)
            if block is not None:
                if tag and tag[2] == block:
                    depth += -1 if tag[1] else 1
                    if depth == 0:
                        block = None
                continue
            if tag and not tag[1]:
                block = tag[2]
                depth = 1
                injected = True
                continue
            if AGENTS_HEADER.fullmatch(text) or RUNTIME_BLOCK_LINE.fullmatch(text):
                injected = True
                continue
        if block is None:
            kept.append(line)
    return "".join(kept).strip(), injected


def visible_messages(path, diagnostics=None):
    primary, legacy, seen = [], [], set()
    for line, row in records(path, diagnostics):
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role not in ("user", "assistant") or payload.get("channel") == "analysis" or payload.get("phase") == "analysis":
                continue
            content, injected = clean_runtime_blocks(text_blocks(payload))
            if not content:
                continue
            message_id = payload.get("id") or f"line-{line}"
            if message_id in seen:
                continue
            seen.add(message_id)
            primary.append({"message_id": message_id, "role": role, "timestamp": row.get("timestamp"),
                            "source_line": line, "text": content, "injected": injected})
        elif row.get("type") == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
            raw = payload.get("message")
            if isinstance(raw, str) and raw.strip():
                content, injected = clean_runtime_blocks(raw)
                if not content:
                    continue
                legacy.append({"message_id": f"line-{line}",
                               "role": "user" if payload["type"] == "user_message" else "assistant",
                               "timestamp": row.get("timestamp"), "source_line": line,
                               "text": content, "injected": injected})
    roles = {r["role"] for r in primary}
    return sorted(primary + [r for r in legacy if r["role"] not in roles], key=lambda r: r["source_line"])


def excerpt(args, home):
    path, meta = rollout(home, args.thread)
    diagnostics = {"skipped_jsonl_records": 0}
    messages = visible_messages(path, diagnostics)
    if not messages:
        raise ValueError("No supported visible text messages; local format may be unsupported")
    if args.query:
        pattern = re.compile(re.escape(args.query), re.IGNORECASE)
        hits = [i for i, m in enumerate(messages) if pattern.search(m["text"])]
    elif args.messages:
        wanted = set(args.messages)
        hits = [i for i, m in enumerate(messages) if m["message_id"] in wanted]
        missing = wanted - {messages[i]["message_id"] for i in hits}
        if missing:
            raise ValueError("Message IDs not found: " + ", ".join(sorted(missing)))
    else:
        hits = list(range(max(0, len(messages) - args.last), len(messages)))
    selected_hits = hits[:args.limit] if args.query else hits
    windows = sorted({j for i in selected_hits for j in range(max(0, i - args.context), min(len(messages), i + args.context + 1))})
    # Reserve fair budgets for hits first, so neighboring messages cannot swallow a hit.
    priority = selected_hits + [i for i in windows if i not in selected_hits]
    remaining, clips = args.max_chars, {}
    for position, i in enumerate(priority):
        if remaining <= 0:
            break
        m = messages[i]
        content = m["text"]
        budget = max(1, remaining // (len(priority) - position))
        match = pattern.search(content) if args.query else None
        hit = match.start() if match else -1
        start = max(0, hit - budget // 3) if hit >= 0 else 0
        end = min(len(content), start + budget)
        start = max(0, end - budget) if hit >= 0 else start
        clip = content[start:end]
        clips[i] = {**m, "text": clip, "hit": i in selected_hits, "start_char": start, "end_char": end,
                    "original_chars": len(content), "truncated": start != 0 or end != len(content),
                    "message_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        remaining -= len(clip)
    return {"status": "ok" if hits else "no_match", "kind": "chat_excerpt", "untrusted_data": True,
            "source": {"thread_id": args.thread, "title": meta.get("title"), "cwd": meta.get("cwd"), "rollout_path": str(path)},
            "query": args.query, "total_hits": len(hits), "returned_hits": sum(m["hit"] for m in clips.values()),
            "local_snapshot": True, "diagnostics": diagnostics,
            "more_hits": len(selected_hits) < len(hits), "omitted_window_messages": len(windows) - len(clips),
            "messages": [clips[i] for i in sorted(clips)]}


def git_metadata(root, path):
    def run(directory, *command):
        result = subprocess.run(
            ["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=false",
             "-C", str(directory), *command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
        )
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        top = run(root, "rev-parse", "--show-toplevel")
        if not top:
            return None
        origin = run(top, "remote", "get-url", "origin")
        if origin and "://" in origin:
            parts = urlsplit(origin)
            origin = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        relative = path.relative_to(Path(top).resolve()).as_posix()
        state = run(top, "status", "--porcelain", "--ignore-submodules=all", "--", relative)
        return {"root": top, "commit": run(top, "rev-parse", "HEAD"), "origin": origin,
                "file_status": state, "working_copy": True}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def sensitive_source(relative):
    parts = tuple(part.rstrip(" .").casefold() for part in relative.parts)
    name = parts[-1]
    return (
        any(part in {".git", ".ssh", ".aws", ".gnupg"} for part in parts)
        or name.startswith((".env", "id_rsa", "id_ed25519", "credentials"))
        or name.endswith((".pem", ".key"))
        or name in {".npmrc", ".pypirc", ".netrc", ".git-credentials"}
    )


def code_excerpt(args):
    root = Path(args.root).resolve(strict=True)
    requested = root / args.file
    path = requested.resolve(strict=True)
    if not root.is_dir() or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Source file must resolve inside the specified project root")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("Choose a project directory; the filesystem root and user home are too broad")
    lexical = Path(os.path.abspath(requested))
    if sensitive_source(path) or sensitive_source(lexical):
        raise ValueError("Reading this sensitive source path is not supported")
    if args.start < 1 or args.end < args.start or args.end - args.start + 1 > 1000:
        raise ValueError("Choose a valid 1-based line range of at most 1000 lines")
    data = path.read_bytes()
    if b"\x00" in data:
        raise ValueError("Binary source is unsupported")
    lines = data.decode(args.encoding).splitlines(keepends=True)
    if args.end > len(lines):
        raise ValueError(f"Requested line range exceeds the file's {len(lines)} lines")
    text = "".join(lines[args.start - 1:args.end])
    if len(text) > args.max_chars:
        raise ValueError("Exact source excerpt exceeds character budget; narrow the line range")
    return {"status": "ok", "kind": "code_excerpt", "untrusted_data": True,
            "source": {"root": str(root), "path": str(path), "start_line": args.start, "end_line": args.end,
                       "encoding": args.encoding, "file_sha256": hashlib.sha256(data).hexdigest(),
                       "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                       "git": git_metadata(root, path)}, "text": text}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("threads", help="Search thread metadata only")
    t.add_argument("--query", default="")
    t.add_argument("--project", default="")
    t.add_argument("--limit", type=int, default=10)
    e = sub.add_parser("excerpt", help="Read only the selected local thread")
    e.add_argument("--thread", required=True)
    choice = e.add_mutually_exclusive_group(required=True)
    choice.add_argument("--query")
    choice.add_argument("--messages", nargs="+")
    choice.add_argument("--last", type=int)
    e.add_argument("--limit", type=int, default=3)
    e.add_argument("--context", type=int, default=1)
    e.add_argument("--max-chars", type=int, default=8000)
    c = sub.add_parser("code", help="Read exact source lines and provenance")
    c.add_argument("--root", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--start", type=int, required=True)
    c.add_argument("--end", type=int, required=True)
    c.add_argument("--encoding", default="utf-8-sig")
    c.add_argument("--max-chars", type=int, default=16000)
    for command in (t, e, c):
        command.add_argument("--out", help="Exclusively create a small JSON packet")
    return p


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        if hasattr(args, "limit") and not 1 <= args.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if hasattr(args, "context") and not 0 <= args.context <= 3:
            raise ValueError("context must be between 0 and 3")
        if hasattr(args, "max_chars") and not 1 <= args.max_chars <= 64000:
            raise ValueError("max-chars must be between 1 and 64000")
        if args.command == "excerpt" and (args.query == "" or args.last is not None and not 1 <= args.last <= 50):
            raise ValueError("Use a nonempty keyword or last between 1 and 50")
        home = Path(args.codex_home).expanduser().resolve()
        if args.command == "threads":
            rows, backend = thread_rows(home, args.query, args.project, args.limit)
            result = {"status": "ok" if rows else "no_match", "kind": "thread_candidates", "untrusted_data": True,
                      "backend": backend, "threads": [{k: v for k, v in r.items() if k != "rollout_path"} for r in rows]}
        elif args.command == "excerpt":
            result = excerpt(args, home)
        else:
            result = code_excerpt(args)
        result["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            target = Path(args.out).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
            print(json.dumps({"status": "saved", "path": str(target), "kind": result["kind"]}, ensure_ascii=False))
        else:
            print(encoded)
        return 0
    except (OSError, ValueError, sqlite3.Error, UnicodeError, LookupError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
