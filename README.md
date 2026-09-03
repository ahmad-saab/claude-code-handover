# claude-code-handover

Turn Claude Code's `.jsonl` session transcripts into a small, greppable handover
pack, so you can `/clear` instead of `/compact` and pull back only the history
you actually need.

## The problem

Compaction is lossy and one-way. When a long session compacts, the agent's
knowledge of what you asked for, what it tried, and what you rejected is
replaced by a summary. Ask about a decision from three days ago and it no longer
knows.

The full history is still on disk. Claude Code writes every session to
`~/.claude/projects/<project>/<session-id>.jsonl` and never rewrites those files.
The problem is that they are unreadable and enormous — a single long session can
run to tens of megabytes, most of it tool payloads and harness bookkeeping.

This tool strips that down to what a takeover agent can use, and indexes it so
the agent can find things by grep instead of by remembering them.

On one real archive: **33 sessions, 190 MB in, 3.0 MB out, in about a second.**

## What it looks like

Illustrative example, real formats. Index rows are one tab-separated line each,
wrapped below so they fit the page.

Ask a question you would otherwise have to remember the answer to:

```console
$ grep -i "rate limit" .handover/INDEX.tsv
2026-08-14T09:12:41.507Z  a1b4c7d2  main  T31  PROMPT  sessions/2026-08-14-0847-a1b4c7d2.md:612
  the login endpoint needs a rate limit, 5 attempts then a 15 minute lockout
2026-08-14T09:31:08.220Z  a1b4c7d2  main  T33  PROMPT  sessions/2026-08-14-0847-a1b4c7d2.md:698
  no, drop the lockout, just throttle. a lockout is a denial of service on the real user
2026-08-19T16:02:55.881Z  e9f2a038  auth   T7  ERROR   sessions/2026-08-19-1554-e9f2a038.md:141
  RuntimeError: throttle key must be scoped per account, not per IP
```

Three lines that a compaction would have thrown away: what was asked, that it was
**reversed** twenty minutes later and why, and the error someone already hit
building it. Every row carries the file and line to read the whole turn.

Open the second hit and you get the instruction with everything done under it:

```markdown
## T33 2026-08-14T09:31:08.220Z main

PROMPT
no, drop the lockout, just throttle. a lockout is a denial of service on the real user

ACTIONS
- EDIT app/Http/Middleware/ThrottleLogins.php
- EDIT config/auth.php
- BASH php artisan test --filter=Throttle
- ERROR 1 failed: expected 429, got 423

REPLY
Lockout removed, replaced with a 5/minute throttle keyed on account plus IP.
config/auth.php no longer carries lockout_minutes. One test still asserted the
old 423 and now asserts 429.
```

The chronological view across every session, `TIMELINE.md`:

```markdown
| when (UTC) | session | turn | branch | instruction | ref |
| --- | --- | --- | --- | --- | --- |
| 2026-08-14T09:12:41.507Z | a1b4c7d2 | T31 | main | the login endpoint needs a rate limit, 5 attempts th ... | sessions/2026-08-14-0847-a1b4c7d2.md:612 |
| 2026-08-14T09:31:08.220Z | a1b4c7d2 | T33 | main | no, drop the lockout, just throttle. a lockout is a  ... | sessions/2026-08-14-0847-a1b4c7d2.md:698 |
| 2026-08-19T15:54:02.113Z | e9f2a038 | T1  | auth | port the throttle to the new auth guard              | sessions/2026-08-19-1554-e9f2a038.md:13  |
```

And which sessions touched a file, `FILES.tsv`:

```console
$ grep -i "ThrottleLogins" .handover/FILES.tsv
app/Http/Middleware/ThrottleLogins.php  6  2026-08-14T09:31:08.220Z  2026-08-19T16:14:20.905Z  a1b4c7d2,e9f2a038
```

## Install

Clone into your Claude Code skills directory:

```bash
git clone https://github.com/ahmad-saab/claude-code-handover ~/.claude/skills/handover
```

Requires Python 3.8+. No dependencies — standard library only.

Claude Code picks the skill up automatically. Invoke it with `/handover`, or just
describe what you want ("build a context pack", "what did we already try here")
and the skill's description will match.

You can also run it directly, without the skill:

```bash
python ~/.claude/skills/handover/scripts/build_context.py
```

## Usage

Run it from a project root. It locates that project's transcripts on its own and
writes the pack to `.handover/` inside the project.

```bash
cd /path/to/your/project
python ~/.claude/skills/handover/scripts/build_context.py
```

```
30 imported, 3 unchanged, 380 new turns, 1 turns still in flight and held back,
2363 index rows -> /path/to/your/project/.handover
```

| flag | meaning |
| --- | --- |
| `--project <dir>` | project root; defaults to the current directory |
| `--in <dir>` | transcript directory, if auto-discovery cannot find it |
| `--out <dir>` | pack location; defaults to `<project>/.handover` |
| `--rebuild` | discard the pack and reimport everything from scratch |

Reruns are cheap. See [Incremental imports](#incremental-imports).

## What you get

```
.handover/
├── TIMELINE.md      every turn from every session, in chronological order
├── INDEX.tsv        one row per prompt, reply, write, command, error, decision
├── FILES.tsv        each project file: how often written, when, by which sessions
├── .state.json      resume manifest
└── sessions/
    └── 2026-08-29-0554-70a0dd2a.md
```

### TIMELINE.md

The entry point. Read this first on a cold start. Every turn from every session
merged into one stream ordered by time, each row pointing at the session file and
line where the full turn lives.

This matters more than it sounds. Sessions overlap — a four-day session can
completely bracket twenty short ones — so a list of session files tells you
nothing about what happened when. Only a turn-level merge does.

### INDEX.tsv

The grep target. One row per event, tab-separated:

```
ts    session    branch    turn    kind    ref    text
```

Every row is self-contained and ends in a `sessions/<file>.md:<line>` jump
target, so a single grep gives you the hit *and* where to read around it.

```bash
grep -i "navbar" .handover/INDEX.tsv        # every mention of a topic
grep -P "\tPROMPT\t" .handover/INDEX.tsv    # every instruction you ever gave
grep -P "\tERROR\t" .handover/INDEX.tsv     # everything that failed
```

Kinds: `PROMPT`, `REPLY`, `COMMAND`, `EDIT`, `WRITE`, `BASH`, `AGENT`, `SKILL`,
`ERROR`, `DECISION`, `INTERRUPT`.

### FILES.tsv

Each project file with a write count, first and last touch, and the sessions
responsible.

```bash
grep -i "src/auth" .handover/FILES.tsv
```

This is the artifact that answers "why does this file look like this" — the one
question compaction reliably destroys. Writes that landed outside the project
root are excluded here and remain in `INDEX.tsv`; the count is in the header.

### sessions/*.md

The readable transcripts, grouped into turns. A **turn** is one instruction plus
everything done under it:

```markdown
## T12 2026-08-29T06:02:00.998Z main

PROMPT
merge the hero image with the navbar, and note the current settings in MEMORY.md
first in case we need to revert

ACTIONS
- EDIT assets/site.css
- EDIT index.html
- BASH npm run build
- ERROR File does not exist.

REPLY
Done. Nav sits on the image with a 70% red band. Previous values recorded in
MEMORY.md under "hero".
```

Files are named `YYYY-MM-DD-HHMM-<id8>.md`, so a lexical sort is a chronological
sort. Sequence-number prefixes were deliberately avoided: importing an older
transcript later would renumber every file and invalidate refs already written.

## What is kept and what is dropped

**Kept**

- user prompts, in full
- the closing reply of each turn, in full
- one line per file write, shell command, subagent spawn and skill invocation
- errors, truncated
- answers to `AskUserQuestion` — these are decisions you made
- interrupts — the points where you stopped the agent

**Dropped**

- all tool inputs, diffs and results, which are the bulk of the bytes
- thinking blocks
- `Read`, `Grep`, `Glob`, `ToolSearch` — lookups that change nothing
- harness bookkeeping: attachments, token reminders, mode and permission changes,
  title and queue events
- the agent's mid-turn narration; only the closing report of a turn survives
- compaction summaries, which are derived from messages the pack already keeps

Two judgement calls worth naming:

**Tool payloads go, the ledger stays.** A one-line `EDIT src/auth.py` is about
fifty bytes and is the only ground truth of what changed. Prompts say what was
asked and replies say what the agent *claims* it did — those claims are wrong
often enough that they cannot be the record.

**Only the closing reply survives.** Mid-turn narration ("Now the CSS.") is
filler between tool calls. The last text in a turn is the report of what happened
and what broke, and that is the part a takeover agent needs. The split is
structural, so no content heuristics are involved.

## Incremental imports

Transcripts are append-only, which makes resuming safe and cheap. `.state.json`
records per transcript:

```json
{
  "size": 29233237,
  "mtime": 1788430843.46,
  "offset": 29233237,
  "last_line_start": 306654,
  "last_uuid": "a0642d70-e7ab-4fe2-af57-27d97726f45a",
  "session_file": "2026-08-29-0554-70a0dd2a.md",
  "turns": 368
}
```

- **`offset`** is the seek point. A rerun reads only new tail bytes.
- **`last_uuid`** is the validator. Before trusting the offset, the tool re-reads
  the line at `last_line_start` and checks the uuid still matches. A uuid alone
  cannot be a resume point — finding one means scanning from byte zero, which is
  the full read being avoided.
- A **shrunk or rewritten** transcript fails that check, so its session file and
  index rows are purged and it is reimported cleanly.
- **`format_version`** in the manifest forces a full rebuild when the output
  layout changes, which is necessary because session files are append-only.

Session files and `INDEX.tsv` are append-only, so refs stay valid across runs.
`TIMELINE.md` and `FILES.tsv` are regenerated each run from `INDEX.tsv`, because
a new turn can be *older* than existing rows when sessions overlap.

### The in-flight turn

If a transcript has been touched in the last 10 minutes it may be mid-turn, so
its final turn is held back and rendered whole on a later run. Without this, an
agent running the tool on its own session would split every turn in half — its
own turn is by definition unfinished.

## Transcript details this relies on

Worth knowing if you plan to modify it:

- Transcripts are **append-only**. Compaction trims live context; it never
  rewrites the file. Messages from before a compaction boundary stay on disk.
- The transcript is a **tree, not a list**. A resent or edited prompt appears as a
  sibling sharing the same `parentUuid`, and the earlier branch was abandoned.
  Rendering both makes discarded instructions look real, so superseded siblings
  are dropped.
- The project directory name under `~/.claude/projects/` is the project path with
  **every non-alphanumeric character** replaced by a dash — dots and tildes
  included. `C:\src\my.app` becomes `C--src-my-app`.
- `file-history-snapshot` and `file-history-delta` are a backup ledger, not a
  record of what the tool has imported.
- A transcript's last line may be a partial write, so an unterminated final line
  is left for the next run.

## Limitations

- **The pack is history, not state.** It records what was asked and done at the
  time. A prompt from three weeks ago can describe code since deleted. Your code
  and project notes remain authoritative for how things are now.
- **It can contain secrets.** Transcripts hold whatever was ever pasted into a
  prompt, including `.env` contents and API keys. There is no redaction. Keep
  `.handover/` out of version control and out of deploys — the shipped
  `.gitignore` covers the pack itself.
- **Grep finds only what you know to ask for.** It answers a directed question
  well and "catch me up" poorly; `TIMELINE.md` carries that load and gets weaker
  as sessions accumulate.
- **Value scales with session length.** For a several-hundred-turn session it is
  a large win. For a handful of fifty-line sessions it is close to nothing.
- **Subagent transcripts are not imported.** The parent's `AGENT` line records
  that a subagent ran and what it was asked to do, but its internal transcript in
  `subagents/` is skipped.

## Design

Nine classes, one file, standard library only.

| class | responsibility |
| --- | --- |
| `Event`, `Turn` | the two data shapes |
| `TextCleaner` | strips harness wrappers from user and assistant text |
| `TranscriptReader` | streams one transcript from a byte offset, emitting kept events |
| `SessionDigest` | groups events into turns, drops superseded branches |
| `PackState` | the resume manifest |
| `ContextPack` | writes session files, index, timeline and file map |
| `ProjectLocator` | finds a project's transcript directory |
| `Cli` | arguments and the import run |

## License

MIT
