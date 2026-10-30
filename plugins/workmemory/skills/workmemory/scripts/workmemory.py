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
STATUSES = {"discussed": "讨论中", "proposed": "建议", "confirmed": "已确认"}


class MemoryError(ValueError):
    pass


def now():
    return datetime.now().astimezone()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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

    def records(self):
        records = []
        for candidate in sorted(self.root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.md")):
            relative = candidate.relative_to(self.root).as_posix()
            if not re.fullmatch(r"\d{4}-\d{2}/\d{4}-\d{2}-\d{2}\.md", relative):
                continue
            if candidate.stem[:7] != candidate.parent.name:
                raise MemoryError(f"Date and month disagree: {relative}")
            text = self.path(relative).read_text(encoding="utf-8")
            matches = list(BLOCK.finditer(text))
            leftover = BLOCK.sub("", text)
            if "<!-- workmemory:" in leftover:
                raise MemoryError(f"Broken record markers in {relative}; repair them before continuing.")
            for match in matches:
                body = match[2]
                metadata_match = META.search(body)
                if not metadata_match or len(META.findall(body)) != 1:
                    raise MemoryError(f"Missing or duplicated metadata in {match[1]}.")
                metadata = json.loads(metadata_match[1])
                required = {"session", "created_at", "fingerprint", "note_sha256", "status", "keywords", "supersedes"}
                if not isinstance(metadata, dict) or not required.issubset(metadata):
                    raise MemoryError(f"Invalid metadata in {match[1]}.")
                if (metadata["status"] not in STATUSES
                        or not isinstance(metadata["session"], str)
                        or not isinstance(metadata["keywords"], list)
                        or not all(isinstance(item, str) for item in metadata["keywords"])
                        or not isinstance(metadata["supersedes"], list)
                        or not all(isinstance(item, str) and re.fullmatch(ID_PATTERN, item) for item in metadata["supersedes"])):
                    raise MemoryError(f"Invalid metadata fields in {match[1]}.")
                lines = body.splitlines()
                heading = re.fullmatch(rf"## {match[1]} \| \d{{2}}:\d{{2}} — (.+)", lines[0]) if lines else None
                if not heading:
                    raise MemoryError(f"Invalid heading in {match[1]}.")
                note = META.sub("", body).strip()
                records.append({
                    "id": match[1], "title": heading[1], "file": str(candidate),
                    "relative": relative, "metadata": metadata, "note": note,
                    "sha256": digest(match[0]), "block": match[0],
                })
        ids = [record["id"] for record in records]
        if len(ids) != len(set(ids)):
            raise MemoryError("Duplicate record IDs found; repair the Markdown files first.")
        return records

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict):
            raise MemoryError("Save input must be a JSON object.")
        allowed = {"title", "conclusion", "status", "reasons", "constraints", "pending", "keywords", "supersedes"}
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
        if result["status"] not in STATUSES:
            raise MemoryError("status must be discussed, proposed, or confirmed.")
        for key in ("reasons", "constraints", "pending", "keywords", "supersedes"):
            items = payload.get(key, [])
            if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
                raise MemoryError(f"{key} must be an array of nonempty strings.")
            result[key] = list(dict.fromkeys(item.strip() for item in items))
        if any("\n" in keyword or "\r" in keyword for keyword in result["keywords"]):
            raise MemoryError("keywords must be single-line strings.")
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
        records = self.records()
        for record in records:
            meta = record["metadata"]
            if (meta["session"] == self.session and meta["fingerprint"] == fingerprint
                    and meta["note_sha256"] == digest(record["note"])):
                state["last_id"] = record["id"]
                self.write_state(state)
                return {"saved": True, "duplicate": True, **self.summary(record)}
        existing = {record["id"] for record in records}
        if not set(payload["supersedes"]).issubset(existing):
            raise MemoryError("A superseded record does not exist.")
        timestamp = now()
        day = timestamp.strftime("%Y-%m-%d")
        record_id = f"M{timestamp:%Y%m%d}-{uuid.uuid4().hex[:12]}"
        relative = f"{day[:7]}/{day}.md"
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
        block = f"<!-- workmemory:start {record_id} -->\n{note}\n<!-- workmemory:meta {json_text(metadata)} -->\n<!-- workmemory:end {record_id} -->"
        path = self.path(relative)
        previous = path.read_text(encoding="utf-8") if path.exists() else f"# {day}\n"
        self.write(relative, previous.rstrip() + "\n\n" + block + "\n")
        state["last_id"] = record_id
        if restore_deleted:
            state["deleted"] = [item for item in state.get("deleted", []) if item != fingerprint]
        # The note is authoritative. Report ancillary failures without hiding a successful write.
        result = {"saved": True, "duplicate": False, "id": record_id, "file": str(path), "title": payload["title"]}
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
        records = self.records()
        for record in records:
            if record["id"] == record_id:
                corrections = [self.summary(item) for item in records if record_id in item["metadata"]["supersedes"]]
                return {**self.summary(record), "sha256": record["sha256"],
                        "content": record["note"], "metadata": record["metadata"], "superseded_by": corrections}
        raise MemoryError(f"Record not found: {record_id}")

    def search(self, query=None, limit=20, offset=0):
        terms = list(dict.fromkeys(query.casefold().split())) if query is not None else []
        if query is not None and not terms:
            raise MemoryError("Search query cannot be empty.")
        matches = []
        for order, record in enumerate(self.records()):
            keywords = " ".join(record["metadata"]["keywords"])
            text = f"{record['title']}\n{keywords}\n{record['note']}".casefold()
            if terms and not any(term in text for term in terms):
                continue
            score = sum(3 * (term in record["title"].casefold()) + 2 * (term in keywords.casefold()) + (term in text) for term in terms)
            lines = record["note"].splitlines()
            evidence = next((line for line in lines if any(term in line.casefold() for term in terms)), record["title"])
            matches.append({**self.summary(record), "score": score, "preview": evidence[:240], "_order": order,
                            "status": record["metadata"]["status"], "keywords": record["metadata"]["keywords"]})
        matches.sort(key=lambda item: (item["score"], item["_order"]), reverse=True)
        for item in matches:
            del item["_order"]
        return {"total": len(matches), "offset": offset, "hits": matches[offset:offset + limit],
                "message": "" if matches else "目前记忆库中没有相关信息"}

    def delete(self, record_id, expected_sha256):
        self.read(record_id)
        record = next(item for item in self.records() if item["id"] == record_id)
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
        records = self.records()
        if not records:
            index = self.path("INDEX.md")
            if index.exists():
                index.unlink()
            return {"indexed": 0}
        lines = ["# WorkMemory Index", "", "<!-- Generated outline. Read the daily record for evidence. -->", ""]
        for record in records:
            title = record["title"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"- {record['id']} | [{title}]({record['relative']})")
            if record["metadata"]["keywords"]:
                lines.append("  关键词：" + "、".join(record["metadata"]["keywords"]))
        self.write("INDEX.md", "\n".join(lines) + "\n")
        return {"indexed": len(records), "file": str(self.path("INDEX.md"))}

    def refresh_index(self, result):
        try:
            self.reindex()
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
