"""measure.py — the agent's senses (eyes ladder, levels 2-3).

Measures, for one video, without any AI model:
  - silences      (audio, ffmpeg silencedetect)
  - scene changes (video signal, scene-score cut detection)
  - brightness    (YAVG sampled at 1 fps, signalstats)

Result: plans/<name>.measurements.json + human-readable summary.
Run:    python3 measure.py            (uses inbox/job.json video)
        python3 measure.py test_clip  (explicit video name)
"""
import json
import re
import subprocess
import sys
from pathlib import Path


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def video_duration(path):
    r = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def detect_silences(source, noise_db=-35, min_dur=0.8):
    """Level 2: audio sense. Returns [(start, end), ...] of silences."""
    r = sh(["ffmpeg", "-i", str(source), "-af",
            f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((round(s, 2), round(e, 2) if e is not None else None))
    return out


def detect_scene_changes(source, threshold=0.3, sample_fps=4.0):
    """Level 3: signal sense. Returns [t, ...] where the picture changes hard."""
    r = sh(["ffmpeg", "-i", str(source), "-vf",
            f"fps={sample_fps},select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-"])
    times = sorted({round(float(m), 2) for m in
                    re.findall(r"pts_time:([\d.]+)", r.stdout)})
    return times


def measure_brightness(source):
    """Level 3: objective numbers. YAVG (0=black, 255=white) at 1 fps."""
    r = sh(["ffprobe", "-v", "error", "-f", "lavfi",
            "-i", f"movie={path_escape(source)},fps=1,signalstats",
            "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
            "-of", "csv=p=0"])
    values = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                values.append(round(float(line), 1))
            except ValueError:
                pass
    return values


def path_escape(p):
    r"""movie= filter needs \: escaping on Windows-style colons; on linux plain path ok."""
    return str(p).replace("\\", "\\\\").replace(":", "\\:")


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
    else:
        name = json.loads(Path("inbox/job.json").read_text())["video"]
    source = Path(f"inbox/{name}.mp4")
    if not source.exists():
        sys.exit(f"No such video: {source}")

    duration = video_duration(source)
    print(f"MEASURE {name}  ({duration:.1f}s)")
    print("  [audio] listening for silences...")

    silences = detect_silences(source)
    for s, e in silences:
        end = f"{e:.2f}s" if e is not None else "?"
        print(f"    silence: {s:7.2f}s -> {end}")

    print("  [video] scanning for scene changes...")
    scenes = detect_scene_changes(source)
    print(f"    {len(scenes)} hard cuts at: " +
          (", ".join(f"{t:.1f}s" for t in scenes[:12]) or "none"))

    print("  [video] sampling brightness (1 fps)...")
    bright = measure_brightness(source)
    if bright:
        avg = sum(bright) / len(bright)
        dark = [(i, v) for i, v in enumerate(bright) if v < 20]
        print(f"    YAVG avg: {avg:.1f}/255, min: {min(bright):.1f}, "
              f"max: {max(bright):.1f}")
        if dark:
            print(f"    very dark seconds: {len(dark)}")

    Path("plans").mkdir(exist_ok=True)
    out_path = Path(f"plans/{name}.measurements.json")
    out_path.write_text(json.dumps({
        "video": name,
        "duration": duration,
        "silences": silences,
        "scene_changes": scenes,
        "brightness_yavg_1fps": bright,
        "brightness_avg": round(avg, 1) if bright else None,
    }, indent=2))
    print(f"  saved: {out_path}")


if __name__ == "__main__":
    main()
