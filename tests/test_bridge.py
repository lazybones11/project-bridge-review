"""Behavioral tests use synthetic chats, never copy the user's whole history."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "project-bridge" / "scripts" / "bridge.py"
spec = importlib.util.spec_from_file_location("bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
THREAD = "12345678-1234-1234-1234-123456789012"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="片段桥测试-")
        self.root = Path(self.temp.name)
        self.home = self.root / "本地记录"
        self.home.mkdir()
        self.path = self.home / "sessions" / f"rollout-{THREAD}.jsonl"
        self.path.parent.mkdir()
        rows = [{"type": "session_meta", "payload": {"id": THREAD}}]
        def msg(identifier, role, text, **extra):
            return {"type": "response_item", "timestamp": "2026-10-02T10:00:00Z",
                    "payload": {"type": "message", "id": identifier, "role": role,
                                "content": [{"type": "output_text", "text": text}], **extra}}
        rows += [msg("system", "developer", "不得输出的系统指令"),
                 msg("m1", "user", "先讨论图像项目"),
                 {"type": "event_msg", "payload": {"type": "user_message", "message": "先讨论图像项目"}},
                 msg("m2", "assistant", "背景" * 100 + "模型配置: 显存参数=8G" + "无关" * 100),
                 msg("m2", "assistant", "背景" * 100 + "模型配置: 显存参数=8G" + "无关" * 100),
                 {"type": "response_item", "payload": {"type": "function_call_output", "output": "工具秘密"}},
                 {"type": "response_item", "payload": {"type": "reasoning", "summary": "隐藏推理"}},
                 msg("analysis", "assistant", "隐藏分析", channel="analysis"),
                 msg("m3", "user", "参数改成6G；不要采纳之前的8G"),
                 msg("m4", "assistant", "已更新为6G"),
                 msg("runtime", "user", "<environment_context>注入信息</environment_context>")]
        self.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        conn = sqlite3.connect(self.home / "state_5.sqlite")
        conn.execute("CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,updated_at INTEGER,archived INTEGER,rollout_path TEXT)")
        conn.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (THREAD, "图像模型讨论", "E:/源码/ComfyUI", 42, 0, str(self.path)))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **changes):
        defaults = dict(thread=THREAD, query="模型配置", messages=None, last=None, limit=3, context=1, max_chars=120)
        defaults.update(changes)
        return type("Args", (), defaults)()

    def test_bounded_hit_window_contains_keyword_and_neighbor_correction(self):
        result = bridge.excerpt(self.args(), self.home)
        self.assertEqual(result["total_hits"], 1)
        self.assertEqual([m["message_id"] for m in result["messages"]], ["m1", "m2", "m3"])
        self.assertLessEqual(sum(len(m["text"]) for m in result["messages"]), 120)
        hit = next(m for m in result["messages"] if m["hit"])
        self.assertIn("模型配置", hit["text"])
        original = next(m["text"] for m in bridge.visible_messages(self.path) if m["message_id"] == "m2")
        self.assertEqual(original[hit["start_char"]:hit["end_char"]], hit["text"])
        self.assertEqual(hit["message_sha256"], hashlib.sha256(original.encode("utf-8")).hexdigest())
        self.assertTrue(hit["truncated"])

    def test_no_match_never_exports_recent_messages(self):
        result = bridge.excerpt(self.args(query="不存在的主题"), self.home)
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["messages"], [])

    def test_exact_ids_and_hidden_material_exclusion(self):
        messages = bridge.visible_messages(self.path)
        self.assertEqual([m["message_id"] for m in messages], ["m1", "m2", "m3", "m4"])
        result = bridge.excerpt(self.args(query=None, messages=["m3"], context=0), self.home)
        self.assertEqual(result["messages"][0]["text"], "参数改成6G；不要采纳之前的8G")
        with self.assertRaises(ValueError):
            bridge.excerpt(self.args(query=None, messages=["missing"]), self.home)

    def test_legacy_format_and_partial_tail(self):
        self.path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "旧消息"}}, ensure_ascii=False) + "\n{", encoding="utf-8")
        diagnostics = {}
        self.assertEqual(bridge.visible_messages(self.path, diagnostics)[0]["text"], "旧消息")
        self.assertEqual(diagnostics["skipped_jsonl_records"], 1)

    def test_thread_metadata_and_database_unchanged(self):
        path = self.home / "state_5.sqlite"
        before = path.read_bytes()
        rows, backend = bridge.thread_rows(self.home, project="ComfyUI")
        self.assertEqual(rows[0]["id"], THREAD)
        self.assertEqual(backend, "local_database")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(bridge.thread_rows(self.home, query="不存在")[0], [])

    def test_non_numeric_state_database_is_ignored(self):
        (self.home / "state_backup.sqlite").write_bytes(b"placeholder")
        (self.home / "state_999_backup.sqlite").write_bytes(b"placeholder")
        (self.home / "state_1000.sqlite").mkdir()
        (self.home / "state_10.sqlite").write_bytes((self.home / "state_5.sqlite").read_bytes())
        (self.home / "state_5.sqlite").write_bytes(b"older numeric version")
        rows, backend = bridge.thread_rows(self.home, query="图像")
        self.assertEqual(rows[0]["id"], THREAD)
        self.assertEqual(backend, "local_database")

    def test_user_renamed_thread_is_searchable_by_visible_title(self):
        conn = sqlite3.connect(self.home / "state_5.sqlite")
        conn.execute("ALTER TABLE threads ADD COLUMN name TEXT")
        conn.execute("UPDATE threads SET name='口播'")
        conn.commit()
        conn.close()
        renamed, _ = bridge.thread_rows(self.home, query="口播")
        self.assertEqual(renamed[0]["title"], "口播")
        original, _ = bridge.thread_rows(self.home, query="图像模型讨论")
        self.assertEqual(original[0]["title"], "口播")

    def test_index_fallback_and_missing_project_metadata(self):
        (self.home / "state_5.sqlite").unlink()
        (self.home / "session_index.jsonl").write_text(json.dumps({"id": THREAD, "thread_name": "模型讨论", "updated_at": "2026-10-02"}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(bridge.thread_rows(self.home, query="模型")[0][0]["id"], THREAD)
        with self.assertRaises(ValueError):
            bridge.thread_rows(self.home, project="ComfyUI")
        self.assertEqual(bridge.rollout(self.home, THREAD)[0], self.path.resolve())

    def test_index_fallback_when_latest_database_has_no_threads_table(self):
        db_path = self.home / "state_10.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA user_version = 1")
        finally:
            conn.close()
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": THREAD, "thread_name": "索引中的图像讨论"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        before = db_path.read_bytes()
        rows, backend = bridge.thread_rows(self.home, query="图像")
        self.assertEqual(backend, "local_index")
        self.assertEqual(rows[0]["id"], THREAD)
        self.assertIsNone(rows[0]["cwd"])
        self.assertEqual(db_path.read_bytes(), before)

    def test_null_index_title_and_timestamp_do_not_crash_lookup(self):
        (self.home / "state_5.sqlite").unlink()
        rows = [
            {"id": THREAD, "thread_name": None, "updated_at": None},
            {"id": "other", "thread_name": "有标题", "updated_at": "2026-10-02"},
        ]
        (self.home / "session_index.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
        )
        found, backend = bridge.thread_rows(self.home)
        self.assertEqual(backend, "local_index")
        self.assertEqual([row["id"] for row in found], ["other", THREAD])
        self.assertEqual((found[1]["title"], found[1]["updated_at"]), ("", ""))
        self.assertEqual([row["id"] for row in bridge.thread_rows(self.home, query="标题")[0]], ["other"])

    def test_exact_source_unicode_hashes_and_traversal(self):
        project = self.root / "中文源码"
        project.mkdir()
        source = project / "示例.py"
        raw = "# 模型\r\n参数 = 8\r\nprint(参数)\r\n".encode("utf-8")
        source.write_bytes(raw)
        args = type("Args", (), dict(root=str(project), file="示例.py", start=2, end=3, encoding="utf-8", max_chars=1000))()
        result = bridge.code_excerpt(args)
        self.assertEqual(result["text"], "参数 = 8\r\nprint(参数)\r\n")
        self.assertEqual(result["source"]["file_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.read_bytes(), raw)
        outside = self.root / "outside.py"
        outside.write_text("secret", encoding="utf-8")
        args.file = "../outside.py"
        with self.assertRaises(ValueError):
            bridge.code_excerpt(args)
        args.file, args.end = "示例.py", 4
        with self.assertRaises(ValueError):
            bridge.code_excerpt(args)

    def test_code_budget_rejects_instead_of_silent_crop(self):
        source = self.root / "long.py"
        source.write_text("very_long_variable = 123\n", encoding="utf-8")
        args = type("Args", (), dict(root=str(self.root), file=source.name, start=1, end=1, encoding="utf-8", max_chars=2))()
        with self.assertRaises(ValueError):
            bridge.code_excerpt(args)

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_code_rejects_common_sensitive_paths_before_reading(self):
        sample = "synthetic-contents-not-for-output"
        project = self.root / "project"
        project.mkdir()
        for name in (".ENV.local", "config/identity.pem", ".git/config", ".ssh/config", "credentials.json"):
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sample, encoding="utf-8")
            command = [sys.executable, "-X", "utf8", str(SCRIPT), "code", "--root", str(project),
                       "--file", name, "--start", "1", "--end", "1"]
            with self.subTest(path=name):
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertIn("sensitive source", result.stderr)
                self.assertNotIn(sample, result.stdout + result.stderr)

    def test_git_nested_root_reports_actual_dirty_file(self):
        repo = self.root / "repo"
        repo.mkdir()
        for command in (["init"], ["config", "user.email", "fixture@example.invalid"], ["config", "user.name", "Fixture"]):
            subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
        child = repo / "src"
        child.mkdir()
        source = child / "sample.py"
        source.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
        clean = bridge.git_metadata(child, source)
        self.assertEqual(clean["file_status"], "")
        self.assertTrue(clean["commit"])
        source.write_text("value = 2\n", encoding="utf-8")
        dirty = bridge.git_metadata(child, source)
        self.assertIn("src/sample.py", dirty["file_status"])
        self.assertIn("M", dirty["file_status"])

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_git_metadata_keeps_index_and_disables_fsmonitor(self):
        repo = self.root / "read-only-git"
        repo.mkdir()
        def run(*command):
            subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
        run("init", "--template=")
        run("config", "user.email", "fixture@example.invalid")
        run("config", "user.name", "Fixture")
        run("config", "commit.gpgsign", "false")
        source = repo / "fixture.txt"
        source.write_bytes(b"unchanged content\n")
        run("add", "--", "fixture.txt")
        run("commit", "-m", "fixture")
        index = repo / ".git/index"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        before = index.read_bytes()
        self.assertEqual(bridge.git_metadata(repo, source)["file_status"], "")
        self.assertEqual(index.read_bytes(), before)

        hook = repo / ".git/fixture-fsmonitor.sh"
        marker = repo / ".git/fixture-invoked"
        hook.write_bytes(b"#!/bin/sh\nprintf invoked > .git/fixture-invoked\nprintf 'fixture\\0'\n")
        hook.chmod(0o755)
        run("config", "core.fsmonitor", ".git/fixture-fsmonitor.sh")
        self.assertEqual(bridge.git_metadata(repo, source)["file_status"], "")
        self.assertFalse(marker.exists(), "repository fsmonitor unexpectedly ran")
        self.assertEqual(index.read_bytes(), before)

    def test_packet_exclusive_create_and_cli_failure(self):
        packet = self.root / "片段包" / "sample.json"
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--codex-home", str(self.home),
                   "excerpt", "--thread", THREAD, "--messages", "m3", "--context", "0", "--out", str(packet)]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(packet.read_text(encoding="utf-8"))["messages"][0]["message_id"], "m3")
        before = packet.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(second.returncode, 2)
        self.assertEqual(packet.read_bytes(), before)

    def test_wrong_thread_identity_rejected(self):
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other"}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            bridge.rollout(self.home, THREAD)


if __name__ == "__main__":
    unittest.main()
