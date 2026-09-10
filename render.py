"""render.py v2 — the hands. Compiles an approved EDL into FFmpeg renders.

Supported ops (validated, never shell-injected):
  {"op":"text",   "start":s,"end":e,"text":"...","position":"top|center|bottom","size":60}
  {"op":"zoom",   "start":s,"end":e,"rect":[x,y,w,h]}   (fractions of frame)
  {"op":"polish", "start":s,"end":e|null,"saturation":1.3,"brightness":0.05,"contrast":1.1}
  {"op":"fade",   "start":s,"end":e,"dur":1.0}          (video fade + synced afade)
  {"op":"loudnorm"}                                     (whole-output EBU R128, second pass)

Zoom ops split segments (crop cannot be time-limited); text/polish use
'enable' windows with times remapped to each segment's local clock.
Run: python3 render.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

VALID_OPS = {"text", "zoom", "polish", "fade", "loudnorm"}


def pick_encoder():
    test = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=30", "-t", "0.5",
         "-c:v", "h264_qsv", "-f", "mp4", "/tmp/_enc_probe.mp4"],
        capture_output=True)
    if test.returncode == 0:
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    return ["-c:v", "libx264", "-preset", "veryfast"]


def video_size(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    w, h = r.stdout.strip().split(",")
    return int(w), int(h)


def esc_drawtext(s):
    return (s.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\'")
             .replace("%", "\\%"))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def split_for_zooms(segments, ops):
    """Zoom crops cannot use enable: split segments so each sub-segment
    is either fully zoomed or fully not. Returns [(s, e, rect|None)]."""
    zooms = [(op["start"], op["end"], tuple(op["rect"]))
             for op in ops if op["op"] == "zoom"]
    out = []
    for s, e in segments:
        points = {s, e}
        for zs, ze, _ in zooms:
            if s < zs < e:
                points.add(zs)
            if s < ze < e:
                points.add(ze)
        pts = sorted(points)
        for a, b in zip(pts, pts[1:]):
            mid = (a + b) / 2
            rect = next((r for zs, ze, r in zooms if zs <= mid <= ze), None)
            out.append((a, b, rect))
    return out


def build_vf_chain(sub, ops, width, height):
    """Compile ops overlapping this sub-segment into a filtergraph."""
    s, e, zoom_rect = sub
    filters = []

    if zoom_rect is not None:
        rx, ry, rw, rh = zoom_rect
        filters.append(
            f"crop=w=iw*{rw}:h=ih*{rh}:x=iw*{rx}:y=ih*{ry},"
            f"scale={width}:{height}")

    for op in ops:
        if op["op"] in ("zoom", "loudnorm"):
            continue  # zoom splits segments; loudnorm runs on the whole concat
        os_, oe = op.get("start"), op.get("end")
        if oe is None:
            oe = e  # open-ended op (e.g. polish whole video): run to segment end
        if oe <= s or os_ >= e:
            continue
        a, b = max(os_, s) - s, min(oe, e) - s

        if op["op"] == "text":
            pos = op.get("position", "top")
            y = {"top": "60", "center": "(h-th)/2", "bottom": "h-th-60"}[pos]
            size = op.get("size", 60)
            filters.append(
                "drawtext=text='" + esc_drawtext(op["text"]) + "'"
                f":x=(w-tw)/2:y={y}:fontsize={size}"
                f":fontcolor=white:borderw=2"
                f":enable='between(t,{a:.3f},{b:.3f})'")
        elif op["op"] == "polish":
            parts = [f"{k}={op[k]}" for k in
                     ("saturation", "brightness", "contrast") if k in op]
            filters.append(
                "eq=" + ":".join(parts) +
                f":enable='between(t,{a:.3f},{b:.3f})'")
        elif op["op"] == "fade":
            dur = min(op.get("dur", 1.0), max((b - a) / 2, 0.1))
            filters.append(f"fade=t=in:st={a:.3f}:d={dur:.3f}")
            filters.append(f"fade=t=out:st={max(b - dur, 0):.3f}:d={dur:.3f}")

    return ",".join(filters)


def audio_filters(sub, ops):
    """Audio filter chain for this sub-segment (afade only)."""
    s, e, _ = sub
    af = []
    for op in ops:
        if op["op"] != "fade":
            continue
        os_, oe = op.get("start"), op.get("end")
        if oe <= s or os_ >= e:
            continue
        dur = op.get("dur", 1.0)
        a, b = max(os_, s), min(oe, e)
        af.append(f"afade=t=in:st={a - s:.3f}:d={dur:.3f}")
        af.append(f"afade=t=out:st={b - s - dur:.3f}:d={dur:.3f}")
    return af


def main():
    plans = sorted(Path("plans").glob("*.edl.json"))
    approved = [p for p in plans if json.loads(p.read_text()).get("approved")]
    if not approved:
        print("No approved plan. Run plan.py then review.py first.")
        return

    plan_path = approved[-1]
    plan = json.loads(plan_path.read_text())
    name = plan_path.stem.replace(".edl", "")
    source = Path(plan["source"])
    segments = [(s, e) for s, e in plan["segments"]]
    ops = plan.get("ops", [])

    for op in ops:
        if op["op"] not in VALID_OPS:
            sys.exit(f"Unknown op type: {op['op']} (allowed: {sorted(VALID_OPS)})")

    for op in ops:
        if op.get("end") is None:
            continue
        s0, e0 = op["start"], op["end"]
        if not any(s0 >= s and e0 <= e for s, e in segments):
            sys.exit(f"Op {op['op']} [{s0}..{e0}] is not fully inside kept "
                     f"segments — refusing (op would be cut in half).")

    encoder = pick_encoder()
    enc_name = "Quick Sync (hardware)" if "h264_qsv" in encoder else "libx264 (software)"
    width, height = video_size(source)
    print(f"RENDER: {name}  |  encoder: {enc_name}  |  {len(ops)} op(s)")

    tmp = Path("tmp_render")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()

    subs = split_for_zooms(segments, ops)
    seg_paths = []
    for i, (s, e, rect) in enumerate(subs):
        vf = build_vf_chain((s, e, rect), ops, width, height)
        seg = tmp / f"seg_{i:03d}.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", str(source)]
        if vf:
            cmd += ["-vf", vf]
        af = audio_filters((s, e, rect), ops)
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += [*encoder, "-c:a", "aac", str(seg)]
        print(f"  [{i + 1}/{len(subs)}] {s:.2f}s -> {e:.2f}s"
              + ("  (zoom)" if rect else ""))
        subprocess.run(cmd, check=True)
        seg_paths.append(seg)

    concat_list = tmp / "list.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_paths))

    Path("outbox").mkdir(exist_ok=True)
    out_path = Path(f"outbox/{name}_clean.mp4")

    # loudnorm: if any op asks for it, run as a second pass on the concat.
    if any(op["op"] == "loudnorm" for op in ops):
        norm_tmp = tmp / "normalized.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             *encoder, "-c:a", "aac", str(norm_tmp)], check=True)
        shutil.move(norm_tmp, out_path)
    else:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c", "copy", str(out_path)], check=True)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out_path)], capture_output=True, text=True)
    print(f"DONE: {out_path}  ({float(probe.stdout):.1f}s)")

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()