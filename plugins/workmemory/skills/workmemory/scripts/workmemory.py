#!/usr/bin/env python3
"""Explicit local Markdown memory. Standard library only; no network access."""

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid


ID_PATTERN = r"M\d{8}-[0-9a-f]{12}"
BLOCK = re.compile(
    rf"<!-- workmemory:start ({ID_PATTERN}) -->\n(.*?)"
    rf"<!-- workmemory:end \1 -->", re.DOTALL
)
META = re.compile(r"^<!-- workmemory:meta (.+) -->$", re.MULTILINE)
START = re.compile(r"^<!-- workmemory:start[^\n]*$", re.MULTILINE)
STATUSES = {"discussed": "讨论中", "proposed": "建议", "confirmed": "已确认"}
# Small topic aliases improve recall without claiming semantic understanding.
ALIASES = (
    ("上线", "发布", "release", "deployment"),
    ("延期", "推迟", "延后", "往后推", "推后", "delay", "postpone"),
    ("备份", "backup"),
    ("检索", "搜索", "查找", "search", "retrieval"),
    ("删除", "移除", "delete", "remove"),
    ("本地", "local"),
)


class MemoryError(ValueError):
    pass


def now():
    return datetime.now().astimezone()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def contains(text, term):
    if re.fullmatch(r"[a-z0-9_-]+", term):
        return re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text) is not None
    return term in text


def query_groups(query):
    terms = list(dict.fromkeys(query.casefold().split()))
    if not terms:
        raise MemoryError("Search query cannot be empty.")
    groups = []
    for term in terms:
        exact = next((group for group in ALIASES if term in group), (term,))
        groups.append(exact)
        # Also recognize topic words inside an unsegmented Chinese question.
        groups.extend(group for group in ALIASES if any(contains(term, alias) for alias in group))
    return terms, list(dict.fromkeys(groups))


class Store:
    def __init__(self, root, session=None):
        self.root = Path(root).expanduser().resolve()
        self.session = session
        # Never allow personal notes under this plugin's source checkout/cache.
        plugin_root = Path(__file__).resolve().parents[3]
        source_root = next((path for path in plugin_root.parents if (path / ".git").exists()), plugin_root)
        if self.root == source_root or source_root in self.root.parents:
            raise MemoryError("Choose a memory directory outside the plugin source checkout.")

    def path(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise MemoryError(f"Path escapes memory directory: {relative}")
        current = path
        while current != self.root:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise MemoryError(f"Links inside the memory directory are not supported: {relative}")
            current = current.parent
        return path

    @contextmanager
    def lock(self):
        directory = self.path(".workmemory")
        directory.mkdir(parents=True, exist_ok=True)
        lock = self.path(".workmemory/write.lock")
        deadline = time.monotonic() + 5
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise MemoryError("Memory store is busy. If a writer crashed, verify it stopped before removing .workmemory/write.lock.")
                time.sleep(0.05)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            lock.unlink()

    def write(self, relative, text):
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".workmemory-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def state_path(self):
        if not self.session or len(self.session) > 512 or "\n" in self.session:
            raise MemoryError("A real chat ID is required: --session or CODEX_THREAD_ID.")
        return f".workmemory/sessions/{digest(self.session)}.json"

    def state(self):
        path = self.path(self.state_path())
        if not path.exists():
            return {"enabled": False, "last_id": None, "deleted": []}
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
            raise MemoryError("Invalid session state; refusing to overwrite it.")
        if not isinstance(state.get("deleted", []), list):
            raise MemoryError("Invalid deletion state.")
        return state

    def write_state(self, state):
        self.write(self.state_path(), json_text(state) + "\n")

    def enable(self):
        state = self.state()
        if not state["enabled"]:
            state.update(enabled=True, enabled_at=now().isoformat(timespec="seconds"))
            self.write_state(state)
        return self.status()

    def disable(self):
        state = self.state()
        state["enabled"] = False
        if self.root.exists():
            self.write_state(state)
        return self.status()

    def status(self):
        return {"root": str(self.root), "session": self.session, **self.state()}

    def scan(self):
        records, issues = [], []
        for candidate in sorted(self.root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.md")):
            relative = candidate.relative_to(self.root).as_posix()
            if not re.fullmatch(r"\d{4}-\d{2}/\d{4}-\d{2}-\d{2}\.md", relative):
                continue
            try:
                if candidate.stem[:7] != candidate.parent.name:
                    raise MemoryError("Date and month disagree.")
                text = self.path(relative).read_text(encoding="utf-8")
            except (OSError, ValueError) as error:
                issues.append({"file": str(candidate), "error": str(error)})
                continue
            starts = list(START.finditer(text))
            outside = [text[:starts[0].start()] if starts else text]
            for index, start in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
                segment = text[start.start():end]
                match = BLOCK.match(segment)
                record_id = re.search(ID_PATTERN, start[0])
                try:
                    if not match:
                        raise MemoryError("Broken record markers; repair this record before using it.")
                    outside.append(segment[match.end():])
                    records.append(self.parse_record(match, candidate, relative))
                except (ValueError, TypeError, KeyError) as error:
                    issues.append({"file": str(candidate), "id": record_id[0] if record_id else None, "error": str(error)})
            if any("<!-- workmemory:" in part or re.search(rf"^## {ID_PATTERN}\b", part, re.MULTILINE) for part in outside):
                issues.append({"file": str(candidate), "error": "Orphaned record markers or a record heading without markers."})
        counts = {}
        for record in records:
            counts[record["id"]] = counts.get(record["id"], 0) + 1
        for record in records:
            if counts[record["id"]] > 1:
                issues.append({"file": record["file"], "id": record["id"], "error": "Duplicate record ID; all ambiguous copies were skipped."})
        return [record for record in records if counts[record["id"]] == 1], issues

    @staticmethod
    def parse_record(match, candidate, relative):
        body = match[2]
        metadata_match = META.search(body)
        if not metadata_match or len(META.findall(body)) != 1:
            raise MemoryError("Missing or duplicated record metadata.")
        metadata = json.loads(metadata_match[1])
        required = {"session", "created_at", "fingerprint", "note_sha256", "status", "keywords", "supersedes"}
        if not isinstance(metadata, dict) or not required.issubset(metadata):
            raise MemoryError("Invalid record metadata.")
        for key in ("session", "created_at", "fingerprint", "note_sha256", "status"):
            if not isinstance(metadata[key], str) or not metadata[key]:
                raise MemoryError(f"Invalid metadata field: {key}.")
        if metadata["status"] not in STATUSES:
            raise MemoryError("Invalid record status.")
        if len(metadata["session"]) > 512 or "\n" in metadata["session"]:
            raise MemoryError("Invalid record session.")
        datetime.fromisoformat(metadata["created_at"])
        for key in ("fingerprint", "note_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", metadata[key]):
                raise MemoryError(f"Invalid metadata hash: {key}.")
        for key in ("keywords", "supersedes", "aliases"):
            items = metadata.get(key, [])
            if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
                raise MemoryError(f"Invalid metadata array: {key}.")
        if any(not re.fullmatch(ID_PATTERN, item) for item in metadata["supersedes"]):
            raise MemoryError("Invalid superseding record ID.")
        lines = body.splitlines()
        heading = re.fullmatch(rf"## {match[1]} \| \d{{2}}:\d{{2}} — (.+)", lines[0]) if lines else None
        if not heading:
            raise MemoryError("Invalid record heading.")
        note = META.sub("", body).strip()
        if "<!-- workmemory:" in note:
            raise MemoryError("Unexpected record marker inside note content.")
        return {"id": match[1], "title": heading[1], "file": str(candidate), "relative": relative,
                "metadata": metadata, "note": note, "sha256": digest(match[0]), "block": match[0]}

    @staticmethod
    def completeness(issues):
        return {"incomplete": bool(issues), "warnings": issues}

    @staticmethod
    def receipt(payload):
        fields = ("title", "conclusion", "reasons", "constraints", "pending")
        size = sum(len(payload[key]) if isinstance(payload[key], str) else sum(map(len, payload[key])) for key in fields)
        return {**{key: payload[key] for key in fields}, "status": STATUSES[payload["status"]], "content_characters": size}

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict):
            raise MemoryError("Save input must be a JSON object.")
        allowed = {"title", "conclusion", "status", "reasons", "constraints", "pending", "keywords", "supersedes", "aliases"}
        if set(payload) - allowed:
            raise MemoryError(f"Unknown input fields: {', '.join(sorted(set(payload) - allowed))}")
        result = {}
        for key in ("title", "conclusion"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise MemoryError(f"{key} must be a nonempty string.")
            result[key] = value.strip()
        if "\n" in result["title"] or "\r" in result["title"]:
            raise MemoryError("title must be a single line.")
        result["status"] = payload.get("status", "discussed")
        if not isinstance(result["status"], str) or result["status"] not in STATUSES:
            raise MemoryError("status must be discussed, proposed, or confirmed.")
        for key in ("reasons", "constraints", "pending", "keywords", "supersedes", "aliases"):
            items = payload.get(key, [])
            if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
                raise MemoryError(f"{key} must be an array of nonempty strings.")
            result[key] = list(dict.fromkeys(item.strip() for item in items))
        if any("\n" in item or "\r" in item for item in result["keywords"] + result["aliases"]):
            raise MemoryError("keywords and aliases must be single-line strings.")
        if not result["aliases"]:
            del result["aliases"]  # Keep v0.1 fingerprints stable when no aliases are supplied.
        if any(not re.fullmatch(ID_PATTERN, item) for item in result["supersedes"]):
            raise MemoryError("supersedes must contain exact record IDs.")
        if "<!-- workmemory:" in json_text(result):
            raise MemoryError("Reserved record markers are not allowed in note content.")
        return result

    def save(self, payload, restore_deleted=False):
        state = self.state()
        if not state["enabled"]:
            raise MemoryError("Recording is disabled for this chat. Explicitly enable it before saving.")
        payload = self.validate(payload)
        fingerprint = digest(json_text(payload))
        if fingerprint in state.get("deleted", []) and not restore_deleted:
            raise MemoryError("This exact content was deleted in this chat. Use --restore-deleted only for an explicit re-save request.")
        records, issues = self.scan()
        for record in records:
            meta = record["metadata"]
            if (meta["session"] == self.session and meta["fingerprint"] == fingerprint
                    and meta["note_sha256"] == digest(record["note"])):
                state["last_id"] = record["id"]
                self.write_state(state)
                return {"saved": True, "duplicate": True, **self.summary(record),
                        "receipt": self.receipt(payload), **self.completeness(issues)}
        existing = {record["id"] for record in records}
        if not set(payload["supersedes"]).issubset(existing):
            raise MemoryError("A superseded record does not exist.")
        timestamp = now()
        day = timestamp.strftime("%Y-%m-%d")
        record_id = f"M{timestamp:%Y%m%d}-{uuid.uuid4().hex[:12]}"
        relative = f"{day[:7]}/{day}.md"
        if any(issue["file"] == str(self.root / relative) for issue in issues):
            raise MemoryError(f"The destination day file has damaged records; repair it before appending: {relative}")
        lines = [f"## {record_id} | {timestamp:%H:%M} — {payload['title']}", "",
                 f"- 状态：{STATUSES[payload['status']]}", f"- 结论：{payload['conclusion']}"]
        for key, label in (("reasons", "原因"), ("constraints", "边界"), ("pending", "待定")):
            lines.extend(f"- {label}：{item}" for item in payload[key])
        if payload["supersedes"]:
            lines.append("- 替代：" + ", ".join(payload["supersedes"]))
        note = "\n".join(lines)
        metadata = {"session": self.session, "enabled_at": state["enabled_at"],
                    "created_at": timestamp.isoformat(timespec="seconds"), "fingerprint": fingerprint,
                    "note_sha256": digest(note), "status": payload["status"],
                    "keywords": payload["keywords"], "supersedes": payload["supersedes"]}
        if payload.get("aliases"):
            metadata["aliases"] = payload["aliases"]
        block = f"<!-- workmemory:start {record_id} -->\n{note}\n<!-- workmemory:meta {json_text(metadata)} -->\n<!-- workmemory:end {record_id} -->"
        path = self.path(relative)
        previous = path.read_text(encoding="utf-8") if path.exists() else f"# {day}\n"
        self.write(relative, previous.rstrip() + "\n\n" + block + "\n")
        state["last_id"] = record_id
        if restore_deleted:
            state["deleted"] = [item for item in state.get("deleted", []) if item != fingerprint]
        # The note is authoritative. Report ancillary failures without hiding a successful write.
        result = {"saved": True, "duplicate": False, "id": record_id, "file": str(path),
                  "title": payload["title"], "receipt": self.receipt(payload)}
        try:
            self.write_state(state)
        except OSError as error:
            result["state_warning"] = str(error)
        return self.refresh_index(result)

    @staticmethod
    def summary(record):
        return {key: record[key] for key in ("id", "title", "file")}

    def read(self, record_id=None, latest=False):
        if latest:
            record_id = self.state().get("last_id")
            if not record_id:
                raise MemoryError("No current saved record in this chat. Specify an exact record ID.")
        if not record_id or not re.fullmatch(ID_PATTERN, record_id):
            raise MemoryError("Invalid record ID.")
        records, issues = self.scan()
        for record in records:
            if record["id"] == record_id:
                corrections = [self.summary(item) for item in records if record_id in item["metadata"]["supersedes"]]
                return {**self.summary(record), "sha256": record["sha256"],
                        "content": record["note"], "metadata": record["metadata"], "superseded_by": corrections,
                        **self.completeness(issues)}
        if issues:
            raise MemoryError(f"Record {record_id} was not readable; the scan is incomplete. Inspect search/list warnings for damaged file locations.")
        raise MemoryError(f"Record not found: {record_id}")

    def search(self, query=None, limit=20, offset=0):
        terms, groups = query_groups(query) if query is not None else ([], [])
        expanded = list(dict.fromkeys(term for group in groups for term in group))
        records, issues = self.scan()
        matches = []
        for order, record in enumerate(records):
            keywords = " ".join(record["metadata"]["keywords"])
            aliases = " ".join(record["metadata"].get("aliases", []))
            fields = [record["title"].casefold(), f"{keywords}\n{aliases}".casefold(), record["note"].casefold()]
            text = "\n".join(fields)
            matched = [term for term in expanded if contains(text, term)]
            if terms and not matched:
                continue
            def weight(term):
                return sum(multiplier for field, multiplier in zip(fields, (3, 2, 1)) if contains(field, term))
            score = 10 * sum(weight(term) for term in terms) + sum(max(map(weight, group)) for group in groups)
            lines = record["note"].splitlines()
            evidence = next((line for line in lines if any(contains(line.casefold(), term) for term in matched)), record["title"])
            matches.append({**self.summary(record), "score": score, "preview": evidence[:240], "_order": order,
                            "status": record["metadata"]["status"], "keywords": record["metadata"]["keywords"],
                            "matched_terms": matched,
                            "matched_by": "literal" if any(contains(f"{record['note']}\n{keywords}".casefold(), term) for term in terms) else "alias"})
        matches.sort(key=lambda item: (item["score"], item["_order"]), reverse=True)
        for item in matches:
            del item["_order"]
        return {"total": len(matches), "offset": offset, "hits": matches[offset:offset + limit],
                "query_terms": terms, "expanded_terms": expanded, **self.completeness(issues),
                "message": "部分记录无法读取，检索不完整；不能确认是否没有相关信息" if issues else ("" if matches else "目前记忆库中没有相关信息")}

    def delete(self, record_id, expected_sha256):
        self.read(record_id)
        records, _ = self.scan()
        record = next(item for item in records if item["id"] == record_id)
        if record["sha256"] != expected_sha256:
            raise MemoryError("Record changed since inspection. Read it again before deleting.")
        # Persist the tombstone first; a retry can safely finish removing the same block.
        owner = Store(self.root, record["metadata"]["session"])
        state = owner.state()
        state["deleted"] = list(dict.fromkeys([*state.get("deleted", []), record["metadata"]["fingerprint"]]))
        if state.get("last_id") == record_id:
            state["last_id"] = None
        owner.write_state(state)
        path = self.path(record["relative"])
        text = path.read_text(encoding="utf-8").replace(record["block"], "", 1)
        if text.strip() == f"# {path.stem}":
            path.unlink()
        else:
            self.write(record["relative"], text.rstrip() + "\n")
        return self.refresh_index({"deleted": True, **self.summary(record)})

    def reindex(self):
        records, issues = self.scan()
        if not records and not issues:
            index = self.path("INDEX.md")
            if index.exists():
                index.unlink()
            return {"indexed": 0}
        lines = ["# WorkMemory Index", "", "<!-- Generated outline. Read the daily record for evidence. -->", ""]
        if issues:
            lines.extend([f"> 索引不完整：有 {len(issues)} 项读取问题。运行 list 查看位置，修复后重新生成索引。", ""])
        for record in records:
            title = record["title"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"- {record['id']} | [{title}]({record['relative']})")
            if record["metadata"]["keywords"]:
                lines.append("  关键词：" + "、".join(record["metadata"]["keywords"]))
            if record["metadata"].get("aliases"):
                lines.append("  别名：" + "、".join(record["metadata"]["aliases"]))
        self.write("INDEX.md", "\n".join(lines) + "\n")
        return {"indexed": len(records), "file": str(self.path("INDEX.md")), **self.completeness(issues)}

    def refresh_index(self, result):
        try:
            index = self.reindex()
            result.update(self.completeness(index.get("warnings", [])))
        except (OSError, ValueError) as error:
            result["index_warning"] = str(error)
        return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("WORKMEMORY_ROOT", str(Path.home() / "WorkMemory")))
    parser.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("enable", "disable", "status", "reindex"):
        commands.add_parser(name)
    save = commands.add_parser("save")
    save.add_argument("--input", type=Path, help="UTF-8 JSON file; defaults to stdin")
    save.add_argument("--restore-deleted", action="store_true")
    for name in ("search", "list"):
        command = commands.add_parser(name)
        if name == "search":
            command.add_argument("--query", required=True)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)
    read = commands.add_parser("read")
    target = read.add_mutually_exclusive_group(required=True)
    target.add_argument("--id")
    target.add_argument("--latest", action="store_true")
    delete = commands.add_parser("delete")
    delete.add_argument("--id", required=True)
    delete.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        store = Store(args.root, args.session)
        if args.command == "enable":
            store.state_path()  # Reject a missing session ID before creating anything.
            store.root.mkdir(parents=True, exist_ok=True)
        if args.command in ("search", "list") and (not 1 <= args.limit <= 200 or args.offset < 0):
            raise MemoryError("limit must be 1..200 and offset must be nonnegative.")
        with store.lock() if store.root.exists() else nullcontext():
            if args.command == "save":
                raw = args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.buffer.read().decode("utf-8-sig")
                result = store.save(json.loads(raw), args.restore_deleted)
            elif args.command == "read":
                result = store.read(args.id, args.latest)
            elif args.command == "delete":
                result = store.delete(args.id, args.expected_sha256)
            elif args.command in ("search", "list"):
                result = store.search(getattr(args, "query", None), args.limit, args.offset)
            else:
                result = getattr(store, args.command)()
        print(json_text(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json_text({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
