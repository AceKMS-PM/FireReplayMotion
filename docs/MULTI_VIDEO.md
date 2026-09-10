# Multi-video processing — design document

> **Status: DOCUMENTED, NOT BUILT.** Decision taken by the author (Sept 2026):
> nothing to rush — the process is written down first, the code comes later,
> probably at Phase 4 (NAS) time. This document is the contract future-us
> agrees to before writing a single line.

## 1. The goal

Edit many videos without re-typing the same instructions. Two real scenarios:

- **Scenario A (correction):** "clean up lesson12, lesson13 and lesson14" —
  same kind of work applied to several videos, reviewed per video.
- **Scenario B (juggling):** several draft plans open at once — "leave
  lesson12 half-done, work on lesson13, come back later."

## 2. The two possible architectures

### Mode 1 — Sequential in chat (recommended first)

The agent works video-by-video inside one conversation:

```
"clean up lesson12, lesson13, lesson14 with the usual"
  → agent: select lesson12 → plan → SHOW PLAN → wait for your OK → render
  → agent: select lesson13 → same
  → agent: select lesson14 → same
```

- **Code impact: almost zero.** The state stays single-slot
  (`state["video"]`, `state["ops"]`); the conversation itself does the
  sequencing. The agent already knows how to loop — system prompt gets a
  batch protocol ("never render two videos without two confirmations").
- **Review gate stays per-video** — you approve each plan individually.
- Limitation: you cannot hold two half-finished drafts simultaneously.

### Mode 2 — True parallel state (Phase 4 territory)

Several plans open at the same time:

```
"add a title to lesson12 AND zoom on lesson13"
```

- **Code impact: invasive.** Every tool signature gains a video parameter
  (`add_text(video, ...)`), `state` becomes a dict keyed by video name, the
  UI needs tabs or a video switcher, and the *small local brain* must never
  confuse which video an op belongs to (mitigation: every tool result
  echoes the video name; every plan file is already per-video).
- **RAM reality unchanged:** rendering stays one-FFmpeg-at-a-time whatever
  we do — "parallel" only means holding several *draft plans* open, never
  rendering them simultaneously. The stove count is fixed by 8 GB.

### Why Mode 2 is deferred

Mode 1 covers Scenario A (the 90% need). Mode 2 only pays off when juggling
drafts across sessions/days — which realistically starts when videos live on
the NAS and batch jobs arrive. Building it now would refactor every tool for
a need nobody has felt yet.

## 3. Related concepts (parked, on purpose)

### Presets — PARKED (author decision, Sept 2026)

A preset = a named recipe card (`presets/standard.json`) holding
`{min_gap, ops[]}` so "apply standard to lesson14" loads saved choices.

**Why parked:** not every video should get silence cuts — the author is
right that editing choices are per-video decisions. Note for future-us:
presets would only ever apply **on explicit request** (a tool call), never
automatically. Revisit when repetitive editing starts to actually hurt.

### Render queue — independent of multi-video

Renders are always a sequential queue (one FFmpeg at a time). A queue can be
built *before* multi-video and serves both modes. See README roadmap.

## 4. Decisions to make at NAS time (Phase 4 checklist)

1. **Trigger source:** webhook from the streaming app? folder watch
   (`inbox/` share)? manual chat only?
2. **Batch review policy:** does every plan need human approval, or do
   deterministic-only plans (pure silence cuts) auto-approve with a report?
   (Trust-earned-per-decision-type principle, README §9.)
3. **Per-channel defaults:** if presets return, where do they live —
   streaming-app DB column? JSON files per channel?
4. **Queue persistence:** jobs must survive worker restarts (SQLite or
   folder-of-JSON-files, not RAM).
5. **Mode choice:** start Mode 1; upgrade to Mode 2 only when Scenario B
   actually occurs in practice.
6. **Failure policy:** a video that fails rendering must not block the
   queue; failed jobs get a visible status + the reason, sources untouched.

## 5. What does NOT change (the contract holds)

- The EDL remains the central artifact — one plan file per video
  (`plans/<video>.edl.json`), whether built sequentially or in parallel.
- Sources stay read-only; outbox stays the only writable output space.
- The LLM only fills schemas; the validator enforces reality; FFmpeg is
  the only thing that touches pixels.
