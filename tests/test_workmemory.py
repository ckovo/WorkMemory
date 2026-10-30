import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "plugins/workmemory/skills/workmemory/scripts/workmemory.py"
spec = importlib.util.spec_from_file_location("workmemory", SCRIPT)
wm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wm)
CLOCK = datetime(2026, 10, 30, 15, 20, tzinfo=timezone(timedelta(hours=8)))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "WorkMemory").resolve()
        self.store = wm.Store(self.root, "chat-one")
        timer = patch.object(wm, "now", return_value=CLOCK)
        timer.start()
        self.addCleanup(timer.stop)
        self.payload = {"title": "保存规则", "conclusion": "仅在用户说记下来时保存。", "status": "confirmed",
                        "reasons": ["便于查看与备份"], "constraints": ["不自动上传个人记忆"],
                        "keywords": ["Markdown", "按月归档"]}

    def save(self, payload=None):
        self.store.enable()
        return self.store.save(payload or self.payload)

    def test_disabled_and_empty_reads_create_no_files(self):
        self.assertFalse(self.store.status()["enabled"])
        self.assertEqual(self.store.search("nothing")["total"], 0)
        self.assertEqual(self.store.reindex()["indexed"], 0)
        self.store.disable()
        self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(wm.MemoryError, "disabled"):
            self.store.save(self.payload)
        self.assertFalse(self.root.exists())

    def test_enable_is_session_scoped_and_creates_no_markdown(self):
        self.store.enable()
        self.assertFalse(list(self.root.rglob("*.md")))
        other = wm.Store(self.root, "chat-two")
        self.assertFalse(other.status()["enabled"])
        with self.assertRaisesRegex(wm.MemoryError, "disabled"):
            other.save(self.payload)
        self.store.disable()
        with self.assertRaisesRegex(wm.MemoryError, "disabled"):
            self.store.save(self.payload)

    def test_save_append_read_and_outline(self):
        first = self.save()
        second = self.save({**self.payload, "title": "检索方案", "conclusion": "先找索引，再读正文。"})
        self.assertEqual(first["file"], str(self.root / "2026-10/2026-10-30.md"))
        self.assertEqual(len(list(self.root.glob("*/*.md"))), 1)
        self.assertIn("不自动上传", self.store.read(first["id"])["content"])
        self.assertEqual(self.store.read(latest=True)["id"], second["id"])
        index = (self.root / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn(first["id"], index)
        self.assertIn(second["id"], index)
        self.assertEqual(self.store.search()["hits"][0]["id"], second["id"])

    def test_local_midnight_and_month_rollover(self):
        self.save()
        with patch.object(wm, "now", return_value=CLOCK.replace(month=11, day=1, hour=0)):
            saved = self.store.save({**self.payload, "conclusion": "次月新结论"})
        self.assertTrue(saved["file"].endswith(str(Path("2026-11/2026-11-01.md"))))
        self.assertEqual(len(list(self.root.glob("*/*.md"))), 2)

    def test_exact_duplicate_is_idempotent(self):
        first = self.save()
        second = self.save()
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.search()["total"], 1)

    def test_search_reads_bodies_and_ignores_stale_index(self):
        saved = self.save()
        (self.root / "INDEX.md").write_text("stale", encoding="utf-8")
        self.assertEqual(self.store.search("备份")["hits"][0]["id"], saved["id"])
        self.assertEqual(self.store.search("markdown")["total"], 1)
        self.assertEqual(self.store.search("不存在")["total"], 0)
        self.assertEqual(self.store.search("不存在")["message"], "目前记忆库中没有相关信息")

    def test_search_paging_does_not_drop_total(self):
        self.save()
        self.store.save({**self.payload, "title": "第二条"})
        self.assertEqual(self.store.search(limit=1)["total"], 2)
        self.assertNotEqual(self.store.search(limit=1)["hits"][0]["id"], self.store.search(limit=1, offset=1)["hits"][0]["id"])

    def test_manual_edits_are_visible_and_not_deduplicated_away(self):
        saved = self.save()
        path = Path(saved["file"])
        path.write_text(path.read_text(encoding="utf-8").replace("仅在用户说记下来时保存。", "人工修正后的结论。"), encoding="utf-8")
        self.assertEqual(self.store.search("人工修正")["total"], 1)
        self.assertFalse(self.store.save(self.payload)["duplicate"])

    def test_delete_preserves_neighbors_and_updates_outline(self):
        first = self.save()
        second = self.save({**self.payload, "title": "保留此条"})
        self.store.delete(first["id"], self.store.read(first["id"])["sha256"])
        self.assertEqual(self.store.search()["total"], 1)
        self.assertEqual(self.store.read(second["id"])["title"], "保留此条")
        self.assertNotIn(first["id"], (self.root / "INDEX.md").read_text(encoding="utf-8"))

    def test_delete_last_removes_empty_day_and_prevents_recreation(self):
        saved = self.save()
        note = self.store.read(saved["id"])
        self.store.delete(saved["id"], note["sha256"])
        self.assertFalse(Path(saved["file"]).exists())
        self.assertFalse((self.root / "INDEX.md").exists())
        with self.assertRaisesRegex(wm.MemoryError, "deleted"):
            self.store.save(self.payload)
        with self.assertRaisesRegex(wm.MemoryError, "No current"):
            self.store.read(latest=True)
        self.assertTrue(self.store.save(self.payload, restore_deleted=True)["saved"])

    def test_delete_does_not_remove_unmanaged_text(self):
        saved = self.save()
        path = Path(saved["file"])
        path.write_text(path.read_text(encoding="utf-8") + "\n人工补充备注\n", encoding="utf-8")
        self.store.delete(saved["id"], self.store.read(saved["id"])["sha256"])
        self.assertIn("人工补充备注", path.read_text(encoding="utf-8"))

    def test_delete_requires_current_content_hash(self):
        saved = self.save()
        old = self.store.read(saved["id"])
        path = Path(saved["file"])
        path.write_text(path.read_text(encoding="utf-8").replace("便于查看", "便于检索"), encoding="utf-8")
        with self.assertRaisesRegex(wm.MemoryError, "changed"):
            self.store.delete(saved["id"], old["sha256"])
        self.assertEqual(self.store.search()["total"], 1)

    def test_current_record_never_falls_back_to_another_chat(self):
        saved = self.save()
        other = wm.Store(self.root, "chat-two")
        with self.assertRaisesRegex(wm.MemoryError, "No current"):
            other.read(latest=True)
        self.assertEqual(other.read(saved["id"])["id"], saved["id"])
        other.delete(saved["id"], other.read(saved["id"])["sha256"])
        self.assertIsNone(self.store.status()["last_id"])

    def test_explicit_corrections_link_both_directions(self):
        first = self.save()
        second = self.save({**self.payload, "conclusion": "改为手动保存", "supersedes": [first["id"]]})
        self.assertEqual(self.store.read(first["id"])["superseded_by"][0]["id"], second["id"])
        self.assertEqual(self.store.read(second["id"])["metadata"]["supersedes"], [first["id"]])

    def test_invalid_input_creates_no_day_file(self):
        self.store.enable()
        for payload in ({}, {**self.payload, "title": "two\nlines"}, {**self.payload, "unknown": 1},
                        {**self.payload, "status": "sure"}, {**self.payload, "reasons": "not an array"},
                        {**self.payload, "conclusion": "<!-- workmemory:start X -->"},
                        {**self.payload, "supersedes": ["../../bad"]}):
            with self.subTest(payload=payload), self.assertRaises(wm.MemoryError):
                self.store.save(payload)
        self.assertFalse(list(self.root.rglob("*.md")))

    def test_broken_markers_report_incomplete_not_empty_search(self):
        saved = self.save()
        path = Path(saved["file"])
        path.write_text(path.read_text(encoding="utf-8").replace("workmemory:end", "workmemory:broken"), encoding="utf-8")
        result = self.store.search("missing")
        self.assertTrue(result["incomplete"])
        self.assertIn("检索不完整", result["message"])
        self.assertNotEqual(result["message"], "目前记忆库中没有相关信息")
        self.assertEqual(result["warnings"][0]["file"], str(path))

    def test_receipt_preserves_meaning_and_does_not_truncate(self):
        payload = {**self.payload, "conclusion": "建议在 2026-11-03 前完成；仅限本机，不允许云端同步。\n" + "必须保留条件。" * 60,
                   "status": "proposed", "pending": ["用户尚未确认截止日期"], "reasons": []}
        saved = self.save(payload)
        receipt = saved["receipt"]
        self.assertEqual(receipt["conclusion"], payload["conclusion"])
        self.assertEqual(receipt["status"], "建议")
        self.assertEqual(receipt["reasons"], [])
        self.assertEqual(receipt["pending"], payload["pending"])
        self.assertGreater(receipt["content_characters"], 300)
        self.assertIn(payload["conclusion"], self.store.read(saved["id"])["content"])
        self.assertEqual(self.store.save(payload)["receipt"], receipt)

    def test_builtin_aliases_find_chinese_and_english_wording(self):
        saved = self.save({"title": "进度调整", "conclusion": "上线时间往后推一周。"})
        for query in ("发布 延期", "release delay", "版本发布延期"):
            with self.subTest(query=query):
                result = self.store.search(query)
                self.assertEqual(result["hits"][0]["id"], saved["id"])
                self.assertIn("往后推", result["hits"][0]["matched_terms"])
                self.assertEqual(result["hits"][0]["matched_by"], "alias")
                self.assertFalse(result["incomplete"])

    def test_exact_wording_ranks_above_alias_wording(self):
        exact = self.save({"title": "发布延期", "conclusion": "发布延期一周。"})
        self.save({"title": "上线推迟", "conclusion": "上线时间往后推一周。"})
        result = self.store.search("发布 延期")
        self.assertEqual(result["hits"][0]["id"], exact["id"])
        self.assertEqual(result["hits"][0]["matched_by"], "literal")

    def test_record_aliases_are_optional_searchable_and_deduplicated(self):
        self.save()
        payload = {"title": "WorkMemory", "conclusion": "使用每日文件。", "aliases": ["工作记忆系统", "个人笔记", "个人笔记"]}
        saved = self.save(payload)
        result = self.store.search("工作记忆系统")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["id"], saved["id"])
        self.assertEqual(result["hits"][0]["matched_by"], "alias")
        self.assertEqual(self.store.read(saved["id"])["metadata"]["aliases"], ["工作记忆系统", "个人笔记"])
        self.assertTrue(self.store.save(payload)["duplicate"])
        self.assertIn("个人笔记", (self.root / "INDEX.md").read_text(encoding="utf-8"))

    def test_english_alias_does_not_match_inside_another_word(self):
        self.save({"title": "prerelease", "conclusion": "redeployment checklist"})
        self.assertEqual(self.store.search("release")["total"], 0)

    def test_broken_neighbor_does_not_hide_valid_record(self):
        broken = self.save()
        good = self.save({"title": "有效方案", "conclusion": "仍应能读取此记录。"})
        path = Path(broken["file"])
        path.write_text(path.read_text(encoding="utf-8").replace(f"workmemory:end {broken['id']}", f"workmemory:broken {broken['id']}"), encoding="utf-8")
        original = path.read_bytes()
        found = self.store.search("有效方案")
        self.assertEqual(found["hits"][0]["id"], good["id"])
        self.assertTrue(found["incomplete"])
        self.assertEqual(found["warnings"][0]["id"], broken["id"])
        read = self.store.read(good["id"])
        self.assertTrue(read["incomplete"])
        self.assertIn("仍应能读取", read["content"])
        index = self.store.reindex()
        self.assertTrue(index["incomplete"])
        self.assertEqual(index["indexed"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("索引不完整", (self.root / "INDEX.md").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(wm.MemoryError, "incomplete"):
            self.store.read(broken["id"])

    def test_missing_start_marker_reports_unmanaged_record(self):
        broken = self.save()
        good = self.save({"title": "正常邻居", "conclusion": "保留"})
        path = Path(broken["file"])
        path.write_text(path.read_text(encoding="utf-8").replace(f"<!-- workmemory:start {broken['id']} -->", ""), encoding="utf-8")
        result = self.store.search()
        self.assertTrue(result["incomplete"])
        self.assertEqual([item["id"] for item in result["hits"]], [good["id"]])

    def test_bad_metadata_is_isolated_from_healthy_neighbors(self):
        broken = self.save()
        good = self.save({"title": "正常记录", "conclusion": "健康数据"})
        path = Path(broken["file"])
        original = path.read_text(encoding="utf-8")
        metadata = wm.META.search(original)
        for replacement in ("{bad json}", json.dumps({"status": []}), "null"):
            with self.subTest(metadata=replacement):
                path.write_text(original[:metadata.start(1)] + replacement + original[metadata.end(1):], encoding="utf-8")
                found = self.store.search()
                self.assertTrue(found["incomplete"])
                self.assertEqual([item["id"] for item in found["hits"]], [good["id"]])

    def test_unreadable_encoding_is_reported_without_hiding_other_days(self):
        good = self.save()
        invalid = self.root / "2026-10/2026-10-29.md"
        invalid.write_bytes(b"\xff\xfe\xff")
        result = self.store.search("保存规则")
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["hits"][0]["id"], good["id"])
        self.assertEqual(result["warnings"][0]["file"], str(invalid))
        self.assertEqual(invalid.read_bytes(), b"\xff\xfe\xff")

    def test_duplicate_ids_skip_all_ambiguous_copies(self):
        duplicate = self.save()
        good = self.save({"title": "唯一记录", "conclusion": "可以读取"})
        records, _ = self.store.scan()
        block = next(item["block"] for item in records if item["id"] == duplicate["id"])
        path = Path(duplicate["file"])
        path.write_text(path.read_text(encoding="utf-8") + "\n" + block + "\n", encoding="utf-8")
        result = self.store.search()
        self.assertTrue(result["incomplete"])
        self.assertEqual([item["id"] for item in result["hits"]], [good["id"]])
        with self.assertRaisesRegex(wm.MemoryError, "incomplete"):
            self.store.read(duplicate["id"])

    def test_writes_refuse_damaged_destination_but_allow_other_days(self):
        saved = self.save()
        path = Path(saved["file"])
        path.write_text(path.read_text(encoding="utf-8").replace("workmemory:end", "workmemory:broken"), encoding="utf-8")
        original = path.read_bytes()
        with self.assertRaisesRegex(wm.MemoryError, "destination day"):
            self.store.save({"title": "不应写入", "conclusion": "拒绝追加"})
        with patch.object(wm, "now", return_value=CLOCK.replace(day=31)):
            new = self.store.save({"title": "次日记录", "conclusion": "可以正常保存"})
        self.assertTrue(new["saved"])
        self.assertTrue(new["incomplete"])
        self.assertEqual(path.read_bytes(), original)

    def test_delete_healthy_neighbor_preserves_broken_block(self):
        broken = self.save()
        good = self.save({"title": "可删除", "conclusion": "单条删除"})
        path = Path(broken["file"])
        path.write_text(path.read_text(encoding="utf-8").replace(f"workmemory:end {broken['id']}", f"workmemory:broken {broken['id']}"), encoding="utf-8")
        result = self.store.delete(good["id"], self.store.read(good["id"])["sha256"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["incomplete"])
        text = path.read_text(encoding="utf-8")
        self.assertIn(broken["id"], text)
        self.assertNotIn(good["id"], text)

    def test_out_of_root_paths_and_checkout_storage_are_rejected(self):
        with self.assertRaisesRegex(wm.MemoryError, "escapes"):
            self.store.path("../escape.md")
        with self.assertRaisesRegex(wm.MemoryError, "outside"):
            wm.Store(REPO / "personal-notes", "one")
        with self.assertRaisesRegex(wm.MemoryError, "chat ID"):
            wm.Store(self.root).enable()

    def test_symlinked_month_is_rejected(self):
        self.store.enable()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (self.root / "2026-10").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit symlinks")
        with self.assertRaises(wm.MemoryError):
            self.store.save(self.payload)
        self.assertFalse(list(outside.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_is_rejected(self):
        import _winapi
        self.store.enable()
        outside = Path(self.temp.name) / "junction-target"
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(self.root / "2026-10"))
        with self.assertRaises(wm.MemoryError):
            self.store.save(self.payload)
        self.assertFalse(list(outside.iterdir()))

    def test_note_still_succeeds_if_index_write_fails(self):
        self.store.enable()
        with patch.object(self.store, "reindex", side_effect=OSError("index is read-only")):
            saved = self.store.save(self.payload)
        self.assertTrue(saved["saved"])
        self.assertIn("index_warning", saved)
        self.assertEqual(self.store.read(saved["id"])["title"], self.payload["title"])

    def test_atomic_write_failure_preserves_existing_note(self):
        saved = self.save()
        original = Path(saved["file"]).read_bytes()
        with patch.object(wm.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.save({**self.payload, "title": "new"})
        self.assertEqual(Path(saved["file"]).read_bytes(), original)
        self.assertFalse(list(self.root.rglob("*.tmp")))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "WorkMemory").resolve()

    def command(self, *args, payload=None, session="cli-chat"):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--session", session, *args],
                              input=json.dumps(payload, ensure_ascii=False) if payload else None,
                              capture_output=True, encoding="utf-8", timeout=15)

    def test_cli_utf8_lifecycle(self):
        self.assertEqual(self.command("enable").returncode, 0)
        saved = self.command("save", payload={"title": "编码检查", "conclusion": "中文与 emoji 🐱 保留原意。"})
        self.assertEqual(saved.returncode, 0, saved.stderr)
        record_id = json.loads(saved.stdout)["id"]
        found = self.command("search", "--query", "中文")
        self.assertEqual(json.loads(found.stdout)["total"], 1)
        note = json.loads(self.command("read", "--id", record_id).stdout)
        self.assertIn("🐱", note["content"])
        deleted = self.command("delete", "--id", record_id, "--expected-sha256", note["sha256"])
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(self.command("list").stdout)["total"], 0)

    def test_bad_cli_input_is_error_and_releases_lock(self):
        self.command("enable")
        invalid = self.command("save", payload={"bad": "input"})
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("error", json.loads(invalid.stderr))
        self.assertFalse((self.root / ".workmemory/write.lock").exists())
        self.assertNotEqual(self.command("search", "--query", "x", "--limit", "0").returncode, 0)

    def test_cli_reports_receipt_and_incomplete_results_separately_from_errors(self):
        self.command("enable")
        saved = json.loads(self.command("save", payload={"title": "上线调整", "conclusion": "上线时间往后推。"}).stdout)
        self.assertEqual(saved["receipt"]["conclusion"], "上线时间往后推。")
        # Use the saved month, independent of the machine's current date.
        bad = Path(saved["file"]).with_name(Path(saved["file"]).stem[:7] + "-01.md")
        if bad == Path(saved["file"]):
            bad = bad.with_name(Path(saved["file"]).stem[:7] + "-02.md")
        bad.write_bytes(b"\xff")
        result = self.command("search", "--query", "发布延期")
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertTrue(found["incomplete"])
        self.assertEqual(found["hits"][0]["id"], saved["id"])
        missing = json.loads(self.command("search", "--query", "不存在的资料").stdout)
        self.assertEqual(missing["total"], 0)
        self.assertIn("检索不完整", missing["message"])
        self.assertNotEqual(missing["message"], "目前记忆库中没有相关信息")

    def test_concurrent_saves_do_not_lose_records(self):
        self.command("enable")
        processes = []
        for number in range(6):
            process = subprocess.Popen([sys.executable, str(SCRIPT), "--root", str(self.root), "--session", "cli-chat", "save"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
            process.stdin.write(json.dumps({"title": f"record {number}", "conclusion": "keep every writer"}))
            process.stdin.close()
            process.stdin = None
            processes.append(process)
        for process in processes:
            output, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
            self.assertTrue(json.loads(output)["saved"])
        self.assertEqual(json.loads(self.command("list").stdout)["total"], 6)
        self.assertFalse((self.root / ".workmemory/write.lock").exists())


if __name__ == "__main__":
    unittest.main()
