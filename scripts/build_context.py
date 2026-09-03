"""Turn Claude Code .jsonl transcripts into a greppable handover pack.

Strips harness noise and tool payloads, keeps user prompts, the closing reply of
each turn, and a one-line ledger of every file write and shell command. Resumes
from a byte offset recorded per transcript, so reruns read only new tail bytes.
"""

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
STATE_FILE = ".state.json"
INDEX_FILE = "INDEX.tsv"
TIMELINE_FILE = "TIMELINE.md"
FILES_FILE = "FILES.tsv"
SESSIONS_DIR = "sessions"
DEFAULT_PACK_DIR = ".handover"

MAX_BASH_TEXT = 240
MAX_ERROR_TEXT = 400
MAX_INDEX_TEXT = 300
MAX_HEADLINE = 140

# A transcript untouched for this long is treated as finished, so its final turn
# is written out. Anything more recent may still be mid-turn, and its last turn
# is held back to be rendered whole on a later run.
SETTLED_SECONDS = 600

INDEX_COLUMNS = ("ts", "session", "branch", "turn", "kind", "ref", "text")
FILES_COLUMNS = ("path", "touches", "first", "last", "sessions")

# Harness bookkeeping lines. None of them carry conversation meaning.
NOISE_TYPES = frozenset({
    "attachment", "mode", "permission-mode", "queue-operation", "bridge-session",
    "last-prompt", "ai-title", "custom-title", "agent-name", "atis-latch",
    "file-history-snapshot", "file-history-delta", "system",
})

# Tools whose only product is a lookup. They change nothing worth recording.
SKIPPED_TOOLS = frozenset({"Read", "Grep", "Glob", "ToolSearch", "TodoWrite"})

TOOL_KINDS = {
    "Edit": "EDIT",
    "Write": "WRITE",
    "NotebookEdit": "EDIT",
    "Bash": "BASH",
    "PowerShell": "BASH",
    "Agent": "AGENT",
    "Task": "AGENT",
    "Skill": "SKILL",
    "AskUserQuestion": "ASK",
}

TURN_OPENING_KINDS = frozenset({"PROMPT", "COMMAND"})
ACTION_KINDS = ("EDIT", "WRITE", "BASH", "AGENT", "SKILL", "ERROR", "DECISION", "INTERRUPT")
PATH_KINDS = frozenset({"EDIT", "WRITE"})
KEPT_KINDS = frozenset(TURN_OPENING_KINDS | set(ACTION_KINDS) | {"REPLY"})

SYNTHETIC_MODEL = "<synthetic>"
INTERRUPT_PREFIX = "[Request interrupted by user"


@dataclass
class Event:
    ts: str
    uuid: str
    kind: str
    branch: str
    cwd: str
    parent_uuid: str = ""
    line_start: int = 0
    text: str = ""
    target: str = ""


@dataclass
class Turn:
    number: int
    ts: str
    branch: str
    kind: str
    line_start: int = 0
    uuid: str = ""
    parent_uuid: str = ""
    prompt: str = ""
    actions: list = field(default_factory=list)
    reply: str = ""

    def absorb(self, event):
        if event.kind == "REPLY":
            self.reply = event.text
            return
        self.actions.append(event)


class TextCleaner:
    """Removes the wrappers the harness adds around user and assistant text."""

    # Wrappers the harness injects around user text. The stdout one carries the
    # result of a slash command, which is echo, not instruction.
    WRAPPERS = (
        ("<system-reminder>", "</system-reminder>"),
        ("<local-command-caveat>", "</local-command-caveat>"),
        ("<local-command-stdout>", "</local-command-stdout>"),
    )

    @classmethod
    def strip(cls, raw):
        text = str(raw).replace("\r\n", "\n")
        for opener, closer in cls.WRAPPERS:
            text = cls._drop_between(text, opener, closer)
        return text.strip()

    @staticmethod
    def _drop_between(text, opener, closer):
        while True:
            start = text.find(opener)
            if start == -1:
                return text
            end = text.find(closer, start)
            if end == -1:
                return text[:start].rstrip()
            text = text[:start] + text[end + len(closer):]

    @staticmethod
    def slash_command(raw):
        text = str(raw)
        name = TextCleaner._between(text, "<command-name>", "</command-name>")
        if name is None:
            return None
        args = TextCleaner._between(text, "<command-args>", "</command-args>") or ""
        return ("/" + name.lstrip("/").strip() + " " + args.strip()).strip()

    @staticmethod
    def _between(text, opener, closer):
        start = text.find(opener)
        if start == -1:
            return None
        end = text.find(closer, start)
        if end == -1:
            return None
        return text[start + len(opener):end]

    @staticmethod
    def flatten(content):
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(parts)

    @staticmethod
    def truncate(text, limit):
        flat = str(text).strip()
        return flat[:limit] + " ..." if len(flat) > limit else flat

    @staticmethod
    def one_line(text, limit):
        flat = " ".join(str(text).split())
        return flat[:limit] + " ..." if len(flat) > limit else flat


class TranscriptReader:
    """Streams one transcript from a byte offset, yielding kept events only."""

    def __init__(self, path, start_offset=0):
        self.path = Path(path)
        self.session_id = self.path.stem
        self.start_offset = start_offset
        self.offset = start_offset
        self.last_line_start = start_offset
        self.last_uuid = ""
        self._pending_asks = set()

    def uuid_at(self, line_start):
        """Reads the single line at line_start and returns its uuid, or ''."""
        with self.path.open("rb") as handle:
            handle.seek(line_start)
            raw = handle.readline()
        if not raw:
            return ""
        try:
            return json.loads(raw.decode("utf-8")).get("uuid", "")
        except (ValueError, UnicodeDecodeError):
            return ""

    def events(self):
        with self.path.open("rb") as handle:
            handle.seek(self.start_offset)
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n") and handle.peek(1) == b"":
                    # Trailing partial line: a session still being written. Stop
                    # before it so the next run picks it up whole.
                    break
                self.offset = handle.tell()
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as err:
                    raise RuntimeError(f"{self.path}: bad JSONL at byte {line_start} - {err}") from err
                if entry.get("uuid"):
                    self.last_line_start = line_start
                    self.last_uuid = entry["uuid"]
                if entry.get("type") in NOISE_TYPES or entry.get("isMeta"):
                    continue
                yield from self._events_from(entry, line_start)

    def _events_from(self, entry, line_start):
        base = {
            "ts": entry.get("timestamp", ""),
            "uuid": entry.get("uuid", ""),
            "branch": entry.get("gitBranch", ""),
            "cwd": entry.get("cwd", ""),
            "parent_uuid": entry.get("parentUuid") or "",
            "line_start": line_start,
        }
        kind = entry.get("type")
        if kind == "user":
            yield from self._user_events(entry, base)
        elif kind == "assistant":
            yield from self._assistant_events(entry, base)

    def _user_events(self, entry, base):
        message = entry.get("message")
        if not isinstance(message, dict) or entry.get("isCompactSummary"):
            return
        content = message.get("content")
        if isinstance(content, str):
            event = self._text_event(content, base)
            if event:
                yield event
            return
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                event = self._text_event(block.get("text", ""), base)
                if event:
                    yield event
            elif block.get("type") == "tool_result":
                event = self._tool_result_event(block, base)
                if event:
                    yield event

    def _text_event(self, raw, base):
        flat = str(raw).strip()
        if flat.startswith(INTERRUPT_PREFIX):
            return Event(kind="INTERRUPT", text=flat, **base)
        command = TextCleaner.slash_command(raw)
        if command:
            return Event(kind="COMMAND", text=command, **base)
        text = TextCleaner.strip(raw)
        if not text:
            return None
        return Event(kind="PROMPT", text=text, **base)

    def _tool_result_event(self, block, base):
        body = TextCleaner.flatten(block.get("content"))
        if block.get("is_error"):
            text = TextCleaner.truncate(TextCleaner.strip(body), MAX_ERROR_TEXT)
            return Event(kind="ERROR", text=text, **base)
        if block.get("tool_use_id") in self._pending_asks:
            self._pending_asks.discard(block.get("tool_use_id"))
            return Event(kind="DECISION", text=TextCleaner.strip(body), **base)
        return None

    def _assistant_events(self, entry, base):
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("model") == SYNTHETIC_MODEL:
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = TextCleaner.strip(block.get("text", ""))
                if text:
                    yield Event(kind="REPLY", text=text, **base)
            elif block.get("type") == "tool_use":
                event = self._tool_use_event(block, base)
                if event:
                    yield event

    def _tool_use_event(self, block, base):
        name = block.get("name", "")
        if name in SKIPPED_TOOLS:
            return None
        kind = TOOL_KINDS.get(name)
        if kind is None:
            return None
        payload = block.get("input") or {}
        if kind == "ASK":
            self._pending_asks.add(block.get("id"))
            return None
        if kind in PATH_KINDS:
            target = payload.get("file_path") or payload.get("notebook_path") or ""
            return Event(kind=kind, target=target, **base)
        if kind == "BASH":
            return Event(kind=kind, text=TextCleaner.truncate(payload.get("command", ""), MAX_BASH_TEXT), **base)
        if kind == "AGENT":
            suffix = f" ({payload['subagent_type']})" if payload.get("subagent_type") else ""
            return Event(kind=kind, text=payload.get("description", "") + suffix, **base)
        skill_text = " ".join(str(payload.get(key, "")) for key in ("skill", "args")).strip()
        return Event(kind=kind, text=skill_text, **base)


class SessionDigest:
    """Groups one session's events into turns and tracks its metadata."""

    def __init__(self, session_id, first_turn_number=1):
        self.session_id = session_id
        self.turns = []
        self.branches = []
        self.cwds = []
        self.start = ""
        self.end = ""
        self._next_number = first_turn_number
        self.superseded = 0
        self._current = None

    @property
    def short_id(self):
        return self.session_id[:8]

    @property
    def root_cwd(self):
        return min(self.cwds, key=len) if self.cwds else ""

    def add(self, event):
        if event.kind not in KEPT_KINDS:
            return
        if event.branch and event.branch not in self.branches:
            self.branches.append(event.branch)
        if event.cwd and event.cwd not in self.cwds:
            self.cwds.append(event.cwd)
        if event.ts:
            if not self.start:
                self.start = event.ts
            self.end = event.ts
        if event.kind in TURN_OPENING_KINDS:
            self._open_turn(event)
            return
        if self._current is None:
            self._open_turn(Event(ts=event.ts, uuid=event.uuid, kind="PROMPT", branch=event.branch,
                                  cwd=event.cwd, line_start=event.line_start,
                                  text="(continues a turn that began before this import)"))
        self._current.absorb(event)

    def _open_turn(self, event):
        self._drop_if_superseded(event)
        turn = Turn(number=self._next_number, ts=event.ts, branch=event.branch, kind=event.kind,
                    line_start=event.line_start, uuid=event.uuid,
                    parent_uuid=event.parent_uuid, prompt=event.text)
        self._next_number += 1
        self.turns.append(turn)
        self._current = turn

    def _drop_if_superseded(self, event):
        """A resent prompt is a sibling in the transcript tree: same parentUuid as
        the turn it replaces. The abandoned branch and its work never happened."""
        if not self.turns or not event.parent_uuid:
            return
        if self.turns[-1].parent_uuid != event.parent_uuid:
            return
        self.turns.pop()
        self._next_number -= 1
        self.superseded += 1

    def hold_back_last_turn(self):
        """Drops the trailing turn and returns it, so a still-running session's
        in-flight turn is rendered whole on a later run instead of split."""
        if not self.turns:
            return None
        held = self.turns.pop()
        self._next_number -= 1
        self._current = None
        return held

    @property
    def next_turn_number(self):
        return self._next_number


class PackState:
    """The resume manifest: what was imported, and the byte to continue from."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"format_version": FORMAT_VERSION, "generated": "", "project_root": "", "sessions": {}}
        self.stale = False

    def load(self):
        if not self.path.exists():
            return self
        with self.path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if loaded.get("format_version") != FORMAT_VERSION:
            self.stale = True
            return self
        self.data = loaded
        return self

    def entry(self, session_id):
        return self.data["sessions"].get(session_id)

    def record(self, session_id, values):
        self.data["sessions"][session_id] = values

    def save(self, project_root):
        self.data["format_version"] = FORMAT_VERSION
        self.data["generated"] = datetime.now(timezone.utc).isoformat()
        self.data["project_root"] = project_root
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)


class ContextPack:
    """Writes and refreshes the pack: session files, index, timeline, file map."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.sessions_dir = self.out_dir / SESSIONS_DIR
        self.index_path = self.out_dir / INDEX_FILE
        self.project_root = ""

    def prepare(self, rebuild_all):
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if rebuild_all:
            for stale in self.sessions_dir.glob("*.md"):
                stale.unlink()
            if self.index_path.exists():
                self.index_path.unlink()
        if not self.index_path.exists():
            self._write_lines(self.index_path, ["# " + "\t".join(INDEX_COLUMNS)])

    def purge_session(self, file_name):
        """Removes a session's rendered file and its index rows. Used when a
        transcript was rewritten rather than appended to, so the old import
        cannot be continued and must not be left behind as a duplicate."""
        if not file_name:
            return
        target = self.sessions_dir / file_name
        if target.exists():
            target.unlink()
        if not self.index_path.exists():
            return
        prefix = f"{SESSIONS_DIR}/{file_name}:"
        with self.index_path.open("r", encoding="utf-8") as handle:
            kept = [line.rstrip("\n") for line in handle
                    if line.startswith("#") or prefix not in line]
        self._write_lines(self.index_path, kept)

    @staticmethod
    def session_file_name(digest):
        stamp = digest.start.replace("-", "").replace(":", "")
        day = digest.start[:10] if digest.start else "undated"
        clock = stamp[9:13] if len(stamp) > 12 else "0000"
        return f"{day}-{clock}-{digest.short_id}.md"

    def relativise(self, target):
        normalised = str(target).replace("\\", "/")
        if not self.project_root:
            return normalised
        root = self.project_root.replace("\\", "/")
        if normalised.lower().startswith(root.lower() + "/"):
            return normalised[len(root) + 1:]
        return normalised

    def append_session(self, digest, file_name, is_new):
        target = self.sessions_dir / file_name
        lines = []
        if is_new:
            lines.extend(self._session_header(digest))
        start_line = self._line_count(target) + 1
        rows = []
        for turn in digest.turns:
            rows.extend(self._render_turn(digest, turn, lines, start_line))
        self._append_lines(target, lines)
        return rows

    def _session_header(self, digest):
        return [
            f"# {digest.short_id} - session transcript, stripped",
            "",
            f"- session: {digest.session_id}",
            f"- project: {digest.root_cwd or 'unknown'}",
            f"- source: {digest.session_id}.jsonl",
            "- times are UTC, exactly as recorded in the transcript",
            "",
            "---",
            "",
        ]

    def _render_turn(self, digest, turn, lines, start_line):
        rows = []
        header = f"## T{turn.number} {turn.ts} {turn.branch or 'no-branch'}"
        lines.append(header)
        lines.append("")
        lines.append("PROMPT")
        prompt_line = start_line + len(lines)
        for text_line in (turn.prompt or "(empty)").split("\n"):
            lines.append(text_line)
        lines.append("")
        rows.append(self._row(digest, turn, turn.kind, prompt_line, turn.prompt))

        if turn.actions:
            lines.append("ACTIONS")
            for action in turn.actions:
                action_line = start_line + len(lines)
                lines.append("- " + self._action_text(action))
                rows.append(self._row(digest, turn, action.kind, action_line,
                                      self._action_payload(action)))
            lines.append("")

        if turn.reply:
            lines.append("REPLY")
            reply_line = start_line + len(lines)
            for text_line in turn.reply.split("\n"):
                lines.append(text_line)
            lines.append("")
            rows.append(self._row(digest, turn, "REPLY", reply_line, turn.reply))
        return rows

    def _action_text(self, action):
        if action.target:
            return f"{action.kind} {self.relativise(action.target)}"
        return f"{action.kind} {TextCleaner.one_line(action.text, MAX_INDEX_TEXT)}"

    def _action_payload(self, action):
        return self.relativise(action.target) if action.target else action.text

    def _row(self, digest, turn, kind, line_no, text):
        ref = f"@@REF@@:{line_no}"
        return {
            "ts": turn.ts,
            "session": digest.short_id,
            "branch": turn.branch,
            "turn": f"T{turn.number}",
            "kind": kind,
            "ref": ref,
            "text": self._cell(text),
        }

    @staticmethod
    def _cell(text):
        flat = str(text).replace("\t", " ").replace("\r\n", "\n").replace("\n", "\\n").strip()
        return flat[:MAX_INDEX_TEXT] + " ..." if len(flat) > MAX_INDEX_TEXT else flat

    def append_index(self, rows, file_name):
        lines = []
        for row in rows:
            row["ref"] = row["ref"].replace("@@REF@@", f"{SESSIONS_DIR}/{file_name}")
            lines.append("\t".join(row[column] for column in INDEX_COLUMNS))
        self._append_lines(self.index_path, lines)

    def rebuild_views(self):
        rows = self._read_index()
        self._write_timeline(rows)
        self._write_files(rows)
        return len(rows)

    def _read_index(self):
        if not self.index_path.exists():
            return []
        rows = []
        with self.index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(INDEX_COLUMNS):
                    continue
                rows.append(dict(zip(INDEX_COLUMNS, parts)))
        return rows

    def _write_timeline(self, rows):
        openings = sorted((r for r in rows if r["kind"] in TURN_OPENING_KINDS), key=lambda r: r["ts"])
        edits = [r for r in rows if r["kind"] in PATH_KINDS]
        sessions = sorted({r["session"] for r in rows})
        lines = [
            "# Project timeline",
            "",
            f"- project: {self.project_root or 'unknown'}",
            f"- sessions: {len(sessions)}",
            f"- turns: {len(openings)}",
            f"- file writes: {len(edits)}",
            f"- window: {openings[0]['ts'] if openings else '-'} -> {openings[-1]['ts'] if openings else '-'}",
            f"- rebuilt: {datetime.now(timezone.utc).isoformat()}",
            "- all times are UTC, as recorded in the transcripts",
            "",
            "## Read this before using the pack",
            "",
            "This is history, not current state. It records what was asked and what was done at",
            "the time. The code and MEMORY.md are authoritative for how things are now.",
            "",
            "It can also contain anything ever pasted into a prompt, including credentials.",
            "Keep it out of version control and out of any deploy.",
            "",
            "## How to use it after /clear",
            "",
            "- `grep -i \"<topic>\" .handover/INDEX.tsv` - every mention, each row ending in a",
            "  `sessions/<file>.md:<line>` jump target.",
            "- `grep -P \"\\tPROMPT\\t\" .handover/INDEX.tsv` - every instruction in order. Swap PROMPT",
            "  for REPLY, EDIT, WRITE, BASH, AGENT, SKILL, ERROR, DECISION, INTERRUPT, COMMAND.",
            "- `grep -i \"<path>\" .handover/FILES.tsv` - which sessions changed a file, and when.",
            "- Open the session file at the line the index gave you to read the whole turn.",
            "",
            "## Turns in order",
            "",
            "| when (UTC) | session | turn | branch | instruction | ref |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in openings:
            instruction = TextCleaner.one_line(row["text"].replace("\\n", " "), MAX_HEADLINE).replace("|", "\\|")
            lines.append(
                f"| {row['ts']} | {row['session']} | {row['turn']} | {row['branch'] or '-'} "
                f"| {instruction or '-'} | {row['ref']} |"
            )
        lines.append("")
        self._write_lines(self.out_dir / TIMELINE_FILE, lines)

    @staticmethod
    def _inside_project(path):
        """Writes to temp dirs, job scratch and other repos stay out of the file
        map. relativise() leaves those absolute, which is the tell."""
        if path.startswith("/"):
            return False
        return not (len(path) > 2 and path[1] == ":" and path[2] == "/")

    def _write_files(self, rows):
        tracked = {}
        outside = 0
        for row in rows:
            if row["kind"] not in PATH_KINDS:
                continue
            path = row["text"]
            if not self._inside_project(path):
                outside += 1
                continue
            entry = tracked.setdefault(path, {"touches": 0, "first": row["ts"], "last": row["ts"], "sessions": []})
            entry["touches"] += 1
            if row["ts"] and row["ts"] < entry["first"]:
                entry["first"] = row["ts"]
            if row["ts"] > entry["last"]:
                entry["last"] = row["ts"]
            if row["session"] not in entry["sessions"]:
                entry["sessions"].append(row["session"])
        lines = [
            "# " + "\t".join(FILES_COLUMNS),
            f"# {outside} writes landed outside the project and are in {INDEX_FILE} only",
        ]
        for path, entry in sorted(tracked.items(), key=lambda item: item[1]["touches"], reverse=True):
            lines.append("\t".join([
                path, str(entry["touches"]), entry["first"], entry["last"], ",".join(entry["sessions"]),
            ]))
        self._write_lines(self.out_dir / FILES_FILE, lines)

    @staticmethod
    def _line_count(path):
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)

    @staticmethod
    def _append_lines(path, lines):
        if not lines:
            return
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

    @staticmethod
    def _write_lines(path, lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")


class ProjectLocator:
    """Finds the transcript directory Claude Code keeps for a project."""

    PROJECTS_DIR = Path.home() / ".claude" / "projects"

    @staticmethod
    def slug(project_dir):
        """Claude Code names a project's transcript directory after its path with
        every non-alphanumeric character replaced by a dash. Note this includes
        dots and tildes, so 'C:\\src\\my.app' becomes 'C--src-my-app'."""
        return re.sub(r"[^A-Za-z0-9]", "-", str(Path(project_dir).resolve()))

    @classmethod
    def transcripts_for(cls, project_dir):
        candidate = cls.PROJECTS_DIR / cls.slug(project_dir)
        if candidate.is_dir():
            return candidate
        local = Path(project_dir)
        if any(local.glob("*.jsonl")):
            return local
        raise RuntimeError(
            f"no transcripts found for {project_dir}\n"
            f"looked in {candidate} and in the directory itself; pass --in to point at them"
        )


class Cli:
    """Argument handling and the import run itself."""

    USAGE = "usage: build_context.py [--project <dir>] [--in <transcripts>] [--out <pack>] [--rebuild]"

    def __init__(self, argv):
        self.options = self._parse(argv)

    @classmethod
    def _parse(cls, argv):
        options = {"project": Path.cwd(), "in": None, "out": None, "rebuild": False}
        index = 0
        while index < len(argv):
            flag = argv[index]
            if flag == "--rebuild":
                options["rebuild"] = True
                index += 1
                continue
            if flag in ("--project", "--in", "--out"):
                if index + 1 >= len(argv):
                    raise RuntimeError(f"missing value for {flag}\n{cls.USAGE}")
                options[flag[2:]] = Path(argv[index + 1]).resolve()
                index += 2
                continue
            raise RuntimeError(f"unknown argument: {flag}\n{cls.USAGE}")
        if options["in"] is None:
            options["in"] = ProjectLocator.transcripts_for(options["project"])
        if options["out"] is None:
            options["out"] = Path(options["project"]) / DEFAULT_PACK_DIR
        return options

    def run(self):
        transcripts = sorted(Path(self.options["in"]).glob("*.jsonl"))
        if not transcripts:
            raise RuntimeError(f"no .jsonl files in {self.options['in']}")
        state = PackState(Path(self.options["out"]) / STATE_FILE).load()
        rebuild = self.options["rebuild"] or state.stale
        if state.stale:
            self._say("output format changed since last run - rebuilding the whole pack")
        if rebuild:
            state.data["sessions"] = {}
        pack = ContextPack(self.options["out"])
        pack.prepare(rebuild)
        pack.project_root = str(self.options["project"])

        tally = {"imported": 0, "skipped": 0, "held": 0}
        turns, in_flight = 0, 0
        for transcript in transcripts:
            status, count, held = self._import(transcript, state, pack)
            tally[status] += 1
            turns += count
            in_flight += held
        rows = pack.rebuild_views()
        state.save(pack.project_root)
        self._say(
            f"{tally['imported']} imported, {tally['skipped']} unchanged, "
            f"{turns} new turns, {in_flight} turns still in flight and held back, "
            f"{rows} index rows -> {self.options['out']}"
        )

    def _import(self, transcript, state, pack):
        stat = transcript.stat()
        session_id = transcript.stem
        previous = state.entry(session_id)
        offset, first_turn = 0, 1
        file_name, is_new = None, True
        if previous and self._resumable(transcript, previous, stat):
            if previous["offset"] >= stat.st_size:
                return ("skipped", 0, 0)
            offset = previous["offset"]
            first_turn = previous["turns"] + 1
            file_name = previous["session_file"]
            is_new = not previous["session_file"]
        elif previous:
            pack.purge_session(previous["session_file"])

        reader = TranscriptReader(transcript, offset)
        digest = SessionDigest(session_id, first_turn)
        for event in reader.events():
            digest.add(event)
        held = None if self._settled(stat) else digest.hold_back_last_turn()
        resume = self._resume_point(reader, held, previous)

        in_flight = 1 if held is not None else 0
        if not digest.turns:
            state.record(session_id, self._entry(stat, resume, previous, file_name, first_turn - 1))
            return ("held" if held is not None else "skipped", 0, in_flight)
        if not file_name:
            file_name = ContextPack.session_file_name(digest)
        rows = pack.append_session(digest, file_name, is_new)
        pack.append_index(rows, file_name)
        state.record(session_id, self._entry(stat, resume, previous, file_name, digest.next_turn_number - 1))
        return ("imported", len(digest.turns), in_flight)

    @staticmethod
    def _settled(stat):
        return (time.time() - stat.st_mtime) > SETTLED_SECONDS

    @staticmethod
    def _resume_point(reader, held, previous):
        if held is not None:
            return {"offset": held.line_start, "last_line_start": held.line_start, "last_uuid": held.uuid}
        return {
            "offset": reader.offset,
            "last_line_start": reader.last_line_start if reader.last_uuid else (
                previous["last_line_start"] if previous else 0),
            "last_uuid": reader.last_uuid or (previous["last_uuid"] if previous else ""),
        }

    @staticmethod
    def _resumable(transcript, previous, stat):
        if stat.st_size < previous["size"]:
            return False
        if not previous.get("last_uuid"):
            return True
        probe = TranscriptReader(transcript).uuid_at(previous["last_line_start"])
        return probe == previous["last_uuid"]

    @staticmethod
    def _entry(stat, resume, previous, file_name, turns):
        return {
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "offset": resume["offset"],
            "last_line_start": resume["last_line_start"],
            "last_uuid": resume["last_uuid"],
            "session_file": file_name or (previous["session_file"] if previous else ""),
            "turns": turns,
        }

    @staticmethod
    def _say(message):
        sys.stdout.write(message + "\n")


def _main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        Cli(sys.argv[1:]).run()
    except RuntimeError as err:
        sys.stderr.write(str(err) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
