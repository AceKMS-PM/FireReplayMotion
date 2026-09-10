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

def main():
    job = json.loads(Path("inbox/job.json").read_text())
    name = job["video"]
    min_gap = job["min_gap"]

    transcript = json.loads(Path(f"inbox/{name}.transcript.json").read_text())
    utts = transcript["utterances"]
    source = Path(f"inbox/{name}.mp4")
    duration = video_duration(source)

    segments = []
    cuts = []
    cur_start = utts[0]["start"]
    prev_end = utts[0]["end"]

    for u in utts[1:]:
        gap = u["start"] - prev_end
        if gap > min_gap:
            segments.append((cur_start, prev_end))
            cuts.append((prev_end, u["start"]))
            cur_start = u["start"]
        prev_end = u["end"]
    segments.append((cur_start, prev_end))

    if segments[0][0] > min_gap:
        cuts.append((0.0, segments[0][0]))
    if duration - segments[-1][1] > min_gap:
        cuts.append((segments[-1][1], duration))

    kept = sum(e - s for s, e in segments)
    removed = sum(e - s for s, e in cuts)

    plan = {
        "source": str(source),
        "approved": False,
        "min_gap": min_gap,
        "segments": segments,
        "cuts": cuts,
        "ops": job.get("ops", []),
        "result_duration": kept,
    }
    Path("plans").mkdir(exist_ok=True)
    plan_path = Path(f"plans/{name}.edl.json")
    plan_path.write_text(json.dumps(plan, indent=2))

    print(f"PLAN for {name} (min_gap = {min_gap}s)")
    print(f"  cuts found : {len(cuts)}")
    for s, e in cuts:
        print(f"    cut  {fmt(s)} -> {fmt(e)}  ({e - s:.1f}s of silence)")
    print(f"  segments kept: {len(segments)}")
    for s, e in segments:
        print(f"    keep {fmt(s)} -> {fmt(e)}")
    print(f"  duration: {duration:.1f}s -> {kept:.1f}s  ({removed:.1f}s removed)")
    print(f"\nSaved to {plan_path}")
    print("Next step: python3 review.py")

if __name__ == "__main__":
    main()
