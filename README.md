#  **FireReplayMotion**



---

## 1. The vision — problem statement

> **« Un agent IA conversationnel de post-production, qui transforme une instruction en langage naturel en un plan de montage exécutable, en s'appuyant uniquement sur la transcription temporalisée des vidéos comme base de connaissance. »**
>
> *(formulated by the author — this is the project's constitution)*

In plain English: an agent you talk to. It **reasons over text** (the transcripts) — the video itself is only touched at the very last moment (render). The core of the system is the passage **user intention → structured edit plan**, not the editing as such.

**This is a true AI agent, in the Claude Code / Hermes Agent sense:** an LLM as the head, a tool loop as the hands. The model reads context (session state + transcript), decides which tool to call, observes the result, iterates until it can propose a verifiable plan. The analogy:

| Claude Code (code agent) | Edit AI (editing agent) |
|---|---|
| reads the repo | reads the transcript + video metadata |
| writes code | writes the edit plan (EDL) |
| runs tests / shows a diff | shows the plan for validation |
| executes the build | FFmpeg render (Quick Sync) |

**The system answers three questions, in this order:**

1. **Comprendre** — what does the user want, on which video? *(chat session + session state)*
2. **Planifier** — which operations, in which order, on which time segments? *(the EDL contract, via the transcript)*
3. **Exécuter** — apply the validated plan, produce a clean output file, sources untouched *(executor + report)*

### Principles

- **The plan is the central artifact.** Chat is ephemeral; the EDL is durable. Review, versioning, trust levels, re-rendering, debugging — everything hangs off the plan file.
- **Knowledge base = transcript + technical metadata.** ffprobe gives duration/resolution for free (milliseconds, no RAM). Zero frame analysis in V1 — the video is never "watched".
- **Safety invariants.** Sources are read-only. The outbox is the only writable space. Execution is atomic (render to temp → move). A vague instruction ("make it nicer") → the agent **asks**, it never guesses.
- **Scope.** One video per chat session in V1. No autopilot over the catalog. Multi-video sessions, then supervised autonomy, come later.
- **VLM (watching frames) is an optional future tool.** The architecture reserves the slot: "look at frames" is just another tool the agent may or may not have. On 8 GB it would be the first thing to hit the memory wall — the "time for better hardware" trigger.

## 2. The constraints of development (why the design looks like this)

| Constraint | Consequence |
|---|---|
| 8 GB RAM, old CPU/iGPU | No heavy AI model + rendering at the same time. Everything runs **sequentially**. |
| €0 budget | Only free, open-source tools: Python, FFmpeg, llama.cpp.  |
| Streaming app must stay fast | Editing runs in the background as a worker; the app only ever reads output files. |
| Prototype locally with test files; design so folders can later become NAS shares + webhooks. |

## 3. The key insight

**The transcripts already exist in the app's database,.**

So there is no need for heavy AI to *listen* to the videos. If one sentence ends at 2:31 and the next starts at 2:38, there is a 7-second silence → cut it. The transcript is the "directing script" that tells the system where to cut. The only "AI" needed for the core pipeline is arithmetic.

## 4. How accurate without "watching"? — the eyes ladder

The agent is blind on one axis (space: *where* something is on screen) — and the design compensates. "Watching" is not binary: there are four levels of eyes, and three of them are nearly free.

| Level | Sense | Cost | Covers |
|---|---|---|---|
| 1 | **Transcript** *(have it)* | free | the *speech* dimension: timing, structure, content |
| 2 | **Audio analysis** — `silencedetect`, loudness stats | ~free, no model | noise, music, clicks — what the transcript misses |
| 3 | **Signal-level video analysis** — scene change, black frame, freeze detection, brightness stats | ~free, no AI, no RAM | *when* visuals change — not *what* they show |


**Why the *system* stays accurate even with a blind model:**

```
deterministic rules 
+ measurements (audio/scene/brightness — objective numbers)
+ cross-validation (transcript gap says "silence", silencedetect confirms it)
+ human review of the plan (your eyes ARE the VLM in V1)
= the LLM only orchestrates; it never has to "see" to be safe
```

Blind-spot example: the agent proposes "zoom at 5:12", the plan preview shows the crop, the human adjusts once — and the input is cached and reused. The human is the expensive sensor; the system uses you sparingly.

## 5. The architecture: brain / hands / contract

```
[instruction (chat)] + [transcript DB] + [preferences]
              │
       ┌──────▼───────┐
       │    BRAIN     │  decides WHAT to edit
       │  (pluggable) │  v1: deterministic rules (built)
       └──────┬───────┘  v2: LLM agent via llama.cpp (next)
              │  writes: edit plan (JSON)  ◄── THE CONTRACT
       ┌──────▼───────┐
       │    HANDS     │  executes the plan with FFmpeg
       │  (executor)  │  dumb forever — never changes its nature
       └──────┬───────┘
         outbox → streaming app reads
```

**Why the contract matters:** the brain is swappable (rules → small local LLM → future improvements) without ever touching the executor. The LLM never sees video — it reads text and writes JSON. That is the cheapest possible AI workload, which is why a small 3B model is enough.

**Memory math:** LLM ~2 GB + one FFmpeg ~0.4 GB + system ≈ 4–5 GB. Fits in 8 GB because the pipeline is sequential and light.

## 6. What is already built (and verified)

- **FFmpeg is installed and battle-tested** (v9, full build)
- **Quick Sync works**: hardware encoding on the HD 520 is verified (`h264_qsv`, iHD driver) → renders several times faster, CPU stays free ( specific to my hardware )
- **Phase 0 — lab demos** (`lab/`): cut, zoom, drawtext, fade, color — all proven on `test_clip.mp4` (24 s clip with burned-in timestamp and planted silences at 0:06–0:09, 0:13–0:16, 0:20–0:23)
- **Phase 1 — the pipeline** (tested end-to-end, output exactly as planned):

```
inbox/                      plans/                outbox/
 ├─ test_clip.mp4    →      test_clip.edl.json   →  test_clip_clean.mp4
 ├─ test_clip.transcript.json   (the edit plan)
 └─ job.json (min_gap: 2.0)
```

| Script | Role | Size |
|---|---|---|
| `plan.py` | the brain (rules): transcript + choices → edit plan | ~70 lines |
| `review.py` | the safety gate: shows plan, human approves | ~40 lines |
| `render.py` | the hands: segments → Quick Sync render → concat | ~80 lines |

Verified result: 24 s → 15 s, three silences removed, rendered on hardware.

**Try it:**
```
python3 plan.py       # transcript → plan
python3 review.py     # read the plan → type y
python3 render.py     # → outbox/
```
Experiments: change `min_gap` in `inbox/job.json`, move timestamps in the transcript, reject in review — rerun and watch the plan change.

## 7. The agent — built (Phase 2 complete)

**The conversational agent exists and works end-to-end.**

```
python3 agent.py                # talk to it (one video per session)
python3 agent.py --demo         # scripted session, to watch it work
python3 agent.py --stop-brain   # free the RAM without opening a chat
```

**How to chat with it** — start it, wait for `brain online` (~40 s cold, instant if
warm), then type sentences:

```
You: hi, which videos can I edit?
You: select test_clip
You: are there silences?                       ← it calls measure + transcript
You: cut the silences over 2s, add a title "Chapter 1" at the start for 3 seconds
🤖: Plan ready — 3 cuts + 1 title. Say "render" to proceed.
You: render                                    ← the chat IS the review gate
🤖: Done → outbox/test_clip_clean.mp4 (15.0 s)
You: quit
```

**Leaving the session — every door, clearly marked:**

| Command | Effect |
|---|---|
| `quit` (terminal chat) | leaves the chat; **brain stays warm** in RAM (~2.5 GB) → next session starts instantly |
| `/stop` (terminal chat) | leaves the chat **and stops the brain** — RAM freed |
| `python3 agent.py --stop-brain` | stops the brain from anywhere, no chat needed |
| `Ctrl+C` on `webui.py` | stops the **web server only**; brain stays warm + clear warning with the exact free-RAM command |
| **⏹ Stop brain** button (web UI) | stops the brain from the browser, before closing the page |

Rule of thumb: editing again soon → `quit` / just close the page. Done for the day → `/stop` or the Stop button.

**Layer status:**

| Layer | Component | State |
|---|---|---|
| Senses | `measure.py` — silencedetect + scene changes + brightness (levels 2–3) | ✅ verified: audio sense agrees with transcript |
| Hands v2 | `render.py` — text/zoom/polish ops, zoom splits segments, op validation | ✅ verified end-to-end |
| Brain | llama.cpp (Spark fork, built from source: `tools/llama.cpp-master/`) + **Spark-X2.5-4B Q4_K_M** (default) | ✅ A/B-tested: more agentic than Qwen3-4B — proactive tool chaining, cross-validated senses vs transcript |
| Alt brains | `Qwen3-4B` (faster, lazier), `granite-4.0-micro` (fallback) | downloaded; swap: `EDITAI_MODEL=models/Qwen3-4B-Q4_K_M.gguf EDITAI_PORT=8322 python3 agent.py` |
| Loop | `agent.py` — 13 tools, JSON-schema registry, validator (LLM never touches FFmpeg), session state | ✅ full chat session verified on both brains |
| Hands v2.1 | `render.py` — text/zoom/polish/fade ops + `afade` audio sync + `loudnorm` second pass | ✅ fade + loudnorm added by the author, verified end-to-end (fade-in frame ≈ black, output 15.0s) |
| Web UI | `webui.py` (stdlib server, 27 MB RAM) + `web/static/` (html/css/js, no framework) | ✅ chat + timeline + video preview + Stop button verified |

### The web UI (Phase 2.5)

**One brain, three faces:** CLI (`agent.py`), demo, web (`webui.py`) — the web
backend *imports* the same tools, validator and brain management. No second
pipeline, no heavy stack (Open WebUI-class: 1–2 GB — disqualified).

```
python3 webui.py          → open http://localhost:8080 in Firefox
```

- **Chat-first** (type like in the terminal), plus minimal buttons:
  *Add video* (file picker → inbox/), *select* video by clicking,
  *Stop brain* (RAM control as a click)
- **Plan timeline**: keep (green) / cuts (red) / ops markers (yellow) drawn
  from the EDL; video preview plays in the browser (Range streaming)
- localhost-only in V1; LAN access = Phase 4 with proper auth
- Verified: chat roundtrip with real brain, plan auto-refresh, add-video,
  stop-brain (2.2 GB → 5.7 GB available after stop); server = 27 MB RSS

**Sessions are separate per face:** the terminal chat (`agent.py`) and the web
chat each keep their own conversation memory — they share *files* (plans/,
outbox/ — a CLI render shows up in the UI's outbox after a refresh), never
the chat transcript. Unifying sessions is a Phase 3 candidate.

**The tools** (the LLM's vocabulary — all deterministic Python underneath):
`list_videos` `select_video` `read_transcript` `find_silences` `apply_cuts` `remove_cuts` `cut_range` `measure` `add_text` `add_zoom` `adjust_polish` `add_fade` `add_loudnorm` `remove_ops` `show_plan` `render`

**NOTHING IS AUTOMATIC — senses propose, you dispose.** `find_silences` and
`measure` only ever *suggest* (stored as a proposal, applied to nothing).
Silences, black screens, or unusual passages may be the video's intended
aesthetics — only explicit human confirmation (`apply_cuts` after approval,
or a direct request via `cut_range`) enters the plan. `plan.py` applies
exactly `job["cuts"]` and nothing else.

**Safety architecture:** the model only fills JSON schemas; the validator rejects unknown ops, out-of-range timestamps, malformed rects. `render` requires `user_confirmed=true`, and the system prompt forbids calling it without explicit user consent — chat IS the review gate.

**Model swap:** edit the `MODEL` line in `agent.py`, point at any GGUF — the brain is a config line, not an architecture. (Ornith/GPT-OSS/8B-class remain out of reach on this machine: see model board below.)

## 8. Roadmap

- [x] Phase 0 — FFmpeg hands-on, feasibility proof, Quick Sync verified
- [x] Phase 1 — pipeline: plan → review → render (rules brain)
- [x] Phase 2 — agent: senses + llama.cpp + Spark-X2.5-4B (A/B winner) + chat + 13 tools (executor v2.1)
- [x] Phase 2.5 — web UI: async chat + timeline + preview + RAM control (stdlib only, 27 MB)
- [x] Phase 2.9 — consolidation: multi-video design doc (`docs/MULTI_VIDEO.md`), smoke-test suite (`smoke_test.py`)
- [ ] Phase 3 — **parked / reframed by the author:** presets parked (editing choices are per-video; presets would only ever apply on explicit request), multi-video documented-not-built (`docs/MULTI_VIDEO.md`). Remaining candidates when needed: render queue, session persistence.
- [ ] Phase 4 — NAS: inbox/outbox become shares, webhooks, worker as a service, DB-transcript adapter (see `docs/MULTI_VIDEO.md` §4 for the decision checklist)

## 8b. Smoke tests

One command verifies the whole pipeline end-to-end:

```
python3 smoke_test.py
```

Checks: all scripts compile · tool registry consistent (schemas ↔ functions) ·
validator rejects bad ops · silence math correct on the known transcript ·
plan.py produces the expected EDL · render produces a fresh output of the
right duration · senses agree with the planted silences. Restores `job.json`
and plan files afterwards; exits non-zero on any failure. Run it after every
change — it is the "did I break anything?" oracle.

## 9. Philosophy

- **Add features until impossible.** Every new editing capability = one new tool + one new line type in the edit list. The architecture does not change.
- **Trust is earned per decision type.** *And the author's line in the sand:
  NOTHING is automatic — senses propose, the human disposes, even for
  deterministic edits.* The review gate is never skipped.
- **The heavy lifting is already solved.** The 8 GB limit shaped a sequential, text-driven design — which turns out to be exactly what makes a small local LLM viable as the agent's brain.

## 10. Honest limits

- A 3B local model is not ChatGPT: slower, best at structured tasks — fine here because it only chooses tools, never edits pixels.
- Creative/motion-graphic editing (true "effects") is future work: each feature lands as a new op type + executor rule, as far as FFmpeg can go — which is far.
- Complex AI visual effects (background removal, tracking) = a future VLM *tool* — the architecture reserves the slot (see Principles), and on 8 GB it would be the first thing to hit the memory wall: the "better hardware" trigger. No rewrite needed to add it.

## 11. Install from zero (for a fresh clone)

The repo ships code only — no binaries, no weights. You need three things: FFmpeg, a llama.cpp build (the "brain server"), and one GGUF model.

### 11.1 Prerequisites

- Python 3.10+ (stdlib only, no pip packages)
- FFmpeg + ffprobe (v6+): `sudo apt install ffmpeg` / `sudo pacman -S ffmpeg`
- CMake + a C++ compiler (only to build llama.cpp): `sudo apt install cmake build-essential`
- ~4 GB free RAM for the agent, more if you pick a bigger model (see 11.4)

### 11.2 Build the brain server (llama.cpp)

Any recent llama.cpp works — the project talks to it over its OpenAI-compatible HTTP API, nothing fork-specific.

**Run these from OUTSIDE the project directory** (e.g. in `~/`, or anywhere temporary — the llama.cpp source is throwaway, only the `llama-server` binary is kept):

```
cd ~                              # or any temp dir — NOT inside FireReplayMotion
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_NATIVE=ON
cmake --build build --config Release -j4
```

Then copy the built binary INTO the project (replace `/path/to/FireReplayMotion` with where you cloned this repo):

```
mkdir -p /path/to/FireReplayMotion/tools/llama.cpp-master/build/bin
cp build/bin/llama-server /path/to/FireReplayMotion/tools/llama.cpp-master/build/bin/
```

The project expects `llama-server` at exactly `tools/llama.cpp-master/build/bin/llama-server` relative to its own folder (`agent.py` resolves paths from its own location). You can delete the llama.cpp clone afterwards — only the binary is used.

(On Intel iGPUs, Quick Sync only affects the *render* — FFmpeg, not llama.cpp. The brain runs on CPU.)

### 11.3 Download a model

**From INSIDE the project directory** (the model must sit in `models/` relative to it):

Any instruct GGUF works. The default is Spark-X2.5-4B (Q4_K_M, ~2.6 GB):

```
mkdir -p models
wget -O models/Spark-X2.5-4B-Q4_K_M.gguf \
  "https://huggingface.co/abenzerps/Spark-X2.5-4B-GGUF/resolve/main/Spark-X2.5-4B-Q4_K_M.gguf"
```

(Alternative mirror: `ngquocvinh/Spark-X2.5-4B-GGUF` on Hugging Face — same file name, Q4_K_M ≈ 2.6 GB. If it 404s, search "Spark-X2.5-4B GGUF" on huggingface.co, or set `EDITAI_MODEL` to any GGUF you have.)

### 11.4 Pick a brain (any GGUF instruct model works)

The brain is a config line, not an architecture. `agent.py` reads `EDITAI_MODEL`; the model only chooses tools and fills JSON — it never edits pixels. More powerful hardware → bigger brain, same architecture:

| Hardware | Suggested model | How |
|---|---|---|
| 8 GB RAM (reference machine) | Spark-X2.5-4B / Qwen3-4B Q4_K_M (~2.5 GB RAM) | default |
| 16 GB RAM | Qwen2.5-7B / Llama-3.1-8B Q4_K_M | `EDITAI_MODEL=models/qwen2.5-7b-instruct-q4_k_m.gguf python3 agent.py` |
| 32 GB+ / GPU offload | 14B–32B class Q4 | add `-ngl 99` to the llama-server flags in `agent.py` |

Rules of thumb: instruct-tuned, tool-calling-capable (function calling), Q4_K_M quant is the sweet spot. Smaller than 3B tends to fail at multi-step tool chaining; 4B is the verified floor on CPU.

### 11.5 First run

**From INSIDE the project directory** (all commands — the pipeline reads `inbox/`, `plans/`, `outbox/` relative to the current directory):

```
python3 smoke_test.py    # pipeline self-check (no brain needed)
python3 agent.py         # starts the brain on first chat (port 8321)
# or the web face:
python3 webui.py         # → http://localhost:8080
```

Environment variables: `EDITAI_MODEL` (path to GGUF), `EDITAI_PORT` (default 8321).
