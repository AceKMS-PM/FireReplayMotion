"""agent.py — Edit AI: the conversational editing agent (V1).

Brain:  local LLM via llama-server (OpenAI-compatible, localhost only)
Hands:  plan.py / render.py pipeline (deterministic, validated)
Rule:   the LLM only fills tool schemas; Python translates to FFmpeg.

Run:  python3 agent.py            (interactive chat)
      python3 agent.py --demo     (scripted session for testing)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
MODEL = os.environ.get("EDITAI_MODEL", "models/Spark-X2.5-4B-Q4_K_M.gguf")
PORT = int(os.environ.get("EDITAI_PORT", "8321"))
SERVER_BIN = ROOT / "tools/llama.cpp-master/build/bin/llama-server"
BASE = f"http://127.0.0.1:{PORT}"
MAX_TOOL_ROUNDS = 16

SYSTEM_PROMPT = """You are Edit AI, a video editing agent. You edit ONE video per \
session, chosen by the user.

## Hard rules
1. You NEVER invent timestamps. Every timestamp you put in a plan op must come \
from read_transcript (utterance times), find_silences, measure, or what the \
user explicitly said.
2. You NEVER call render unless the user explicitly asked to render/proceed in \
their last message. When they do, call render with user_confirmed=true.
3. If the instruction is vague ("make it nicer"), ASK a question instead of \
guessing.
4. Before any render, briefly summarize in text what the plan will do.
5. Use the tools. Do not describe editing — do it via tool calls, then report \
the result in one or two sentences.
6. Do NOT select a video on greetings or vague messages. Only call \
select_video when the user names a video or clearly wants to edit one. \
If several videos exist and it is ambiguous, ask which one.
7. A plan described only in words is NOT a plan. Every cut/op MUST exist as a \
tool call (apply_cuts, cut_range, add_text, ...). Never compute timestamps \
yourself — pass the user's numbers to the tool and report the tool's own \
result. After any plan change, call show_plan so the user sees the true state.
8. NOTHING IS AUTOMATIC. find_silences and measure only PROPOSE. Silences, \
black screens, or unusual passages may be the video's intended aesthetics — \
only the user decides. Never apply a proposal until the user confirms. To \
apply a confirmed silence proposal, call apply_cuts with exactly the \
cut_points list the proposal returned. A manual request ("cut the last 6 \
seconds") enters the plan via cut_range — still call show_plan before render.

## Tool guidance
- list_videos / select_video: pick the session video first.
- read_transcript: see what is said and when (sentence-level).
- find_silences(min_gap): PROPOSES silence cuts. Returns cut_points — \
nothing is applied. Show the user, ask, and only apply via apply_cuts after \
explicit confirmation.
- apply_cuts(cuts): confirm a cut list (usually the approved proposal) — \
this REPLACES the current confirmed cuts.
- remove_cuts: clear all confirmed cuts.
- cut_range(start,end): user-requested cut, enters confirmed cuts immediately \
("cut the last 6 seconds" -> pass the user's numbers, e.g. for a 24s video \
cut_range(18,24) — verify the duration from select_video, never mental math).
- measure: silences (audio), scene changes, brightness — objective senses, \
proposals only.
- add_text(start,end,text,position,size), add_zoom(start,end,rect[fractions]),
  adjust_polish(saturation,brightness,contrast,start,end),
  add_fade(start,end,dur), add_loudnorm(): accumulate ops.
- add_fade: fade-in + fade-out over a window (video + audio synced).
- add_loudnorm: normalize whole-output audio to -16 LUFS (no params).
- remove_ops: clear accumulated ops.
- start/end are seconds (floats). rect = [x, y, w, h] as fractions of frame.

Keep answers short. You are running on a small local machine."""


# ---------------- tools (deterministic hands) ----------------

def _ffprobe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def tool_list_videos():
    vids = sorted(p.stem for p in (ROOT / "inbox").glob("*.mp4"))
    return {"videos": vids}


def tool_select_video(name, state):
    src = ROOT / "inbox" / f"{name}.mp4"
    if not src.exists():
        return {"error": f"no video named {name} in inbox/"}
    state["video"] = name
    state["ops"] = []
    state["cuts"] = []          # only EXPLICIT, user-confirmed cuts
    state["proposal"] = None    # last find_silences proposal (never applied)
    state["min_gap"] = 2.0
    dur = _ffprobe_duration(src)
    return {"selected": name, "duration_s": dur,
            "transcript_file": f"inbox/{name}.transcript.json (use read_transcript)"}


def _require_video(state):
    if not state.get("video"):
        return {"error": "no video selected — call select_video first"}
    return None


def tool_read_transcript(state):
    err = _require_video(state)
    if err:
        return err
    p = ROOT / "inbox" / f"{state['video']}.transcript.json"
    if not p.exists():
        return {"error": f"{p} not found"}
    return json.loads(p.read_text())


def tool_find_silences(min_gap, state):
    """PROPOSES silence cuts. NEVER applies anything — the user decides."""
    err = _require_video(state)
    if err:
        return err
    p = ROOT / "inbox" / f"{state['video']}.transcript.json"
    if not p.exists():
        return {"error": f"{p} not found"}
    utts = json.loads(p.read_text())["utterances"]
    dur = _ffprobe_duration(ROOT / "inbox" / f"{state['video']}.mp4")
    gaps = []
    prev = utts[0]["end"]
    for u in utts[1:]:
        if u["start"] - prev > min_gap:
            gaps.append([round(prev, 2), round(u["start"], 2)])
        prev = u["end"]
    if utts[0]["start"] > min_gap:
        gaps.insert(0, [0.0, round(utts[0]["start"], 2)])
    if dur - prev > min_gap:
        gaps.append([round(prev, 2), round(dur, 2)])
    state["min_gap"] = min_gap
    state["proposal"] = gaps          # stored for apply_cuts, NOT applied
    removed = sum(e - s for s, e in gaps)
    return {"cut_points": gaps, "seconds_removed": round(removed, 1),
            "note": "PROPOSAL ONLY — nothing is applied. To apply these "
                    "after the user confirms, call apply_cuts with exactly "
                    "this cut_points list."}


def tool_apply_cuts(cuts, state):
    """Explicitly confirm a cut list (usually the proposal the user approved).
    Replaces the current confirmed cuts."""
    err = _require_video(state)
    if err:
        return err
    dur = _ffprobe_duration(ROOT / "inbox" / f"{state['video']}.mp4")
    cleaned = []
    for c in cuts:
        if not (isinstance(c, list) and len(c) == 2
                and isinstance(c[0], (int, float))
                and isinstance(c[1], (int, float))
                and 0 <= c[0] < c[1] <= dur):
            return {"error": f"invalid cut {c} — need [start, end] with "
                            f"0 <= start < end <= {dur}"}
        cleaned.append([round(c[0], 2), round(c[1], 2)])
    state["cuts"] = cleaned
    removed = sum(e - s for s, e in cleaned)
    return {"applied_cuts": cleaned, "seconds_removed": round(removed, 1),
            "note": "confirmed — render will apply exactly these cuts. "
                    "Call show_plan to see the result."}


def tool_remove_cuts(state):
    err = _require_video(state)
    if err:
        return err
    n = len(state.get("cuts", []))
    state["cuts"] = []
    return {"cleared": n, "cuts": []}


def tool_measure(state):
    err = _require_video(state)
    if err:
        return err
    mpath = ROOT / "plans" / f"{state['video']}.measurements.json"
    if not mpath.exists():
        subprocess.run([sys.executable, str(ROOT / "measure.py"),
                        state["video"]], cwd=ROOT, check=True)
    m = json.loads(mpath.read_text())
    m.pop("brightness_yavg_1fps", None)  # keep context small
    return m


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _validate_op(op, state):
    name = op["op"]
    dur = _ffprobe_duration(ROOT / "inbox" / f"{state['video']}.mp4")
    if name in ("text", "zoom", "polish"):
        s, e = op.get("start"), op.get("end")
        if not (isinstance(s, (int, float)) and isinstance(e, (int, float))):
            return "start and end must be numbers"
        if not (0 <= s < e <= dur):
            return f"start/end must be 0 <= start < end <= {dur}"
    if name == "text":
        if not op.get("text") or len(op["text"]) > 200:
            return "text required (max 200 chars)"
        op["position"] = op.get("position", "top")
        if op["position"] not in ("top", "center", "bottom"):
            return "position must be top|center|bottom"
        op["size"] = _clamp(int(op.get("size", 60)), 12, 200)
    if name == "zoom":
        r = op.get("rect")
        if (not isinstance(r, list) or len(r) != 4 or
                not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in r)):
            return "rect must be [x,y,w,h] as fractions in 0..1"
        if r[2] * r[3] > 1.0:
            return "rect area must be <= 1.0"
    if name == "polish":
        for k in ("saturation", "brightness", "contrast"):
            if k in op and not isinstance(op[k], (int, float)):
                return f"{k} must be a number"
        if not any(k in op for k in ("saturation", "brightness", "contrast")):
            return "at least one of saturation/brightness/contrast required"
    if name == "fade":
        s, e = op.get("start"), op.get("end")
        if not (isinstance(s, (int, float)) and isinstance(e, (int, float))):
            return "start and end must be numbers"
        if not (0 <= s < e <= dur):
            return f"start/end must be 0 <= start < end <= {dur}"
        op["dur"] = _clamp(float(op.get("dur", 1.0)), 0.1, 10.0)
    if name == "loudnorm":
        pass  # no parameters — whole-output normalization
    return None


def tool_cut_range(start, end, state=None):
    """Remove an arbitrary time range. This IS a user request, so it enters
    the confirmed cuts immediately (still gated at render by show_plan)."""
    err = _require_video(state)
    if err:
        return err
    dur = _ffprobe_duration(ROOT / "inbox" / f"{state['video']}.mp4")
    if not (isinstance(start, (int, float)) and isinstance(end, (int, float))
            and 0 <= start < end <= dur):
        return {"error": f"need 0 <= start < end <= {dur}"}
    state.setdefault("cuts", []).append([round(start, 2), round(end, 2)])
    return {"cut_added": [round(start, 2), round(end, 2)],
            "confirmed_cuts": state["cuts"],
            "note": "confirmed — call show_plan to see resulting segments"}


def tool_add_text(start, end, text, position="top", size=60, state=None):
    op = {"op": "text", "start": start, "end": end, "text": text,
          "position": position, "size": int(size)}
    if err := _validate_op(op, state):
        return {"error": err}
    state["ops"].append(op)
    return {"added": op, "ops_count": len(state["ops"])}


def tool_add_zoom(start, end, rect, state=None):
    op = {"op": "zoom", "start": start, "end": end, "rect": rect}
    if err := _validate_op(op, state):
        return {"error": err}
    state["ops"].append(op)
    return {"added": op, "ops_count": len(state["ops"])}


def tool_adjust_polish(saturation=None, brightness=None, contrast=None,
                       start=0, end=None, state=None):
    op = {"op": "polish", "start": start, "end": end}
    for k in ("saturation", "brightness", "contrast"):
        if locals()[k] is not None:
            op[k] = locals()[k]
    if err := _validate_op(op, state):
        return {"error": err}
    state["ops"].append(op)
    return {"added": op, "ops_count": len(state["ops"])}


def tool_add_fade(start, end, dur=1.0, state=None):
    op = {"op": "fade", "start": start, "end": end, "dur": dur}
    if err := _validate_op(op, state):
        return {"error": err}
    state["ops"].append(op)
    return {"added": op, "ops_count": len(state["ops"])}


def tool_add_loudnorm(state=None):
    op = {"op": "loudnorm"}
    if err := _validate_op(op, state):
        return {"error": err}
    state["ops"].append(op)
    return {"added": op, "ops_count": len(state["ops"])}


def tool_remove_ops(state):
    n = len(state["ops"])
    state["ops"] = []
    return {"cleared": f"{n} ops", "ops_count": 0}


def tool_show_plan(state):
    err = _require_video(state)
    if err:
        return err
    # only EXPLICIT, user-confirmed cuts shape the result — nothing automatic.
    name = state["video"]
    dur = _ffprobe_duration(ROOT / "inbox" / f"{name}.mp4")
    raw = sorted(state.get("cuts", []))
    cuts, cursor, segments = [], 0.0, []
    for s, e in raw:
        if s < cursor:
            e = max(cursor, e)   # overlap: absorb
            s = cursor
        if e - s > 0.01:
            cuts.append([round(s, 2), round(e, 2)])
            cursor = e
        else:
            cursor = max(cursor, e)
    c = 0.0
    for s, e in cuts:
        if s - c > 0.01:
            segments.append([round(c, 2), round(s, 2)])
        c = max(c, e)
    if dur - c > 0.01:
        segments.append([round(c, 2), round(dur, 2)])
    kept = sum(e - s for s, e in segments)
    return {"video": name, "min_gap": state.get("min_gap", 2.0),
            "confirmed_cuts": state.get("cuts", []),
            "proposal": state.get("proposal", None),
            "segments": segments, "result_duration": round(kept, 1),
            "next_step": "tell the user this plan; render only if they confirm"}


def tool_render(user_confirmed, state):
    err = _require_video(state)
    if err:
        return err
    if not user_confirmed:
        return {"error": "user did not confirm — ask them first"}
    job = {"video": state["video"], "min_gap": state.get("min_gap", 2.0),
           "ops": state["ops"], "cuts": state.get("cuts", [])}
    (ROOT / "inbox" / "job.json").write_text(json.dumps(job, indent=2))
    before = {p.resolve(): p.stat().st_mtime
              for p in (ROOT / "outbox").glob("*.mp4")}
    log = []

    r = subprocess.run([sys.executable, str(ROOT / "plan.py")], cwd=ROOT,
                       capture_output=True, text=True)
    log.append((r.stdout + r.stderr).strip()[-300:])
    if r.returncode != 0:
        return {"error": "plan.py failed", "log": log}

    plan_path = ROOT / "plans" / f"{state['video']}.edl.json"
    plan = json.loads(plan_path.read_text())
    plan["approved"] = True          # the chat confirmation IS the review gate
    plan_path.write_text(json.dumps(plan, indent=2))

    r = subprocess.run([sys.executable, str(ROOT / "render.py")], cwd=ROOT,
                       capture_output=True, text=True)
    log.append((r.stdout + r.stderr).strip()[-300:])
    if r.returncode != 0:
        return {"error": "render.py failed", "log": log}

    after = {p.resolve(): p.stat().st_mtime
             for p in (ROOT / "outbox").glob("*.mp4")}
    fresh = sorted(p for p, m in after.items() if before.get(p) != m)
    if not fresh:
        return {"error": "render produced no new file — check log", "log": log}
    return {"done": True, "output": str(sorted(fresh)[-1]), "log": log}


# ---------------- tool registry (schema = the contract for the LLM) ----------

def _schema(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": req}}}


TOOLS = [
    _schema("list_videos", "List videos available in the inbox.", {}, []),
    _schema("select_video", "Select the session video (one per session).",
            {"name": {"type": "string"}}, ["name"]),
    _schema("read_transcript",
            "Read the full transcript with timestamps of the selected video.",
            {}, []),
    _schema("find_silences",
            "Compute silence cuts from transcript gaps (deterministic).",
            {"min_gap": {"type": "number", "description": "min silence seconds "
             "to cut, e.g. 2.0"}}, ["min_gap"]),
    _schema("measure",
            "Measure the video: silences (audio), scene changes, brightness.",
            {}, []),
    _schema("cut_range",
            "Remove an arbitrary time range [start,end] (user-requested cut, "
            "enters confirmed cuts immediately).",
            {"start": {"type": "number"}, "end": {"type": "number"}},
            ["start", "end"]),
    _schema("apply_cuts",
            "CONFIRM a cut list — use the exact cut_points a proposal "
            "(find_silences) returned, AFTER the user approves. Replaces "
            "current confirmed cuts.",
            {"cuts": {"type": "array",
                      "items": {"type": "array",
                                "items": {"type": "number"}}}},
            ["cuts"]),
    _schema("remove_cuts", "Clear all confirmed cuts.", {}, []),
    _schema("add_text", "Add a text overlay op.",
            {"start": {"type": "number"}, "end": {"type": "number"},
             "text": {"type": "string"},
             "position": {"type": "string", "enum": ["top", "center", "bottom"]},
             "size": {"type": "integer"}},
            ["start", "end", "text"]),
    _schema("add_zoom", "Add a zoom op. rect=[x,y,w,h] fractions of the frame.",
            {"start": {"type": "number"}, "end": {"type": "number"},
             "rect": {"type": "array", "items": {"type": "number"}}},
            ["start", "end", "rect"]),
    _schema("adjust_polish",
            "Add a color/brightness polish op (end omitted = whole video).",
            {"saturation": {"type": "number"}, "brightness": {"type": "number"},
             "contrast": {"type": "number"}, "start": {"type": "number"},
             "end": {"type": "number"}},
            []),
    _schema("add_fade", "Add a fade-in and fade-out (video + audio) over a time window.",
            {"start": {"type": "number"}, "end": {"type": "number"},
             "dur": {"type": "number", "description": "fade duration seconds, default 1"}}, ["start", "end"]),
    _schema("add_loudnorm", "Normalize audio loudness to EBU R128 (-16 LUFS) for the whole output.",
            {}, []),
    _schema("remove_ops", "Clear all accumulated ops.", {}, []),
    _schema("show_plan", "Show the current plan (cuts + ops).", {}, []),
    _schema("render",
            "Render the plan. ONLY call with user_confirmed=true after the "
            "user explicitly said render/proceed.",
            {"user_confirmed": {"type": "boolean"}}, ["user_confirmed"]),
]

TOOL_FNS = {}


def _register():
    TOOL_FNS["list_videos"] = lambda a, s: tool_list_videos()
    TOOL_FNS["select_video"] = lambda a, s: tool_select_video(a["name"], s)
    TOOL_FNS["read_transcript"] = lambda a, s: tool_read_transcript(s)
    TOOL_FNS["find_silences"] = lambda a, s: tool_find_silences(a["min_gap"], s)
    TOOL_FNS["measure"] = lambda a, s: tool_measure(state=s)
    TOOL_FNS["cut_range"] = lambda a, s: tool_cut_range(
        a["start"], a["end"], state=s)
    TOOL_FNS["apply_cuts"] = lambda a, s: tool_apply_cuts(a["cuts"], s)
    TOOL_FNS["remove_cuts"] = lambda a, s: tool_remove_cuts(s)
    TOOL_FNS["add_text"] = lambda a, s: tool_add_text(
        a["start"], a["end"], a["text"], a.get("position", "top"),
        a.get("size", 60), state=s)
    TOOL_FNS["add_zoom"] = lambda a, s: tool_add_zoom(
        a["start"], a["end"], a["rect"], state=s)
    TOOL_FNS["adjust_polish"] = lambda a, s: tool_adjust_polish(
        saturation=a.get("saturation"), brightness=a.get("brightness"),
        contrast=a.get("contrast"), start=a.get("start", 0),
        end=a.get("end"), state=s)
    TOOL_FNS["add_fade"] = lambda a, s: tool_add_fade(
        a["start"], a["end"], a.get("dur", 1.0), state=s)
    TOOL_FNS["add_loudnorm"] = lambda a, s: tool_add_loudnorm(state=s)
    TOOL_FNS["remove_ops"] = lambda a, s: tool_remove_ops(s)
    TOOL_FNS["show_plan"] = lambda a, s: tool_show_plan(s)
    TOOL_FNS["render"] = lambda a, s: tool_render(a["user_confirmed"], s)


# ---------------- brain management ----------------

def server_up():
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def ensure_server():
    if server_up():
        return
    print(f"  [agent] starting local brain (port {PORT})...")
    subprocess.Popen(
        [str(SERVER_BIN),
         "-m", str(ROOT / MODEL), "-c", "8192", "-t", "4",
         "--port", str(PORT), "--host", "127.0.0.1"],
        stdout=open("/tmp/editai_llama.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(90):
        if server_up():
            print("  [agent] brain online.")
            return
        time.sleep(1)
    sys.exit("llama-server failed to start — see /tmp/editai.log")


def brain(messages):
    body = json.dumps({
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 700,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"]


# ---------------- the loop ----------------

def agent_turn(user_text, state, messages):
    messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_TOOL_ROUNDS):
        msg = brain(messages)
        calls = msg.get("tool_calls") or []
        if calls:
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for c in calls:
                fn = c["function"]["name"]
                args = c["function"].get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {}
                try:
                    result = TOOL_FNS[fn](args, state)
                except Exception as e:
                    result = {"error": f"{fn}: {e}"}
                print(f"  🔧 {fn} -> {json.dumps(result, ensure_ascii=False)[:160]}")
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or ""})
        return msg.get("content") or "(no answer)"
    return "(stopped: too many tool rounds)"


def stop_brain():
    """Stop any llama-server this project started. Returns True if one was killed."""
    r = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.strip()]
    for pid in pids:
        try:
            subprocess.run(["kill", pid], check=False)
        except OSError:
            pass
    if pids:
        for _ in range(20):
            r2 = subprocess.run(["pgrep", "-x", "llama-server"],
                                capture_output=True, text=True)
            if not r2.stdout.strip():
                break
            time.sleep(0.5)
        return True
    return False


def main():
    state = {"video": None, "ops": [], "min_gap": 2.0}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    demo = "--demo" in sys.argv

    if "--stop-brain" in sys.argv:
        if stop_brain():
            print("brain stopped — RAM freed.")
        else:
            print("no brain running.")
        return

    print("Edit AI agent — terminal chat (V1)")
    print("  tools:", ", ".join(TOOL_FNS))
    ensure_server()
    print("  brain online")
    print("  'quit' = leave chat (brain stays warm)  |  '/stop' = leave AND free the RAM\n")

    while True:
        if demo:
            scripted = ["hi, which videos can I edit?", "select test_clip",
                        "what's in it, and are there silences?",
                        "cut the silences over 2s, add a title 'Chapter 1' "
                        "at the start for 3 seconds", "show the plan", "render"]
            for user_text in scripted:
                print(f"\nYou: {user_text}")
                print("🤖:", agent_turn(user_text, state, messages))
            return
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye (brain still warm — /stop next time to free RAM)")
            return
        if not user_text:
            continue
        if user_text.lower() in ("/stop", "stop"):
            print("stopping the brain...", end=" ")
            print("done, RAM freed." if stop_brain() else "nothing to stop.")
            return
        if user_text.lower() in ("quit", "exit", "bye"):
            print("bye (brain stays warm — '/stop' frees the RAM)")
            return
        # no echo print: the terminal already shows what was typed
        print("🤖:", agent_turn(user_text, state, messages))


if __name__ == "__main__":
    _register()
    main()
