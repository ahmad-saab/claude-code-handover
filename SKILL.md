---
name: handover
description: Use when a session is losing history to compaction, when taking over a project from an earlier session, or when the user asks to clean/import jsonl transcripts, build a context or handover pack, or wants a greppable record of past sessions instead of /compact. Also use to answer "when did this file change", "what did we already try", or "what did the user already reject" by grepping prior sessions. Builds and refreshes .handover/ from the project's Claude Code transcripts.
---

# handover

Compaction throws history away. The raw `.jsonl` transcripts keep all of it, but
they are unreadable and enormous. This builds a small greppable pack from them so
you can `/clear` instead of `/compact` and pull back only what you need.

## Build or refresh the pack

```
python ~/.claude/skills/handover/scripts/build_context.py
```

Run it from the project root. It finds that project's transcripts under
`~/.claude/projects/<slugified-cwd>/` on its own and writes `.handover/` into the
project. If `python` is not on PATH, use `py -3`.

Flags, all optional:

- `--project <dir>` project root, defaults to the current directory
- `--in <dir>` transcript directory, when auto-discovery cannot find it
- `--out <dir>` pack location, defaults to `<project>/.handover`
- `--rebuild` discard the pack and import everything from scratch

Reruns are cheap and safe. A manifest at `.handover/.state.json` records a byte
offset per transcript, so a rerun reads only new bytes. The turn in flight when
you run it is held back and rendered whole on the next run.

## What the pack contains

- `TIMELINE.md` — every turn from every session, merged into one chronological
  stream. The entry point. Read this first on a cold start.
- `INDEX.tsv` — one row per prompt, reply, file write, command, error and
  decision. Every row ends in a `sessions/<file>.md:<line>` jump target.
- `FILES.tsv` — each project file, how often it was written, when, and by which
  sessions.
- `sessions/<date>-<time>-<id8>.md` — the readable transcripts, grouped into
  turns. A turn is one instruction plus everything done under it.

Stripped out: tool payloads and results, thinking blocks, reads and greps,
harness reminders, and the agent's mid-turn narration. Kept: user prompts in
full, the closing reply of each turn, and a one-line ledger of every write and
command.

## Using it after /clear

```
grep -i "<topic>" .handover/INDEX.tsv          # every mention, with a jump target
grep -P "\tPROMPT\t" .handover/INDEX.tsv       # every instruction, in order
grep -i "<path>" .handover/FILES.tsv           # who changed this file, and when
```

Swap `PROMPT` for `REPLY`, `EDIT`, `WRITE`, `BASH`, `AGENT`, `SKILL`, `ERROR`,
`DECISION`, `INTERRUPT` or `COMMAND`. Then open the session file at the line the
index gave you to read the whole turn around the hit.

Worth grepping before you start work:

- the file you are about to change, in `FILES.tsv` — see what already touched it
- the feature name, in `INDEX.tsv` — the user may have rejected an approach already
- `ERROR` rows near the same file — the same failure may have been hit before

## Two things to hold onto

**The pack is history, not state.** It records what was asked and what was done
at the time. A prompt from three weeks ago can describe code that has since been
deleted. The code and `MEMORY.md` are authoritative for how things are now; the
pack only tells you how they got that way.

**The pack can contain secrets.** Transcripts hold whatever was ever pasted into
a prompt, including `.env` contents and API keys. Before the first commit after
building it, check that `.handover/` is in `.gitignore` and excluded from any
deploy. If it is not, tell the user rather than adding it silently.
