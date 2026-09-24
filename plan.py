import json
import subprocess
from pathlib import Path

def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"

def video_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return float(r.stdout.strip())

def merge_cuts(cuts, duration):
    """Validate + sort + merge overlapping cuts. Returns [] on bad input."""
    merged = []
    for s, e in cuts:
        if not (isinstance(s, (int, float)) and isinstance(e, (int, float))
                and 0 <= s < e <= duration):
            continue
        merged.append((s, e))
    merged.sort()
    out = []
    for s, e in merged:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def segments_from_cuts(cuts, duration):
    """Complement of the cut list: the pieces to keep."""
    segs = []
    cursor = 0.0
    for s, e in cuts:
        if s - cursor > 0.01:
            segs.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > 0.01:
        segs.append((cursor, duration))
    return segs

def main():
    job = json.loads(Path("inbox/job.json").read_text())
    name = job["video"]
    source = Path(f"inbox/{name}.mp4")
    duration = video_duration(source)

    # The plan applies ONLY explicit cuts. Nothing is automatic:
    # silence proposals live in the transcript; the human decides (agent
    # writes job["cuts"] only after confirmation).
    cuts = merge_cuts(job.get("cuts", []), duration)
    segments = segments_from_cuts(cuts, duration)

    kept = sum(e - s for s, e in segments)
    removed = sum(e - s for s, e in cuts)

    plan = {
        "source": str(source),
        "approved": False,
        "cuts": [list(c) for c in cuts],
        "segments": segments,
        "ops": job.get("ops", []),
        "result_duration": kept,
    }
    Path("plans").mkdir(exist_ok=True)
    plan_path = Path(f"plans/{name}.edl.json")
    plan_path.write_text(json.dumps(plan, indent=2))

    print(f"PLAN for {name}")
    if not cuts:
        print("  no cuts (nothing approved yet)")
    else:
        print(f"  cuts applied : {len(cuts)}")
        for s, e in cuts:
            print(f"    cut  {fmt(s)} -> {fmt(e)}  ({e - s:.1f}s)")
    print(f"  segments kept: {len(segments)}")
    for s, e in segments:
        print(f"    keep {fmt(s)} -> {fmt(e)}")
    print(f"  duration: {duration:.1f}s -> {kept:.1f}s  ({removed:.1f}s removed)")
    print(f"\nSaved to {plan_path}")
    print("Next step: python3 review.py")

if __name__ == "__main__":
    main()