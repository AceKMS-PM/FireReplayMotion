"""smoke_test.py — Edit AI end-to-end verification.

Run after every change: python3 smoke_test.py
Exit 0 = everything green. Restores job.json / plan files afterwards.
"""
import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPTS = ["agent.py", "webui.py", "plan.py", "render.py", "review.py",
           "measure.py"]

RESULTS = []


def check(name, fn):
    try:
        msg = fn()
        RESULTS.append((name, True, msg or "ok"))
        print(f"  PASS  {name:42s} {msg or ''}")
    except Exception as e:
        RESULTS.append((name, False, str(e)[:120]))
        print(f"  FAIL  {name:42s} {e}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return msg


# ---------------------------------------------------------------- tests

def t_compile():
    for s in SCRIPTS:
        py_compile.compile(str(ROOT / s), doraise=True)
    return f"{len(SCRIPTS)} scripts compile"


def t_registry():
    sys.path.insert(0, str(ROOT))
    import agent
    agent._register()
    schema_names = {t["function"]["name"] for t in agent.TOOLS}
    expect(schema_names == set(agent.TOOL_FNS),
           f"schema/fn mismatch: {schema_names ^ set(agent.TOOL_FNS)}")
    return f"{len(agent.TOOLS)} tools, schemas == functions"


def t_validator():
    import agent
    agent._register()
    state = {"video": "test_clip", "ops": [], "min_gap": 2.0}
    bad = [
        {"op": "text", "start": 0, "end": 3},                      # no text
        {"op": "zoom", "start": 0, "end": 3, "rect": [0, 0, 2, 2]},  # area > 1
        {"op": "text", "start": 99, "end": 200, "text": "x"},      # out of range
        {"op": "fade", "start": 5, "end": 2, "dur": 1},            # end < start
    ]
    for op in bad:
        err = agent._validate_op(dict(op), state)
        expect(err is not None, f"validator ACCEPTED bad op: {op}")
    good = {"op": "fade", "start": 0, "end": 3, "dur": 1.0}
    expect(agent._validate_op(dict(good), state) is None,
           "validator rejected a good op")
    return "4 bad ops rejected, good op accepted"


def t_silence_math():
    import agent
    agent._register()
    state = {"video": "test_clip", "ops": [], "cuts": [], "proposal": None,
             "min_gap": 2.0}
    r = agent.tool_find_silences(2.0, state)
    expect(r["cut_points"] == [[6.0, 9.0], [13.0, 16.0], [20.0, 23.0]],
           f"unexpected cuts: {r['cut_points']}")
    expect(r["seconds_removed"] == 9.0, f"removed={r['seconds_removed']}")
    expect(state["cuts"] == [],
           "find_silences applied cuts without user consent")
    return "proposal = 3 silences (9.0s), confirmed cuts untouched"


def t_apply_flow():
    import agent
    agent._register()
    state = {"video": "test_clip", "ops": [], "cuts": [], "proposal": None,
             "min_gap": 2.0}
    p = agent.tool_find_silences(2.0, state)["cut_points"]
    agent.tool_apply_cuts(p, state)
    plan = agent.tool_show_plan(state)
    expect(plan["result_duration"] == 15.0,
           f"after apply: {plan['result_duration']} != 15.0")
    agent.tool_remove_cuts(state)
    expect(agent.tool_show_plan(state)["result_duration"] == 24.0,
           "remove_cuts did not restore full video")
    return "propose → confirm → 15.0s → remove_cuts → 24.0s"


def t_cut_range():
    import agent
    agent._register()
    state = {"video": "test_clip", "ops": [], "cuts": [], "proposal": None,
             "min_gap": 2.0}
    r = agent.tool_cut_range(18.0, 24.0, state)   # "cut the last 6 seconds"
    expect(r.get("cut_added") == [18.0, 24.0], f"cut_range wrong: {r}")
    bad = agent.tool_cut_range(30.0, 40.0, state)  # beyond duration
    expect("error" in bad, "cut_range accepted out-of-range cut")
    plan = agent.tool_show_plan(state)
    expect(plan["result_duration"] == 18.0,
           f"cut last 6s alone must be 18.0s (no auto silences), got "
           f"{plan['result_duration']}")
    expect(plan["segments"][-1][1] == 18.0,
           f"video must end at 18.0s, got {plan['segments']}")
    return "cut last 6s alone -> 18.0s (nothing automatic)"


def t_measure():
    mpath = ROOT / "plans" / "test_clip.measurements.json"
    if not mpath.exists():
        subprocess.run([sys.executable, str(ROOT / "measure.py"), "test_clip"],
                       cwd=ROOT, check=True, capture_output=True)
    m = json.loads(mpath.read_text())
    got = [[s, e] for s, e in m["silences"]]
    expect(all(abs(a[0] - b[0]) < 0.2 and abs(a[1] - b[1]) < 0.2
               for a, b in zip(got, [[6.0, 9.0], [13.0, 16.0], [20.0, 23.0]])),
           f"senses disagree with transcript: {got}")
    return "audio sense agrees with transcript (±0.2s)"


def t_cut_range():
    import agent
    agent._register()
    state = {"video": "test_clip", "ops": [], "cuts": [], "proposal": None,
             "min_gap": 2.0}
    r = agent.tool_cut_range(18.0, 24.0, state)   # "cut the last 6 seconds"
    expect(r.get("cut_added") == [18.0, 24.0], f"cut_range wrong: {r}")
    bad = agent.tool_cut_range(30.0, 40.0, state)  # beyond duration
    expect("error" in bad, "cut_range accepted out-of-range cut")
    plan = agent.tool_show_plan(state)
    expect(plan["result_duration"] == 18.0,
           f"cut last 6s alone must be 18.0s (no auto silences), got "
           f"{plan['result_duration']}")
    expect(plan["segments"][-1][1] == 18.0,
           f"video must end at 18.0s, got {plan['segments']}")
    return "cut last 6s alone -> 18.0s (nothing automatic)"


def t_plan_and_render():
    job_path = ROOT / "inbox" / "job.json"
    original_job = job_path.read_bytes() if job_path.exists() else None
    plan_path = ROOT / "plans" / "test_clip.edl.json"
    original_plan = plan_path.read_bytes() if plan_path.exists() else None
    expected_out = ROOT / "outbox" / "test_clip_clean.mp4"
    backup = Path("/tmp/smoke_test_clip_clean_backup.mp4")
    had_output = expected_out.exists()
    if had_output:
        shutil.move(str(expected_out), backup)   # protect the user's render
    try:
        job = {"video": "test_clip", "min_gap": 2.0,
               "cuts": [[6.0, 9.0], [13.0, 16.0], [21.0, 24.0]],  # explicit
               "ops": [
            {"op": "fade", "start": 0, "end": 3, "dur": 1.0},
            {"op": "loudnorm"}]}
        job_path.write_text(json.dumps(job, indent=2))
        r = subprocess.run([sys.executable, str(ROOT / "plan.py")], cwd=ROOT,
                           capture_output=True, text=True)
        expect(r.returncode == 0, f"plan.py failed: {r.stderr[-200:]}")
        plan = json.loads(plan_path.read_text())
        # explicit cuts: [6,9],[13,16],[21,24] -> segments [0,6],[9,13],[16,21]
        expect(plan["segments"] == [[0.0, 6.0], [9.0, 13.0], [16.0, 21.0]],
               f"segments wrong: {plan['segments']}")
        expect(abs(plan["result_duration"] - 15.0) < 0.01,
               f"result {plan['result_duration']} != 15.0")
        expect(plan["ops"] == job["ops"], "ops lost between job and plan")
        plan["approved"] = True
        plan_path.write_text(json.dumps(plan, indent=2))
        r = subprocess.run([sys.executable, str(ROOT / "render.py")], cwd=ROOT,
                           capture_output=True, text=True)
        expect(r.returncode == 0, f"render.py failed: {r.stderr[-200:]}")
        expect(expected_out.exists(), "render produced no output file")
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0",
                            str(expected_out)], capture_output=True, text=True)
        dur = float(p.stdout.strip())
        expect(abs(dur - 15.0) < 0.5, f"duration {dur:.1f}s != ~15.0s")
        return f"render OK: {expected_out.name} ({dur:.1f}s, explicit cuts)"
    finally:
        # restore the exact pre-test world
        expected_out.unlink(missing_ok=True)
        if had_output and backup.exists():
            shutil.move(str(backup), expected_out)
        if original_job is not None:
            job_path.write_bytes(original_job)
        else:
            job_path.unlink(missing_ok=True)
        if original_plan is not None:
            plan_path.write_bytes(original_plan)


def t_webui_import():
    import webui
    snap = webui.snapshot()
    expect("videos" in snap and "brain_on" in snap, "snapshot shape broken")
    return "webui imports, snapshot works (no server started)"


# ---------------------------------------------------------------- main

def main():
    print("Edit AI smoke test")
    print("=" * 60)
    check("compile", t_compile)
    check("tool registry", t_registry)
    check("validator", t_validator)
    check("silence math (proposal)", t_silence_math)
    check("propose → confirm → remove", t_apply_flow)
    check("cut_range (explicit only)", t_cut_range)
    check("senses (measure)", t_measure)
    check("plan → render (explicit cuts + ops)", t_plan_and_render)
    check("webui import", t_webui_import)
    print("=" * 60)
    fails = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(fails)}/{len(RESULTS)} passed"
          + (f"  —  {len(fails)} FAILED" if fails else "  —  ALL GREEN"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
