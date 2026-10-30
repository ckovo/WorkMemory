# WorkMemory

A Codex plugin for saving discussion conclusions, decisions, and reasoning as concise **local Markdown** notes, then finding them in another chat.

第一版聚焦：**会话内开启 → 说“记下来” → 本地保存 → 跨会话查询 → 指定删除**。

**v0.2.0** adds faithful post-save receipts, Chinese/English topic aliases and per-record aliases, and partial retrieval when individual records are damaged. Existing v0.1 records remain readable without migration.

## Install in Codex

Requires Python 3.10+ on the local execution host and a Codex version supporting local/repository plugins. No Python packages, API keys, database, MCP server, or model download are required by this plugin.

1. Add this repository as a marketplace:

   ```powershell
   codex plugin marketplace add https://github.com/ckovo/WorkMemory.git
   ```

2. Open Codex's Plugins Directory, select the **WorkMemory** marketplace (`workmemory-local`), and install **WorkMemory**. Start a new local chat with the plugin available. UI wording and repository marketplace availability depend on your Codex version; adding a marketplace alone does not install or activate the plugin.
3. Invoke the plugin/skill and say **“开启 WorkMemory 记录”**. Check its activation confirmation before discussing the topic. Enabling is per chat, not global.

The repository includes a portable `plugin.json` and the supported Codex compatibility manifest. The catalog is `.agents/plugins/marketplace.json`; the plugin is `plugins/workmemory`. See [official plugin packaging and marketplace instructions](https://developers.openai.com/plugins/build/plugins).

## Use

| Say | Behavior |
| --- | --- |
| `开启 WorkMemory 记录` | Enable this chat; no dated file is created. |
| `把刚才的方案和原因记下来` | Condense the requested discussion and append a record. |
| `从 WorkMemory 找一下，之前为什么这样设计？` | Search saved records, read evidence, and cite the source. |
| `删除刚才保存的那条记录` | Delete this chat's most recently saved record. |
| `删除 M20261030-012345abcdef` | Delete the exact record identified by ID. |
| `关闭 WorkMemory 记录` | Stop allowing saves in this chat; existing notes remain. |

If a request is not routed to the plugin automatically, explicitly select WorkMemory or invoke its `workmemory` skill. This is a skill-based plugin using Codex's local shell/file tools, not a background recorder or a new custom toolbar switch. The script enforces the save gate; deciding which discussion is in scope and compressing it are performed by the host AI. A stable host chat ID is required for session isolation. If unavailable, the skill asks for a unique explicit session name.

### Storage

Default location: `~/WorkMemory` (`C:\Users\<you>\WorkMemory` on Windows). Set a persistent user environment variable `WORKMEMORY_ROOT` before starting Codex to choose another location. Every chat must use the same root to share memories. Changing it does not migrate old records.

```text
WorkMemory/
  INDEX.md                  # Generated outline; can be rebuilt
  2026-10/
    2026-10-30.md            # Created only when a record is saved
  .workmemory/
    sessions/<hash>.json     # Enable time, latest ID, deleted-content hashes
```

Dates follow the execution computer's local time zone at save time. A day can contain several independently identified records. Saving appends; it does not overwrite an earlier discussion. Empty days have no file, and empty month directories are not created by enabling. Deleting a day's final record removes its generated day file; an empty month folder may remain.

The `.workmemory` JSON files hold runtime state only, not a second copy of note content. Markdown is authoritative. Small HTML comments delimit records and retain source/session metadata; do not remove these comments or change IDs when manually editing. You may edit the readable title and body, then ask to rebuild the index. Unmarked freeform text is preserved but is not indexed as a managed record. A generated record heading with missing markers is reported as damaged. Reindexing never reconstructs lost session consent state.

### Concise, faithful records

The skill first identifies the conclusion, decision status, reasons, boundaries, exact values, and unresolved actions, then compares the condensed draft against that checklist. It removes repetition and omits empty fields. Ordinary notes aim for 150–300 Chinese characters, without a hard limit that could silently truncate details. Technical metadata is additional. AI suggestions remain proposals unless the user actually confirms them. Compression and self-review reduce omissions; they do not guarantee perfect transcription.

After saving, the helper returns a `receipt` containing the actual submitted title, status, conclusion, reasons, boundaries, pending actions, and content character count (excluding technical metadata and keywords). The skill shows this receipt, not just “saved”, so you can correct it immediately without reopening the file. Missing reasons are identified rather than invented. Receipts also accompany exact duplicate saves. Corrections can be saved with a `supersedes` link; there is no additional confirmation step before each save.

Changes to a decision create a new record with an explicit `supersedes` link. Reading the old record reports known later corrections. Different wording alone does not establish that two notes contradict each other.

### Search and deletion

`INDEX.md` is a small navigable outline inspired by structural code indexes. Search currently scans the managed Markdown records' **titles, keywords, aliases, and full bodies**; it does not load the whole archive into the AI context and does not depend on a fresh outline. Space-separated query terms use OR matching. Literal wording is boosted, with titles/keywords weighted higher and ties preferring later appended records. Results are paginated. English terms match at word boundaries to avoid matching `release` inside `prerelease`.

Built-in topic groups cover 发布/上线/release/deployment, 延期/推迟/延后/往后推/推后/delay/postpone, 备份/backup, 检索/搜索/查找/search/retrieval, 删除/移除/delete/remove, and 本地/local. Chinese topic words can also be recognized inside an unsegmented question. For example, `发布延期` can find a note saying `上线时间往后推`. Save optional `aliases` such as `["工作记忆系统", "个人笔记"]` for your own project terminology. These are discovery hints, not new facts. `expanded_terms`, `matched_terms`, and `matched_by` explain the match; the AI must still read the note for evidence. This is vocabulary expansion, **not general semantic search** or a CodeGraph integration.

Malformed metadata, broken markers, duplicate IDs, and unreadable day files produce `incomplete: true` and `warnings` with file/record locations; healthy records continue to be returned, including intact neighbors in the same day. All copies of an ambiguous duplicate ID are skipped. Originals are preserved in place: there is no automatic repair or quarantine copy. Rebuilt partial outlines display an incomplete warning. Even zero hits must not be reported as “no information” when the scan is incomplete; healthy matches may be answered with that limitation. A missing ID in an incomplete scan is an error. Writes into a damaged destination day are refused, but another healthy day can still receive records. A healthy record may be deleted without deleting its damaged neighbors. Once repaired manually, records become available on the next scan.

Deletion removes a single record, regenerates the outline, and removes an otherwise empty day file. It requires an exact ID and the content hash returned by `read`, preventing stale deletions after an edit. “Current” always means the latest save in this chat, not another chat's newest note. No trash copy is kept. The session retains only the deleted payload's SHA-256 hash to block accidental exact re-saves; explicit restoration is supported. Paraphrased duplicates cannot be blocked reliably. Deletion does not erase the AI host's chat history, external backups, or references in other notes.

Writers use an exclusive lock and replace individual files atomically. The record is authoritative; an index update failure is returned as `index_warning` rather than hiding a successful save. A crash can leave a stale `.workmemory/write.lock`; remove it only after verifying no writer is active. Multiple files are not a database transaction; the outline can be rebuilt after interruption. Manual edits by other applications do not participate in the lock.

## Local storage and privacy

The helper makes no network requests and never uploads notes. Personal records belong outside the source repository, and the helper rejects storage inside its own checkout. GitHub stores the plugin code only. This does **not** make Codex's model run offline: notes supplied to a cloud-hosted model may be processed remotely. Existing chats are not imported and full transcripts are not captured. There is no telemetry in this plugin.

## CLI and development

The script is `plugins/workmemory/skills/workmemory/scripts/workmemory.py`. It returns UTF-8 JSON and nonzero status for errors. Global options precede the command. The skill resolves the absolute installed script path, so it works from other project directories.

```powershell
$wmScript = 'D:\WorkSpace\WorkMemory\plugins\workmemory\skills\workmemory\scripts\workmemory.py'
python $wmScript --session demo enable
python $wmScript --session demo status
# Save a UTF-8 JSON file (required fields: title and conclusion):
python $wmScript --session demo save --input '<temporary-input.json>'
python $wmScript search --query 'Markdown 保存'
python $wmScript read --id 'M20261030-012345abcdef'
python $wmScript reindex
python $wmScript --session demo disable
```

Use `--root <path>` before the command for an isolated test directory. In Codex, `CODEX_THREAD_ID` can supply the session ID; standalone CLI callers must pass their own stable ID. Optional save fields are `status` (`discussed`, `proposed`, `confirmed`), string arrays `reasons`, `constraints`, `pending`, `keywords`, `aliases`, and record ID array `supersedes`. Unknown fields are rejected to catch misspellings. To delete, first read the target, then pass its ID and returned hash to `delete --id ID --expected-sha256 HASH`.

Run tests from the repository root:

```powershell
python -m unittest discover -s tests -v
```

Tests use temporary directories and no personal notes. Coverage includes session consent, UTF-8, append/duplicate handling, date rollover, corrections, stale indexes, stale deletion hashes, deletion isolation, write failure, concurrent processes, and path containment. Symlink/junction tests are platform-dependent. CI runs the suite on Windows and Linux.

This release does not include automatic multi-tool capture, vector search, a graph database, a custom UI, or offline model inference. Installation/loading in the live Codex UI is a separate smoke test from the deterministic storage tests.

### Verified for v0.2.0

On Windows with Python 3.12: 36 tests passed, with 1 additional symlink test skipped because the OS disallowed creating symlinks. New cases verify non-truncated save receipts, alias retrieval and ranking, per-record aliases, broken neighboring records, invalid metadata/encoding, duplicate IDs, safe writes around damaged files, deletion preserving damaged neighbors, and the CLI's incomplete-result contract. Windows junction containment continues to pass. Tests check deterministic storage and retrieval; they do not prove the host AI never omits a discussion detail.

### Verified for v0.1.0

On Windows with Python 3.12 and Codex CLI 0.130.0: 23 tests passed, with 1 additional symbolic-link test skipped because the OS disallowed creating symlinks. The Windows junction containment test passed. The skill passed Codex's skill frontmatter validator. An isolated temporary Codex home successfully discovered the marketplace, installed the plugin, and reported its packaged skill as enabled, with no marketplace load errors. No personal plugin configuration or memory directory was changed by these checks. Live conversation behavior and the graphical installation flow still require a user smoke test.
