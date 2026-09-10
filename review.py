import json
from pathlib import Path

def fmt(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"

def main():
    plans = sorted(Path("plans").glob("*.edl.json"))
    if not plans:
        print("No plan found. Run plan.py first.")
        return

    plan_path = plans[-1]
    plan = json.loads(plan_path.read_text())
    name = plan_path.stem.replace(".edl", "")

    print(f"REVIEW: {name}")
    print(f"  source  : {plan['source']}")
    print(f"  cuts    :")
    for s, e in plan["cuts"]:
        print(f"    - {fmt(s)} -> {fmt(e)}")
    print(f"  keeps   :")
    for s, e in plan["segments"]:
        print(f"    - {fmt(s)} -> {fmt(e)}")
    ops = plan.get("ops", [])
    if ops:
        print(f"  ops     :")
        for op in ops:
            if op["op"] == "text":
                print(f"    - text '{op['text']}' at {fmt(op['start'])}")
            elif op["op"] == "zoom":
                print(f"    - zoom {fmt(op['start'])} -> {fmt(op['end'])} "
                      f"rect={op['rect']}")
            elif op["op"] == "polish":
                p = {k: v for k, v in op.items()
                     if k in ("saturation", "brightness", "contrast")}
                print(f"    - polish {p} from {fmt(op['start'])}"
                      + (f" -> {fmt(op['end'])}" if op.get("end") else " (whole)"))
            elif op["op"] == "fade":
                print(f"    - fade in/out {fmt(op['start'])} -> {fmt(op['end'])} "
                      f"(dur {op.get('dur', 1.0)}s, video+audio)")
            elif op["op"] == "loudnorm":
                print("    - loudnorm whole output to -16 LUFS")
    print(f"  result  : ~{plan['result_duration']:.1f}s")
    print()

    answer = input("Approve this edit plan? [y/N] ").strip().lower()
    if answer == "y":
        plan["approved"] = True
        plan_path.write_text(json.dumps(plan, indent=2))
        print("APPROVED. Next step: python3 render.py")
    else:
        print("Not approved. Adjust inbox/job.json and rerun plan.py.")

if __name__ == "__main__":
    main()
