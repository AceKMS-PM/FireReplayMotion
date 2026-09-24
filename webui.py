"""webui.py — Edit AI web interface (V1).

One brain, three faces: CLI (agent.py), demo, web (this file).
The web backend REUSES agent.py's tools, validator and brain management —
no second brain, no second pipeline.

Python stdlib only (no pip, no npm). localhost-only in V1.

Run:  python3 webui.py          → open http://localhost:8080
      python3 webui.py 8090     → custom port
"""
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent

ROOT = Path(__file__).parent
STATIC = ROOT / "web" / "static"

# one session (localhost, single user in V1)
session_lock = threading.Lock()
state = {"video": None, "ops": [], "min_gap": 2.0}
messages = [{"role": "system", "content": agent.SYSTEM_PROMPT}]

# async chat: the turn runs in a worker thread; the page polls for status.
# (Spark turns can outlive the browser's ~300 s HTTP timeout — a long
#  request/response would die in flight. A job + polling cannot.)
chat_status = {"running": False, "reply": None, "error": None, "text": ""}

agent._register()  # activate the shared tool registry


def _chat_worker(text):
    try:
        if not agent.server_up():
            print("[webui] starting brain for chat...")
            agent.ensure_server()
        reply = agent.agent_turn(text, state, messages)
        chat_status["reply"] = reply
    except Exception as e:
        chat_status["error"] = f"brain error: {e}"
    finally:
        chat_status["running"] = False


def snapshot():
    """Everything the page needs to draw the current situation."""
    name = state.get("video")
    plan = None
    if name:
        p = ROOT / "plans" / f"{name}.edl.json"
        if p.exists():
            try:
                plan = json.loads(p.read_text())
            except Exception:
                plan = None
    return {
        "videos": sorted(x.stem for x in (ROOT / "inbox").glob("*.mp4")),
        "outbox": sorted(x.name for x in (ROOT / "outbox").glob("*.mp4")),
        "transcripts": sorted(x.name for x in (ROOT / "inbox").glob("*.transcript.json")),
        "selected": name,
        "ops": state.get("ops", []),
        "cuts": state.get("cuts", []),
        "min_gap": state.get("min_gap", 2.0),
        "plan": plan,
        "brain_on": agent.server_up(),
    }


def parse_multipart(body, boundary):
    """Crude but solid single-file multipart extraction."""
    for part in body.split(b"--" + boundary):
        if b"filename=" not in part:
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        m = re.search(rb'filename="([^"]+)"', head)
        if m:
            return m.group(1).decode("utf-8", "replace"), data
    return None, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[webui] {fmt % args}")

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path, ctype):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # dev: always fresh JS
        self.end_headers()
        self.wfile.write(data)

    def send_media(self, path):
        """Video with Range support (needed for <video> seeking)."""
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                code = 206
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(end - start + 1)
        try:
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass  # browser aborted the stream (seek/scroll) — perfectly normal

    # ---------- GET ----------
    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route in ("/", "/index.html"):
            self.send_static(STATIC / "index.html", "text/html; charset=utf-8")
        elif route == "/style.css":
            self.send_static(STATIC / "style.css", "text/css")
        elif route == "/app.js":
            self.send_static(STATIC / "app.js", "text/javascript")
        elif route == "/api/state":
            self.send_json(snapshot())
        elif route == "/api/chat-status":
            st = dict(chat_status)
            st["state"] = snapshot()
            self.send_json(st)
        elif route.startswith("/media/inbox/") or route.startswith("/media/outbox/"):
            folder = route.split("/")[2]
            fname = Path(urllib.parse.unquote(route)).name
            p = ROOT / folder / fname
            if p.suffix == ".mp4" and p.exists():
                self.send_media(p)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if route == "/api/chat":
            text = json.loads(body.decode()).get("text", "").strip()
            if not text:
                self.send_json({"error": "empty message"})
                return
            if chat_status["running"]:
                self.send_json({"busy": True})
                return
            chat_status.update(running=True, reply=None, error=None, text=text)
            threading.Thread(target=_chat_worker, args=(text,),
                             daemon=True).start()
            self.send_json({"started": True})

        elif route == "/api/chat-status":
            st = dict(chat_status)
            st["state"] = snapshot()
            self.send_json(st)

        elif route == "/api/select":
            name = json.loads(body.decode()).get("video", "")
            with session_lock:
                result = agent.tool_select_video(name, state)
            self.send_json({"result": result, "state": snapshot()})

        elif route == "/api/stop-brain":
            got = session_lock.acquire(blocking=False)
            if not got:
                self.send_json({
                    "stopped": False,
                    "error": "the agent is still thinking — wait for its reply "
                             "before stopping the brain"})
                return
            try:
                self.send_json({"stopped": agent.stop_brain(), "state": snapshot()})
            finally:
                session_lock.release()

        elif route == "/api/add-video":
            m = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type", ""))
            if not m:
                self.send_json({"error": "expected multipart upload"}, 400)
                return
            fname, data = parse_multipart(body, m.group(1).encode())
            safe = Path(fname or "").name
            if not safe.lower().endswith(".mp4") or not data:
                self.send_json({"error": "please upload a .mp4 file"}, 400)
                return
            target = ROOT / "inbox" / safe
            if target.exists():
                self.send_json({"error": f"{safe} already exists in inbox — "
                                         "rename the file first (no silent "
                                         "overwrites)"}, 400)
                return
            target.write_bytes(data)
            print(f"[webui] added video: {safe} ({len(data) / 1e6:.1f} MB)")
            self.send_json({"added": safe, "state": snapshot()})

        else:
            self.send_error(404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Edit AI web UI — http://localhost:{port}  (Ctrl+C to stop)")
    print("  brain starts on first chat; the Stop button frees the RAM")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye — web server stopped.")
        if agent.server_up():
            print("⚠ brain still running (~2.5 GB in RAM). Free it with:")
            print("    python3 agent.py --stop-brain")
            print("  (or click ⏹ Stop brain in the UI before closing)")


if __name__ == "__main__":
    main()
