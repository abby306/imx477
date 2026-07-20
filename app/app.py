#!/usr/bin/env python3
"""
IMX477 Studio — conveyor capture rig.

Two IMX477 cameras + an IR break-beam sensor on a Raspberry Pi 5.
When an object breaks the beam, both cameras photograph it and the shots are
filed into a timestamped folder inside the current session.

Open the same URL anywhere:
    on the Pi        http://localhost:8000
    over SSH / LAN   http://<pi-ip>:8000

Run:  python3 app.py
Stop: Ctrl+C
"""

import os
import sys
import time

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file, send_from_directory, abort)

from config import Config
from storage import Storage, BASE as MEDIA_BASE
from capture import CameraUnit, CaptureController
from sensor import SensorUnit

PORT = 8000

app = Flask(__name__)
cfg = Config()
storage = Storage()
CAMERAS = {}
controller = None
sensor = None


# --------------------------------------------------------------------------- #
def init():
    global controller, sensor

    try:
        from picamera2 import Picamera2
        infos = Picamera2.global_camera_info()
    except Exception as e:
        print(f"picamera2 unavailable: {e}", file=sys.stderr)
        infos = []

    if not infos:
        print("No cameras found. Check the ribbon cables and run:\n"
              "  rpicam-hello --list-cameras", file=sys.stderr)
        sys.exit(1)

    for info in infos:
        idx = info["Num"]
        try:
            print(f"  camera {idx} ({info.get('Model', '?')}) ...")
            CAMERAS[idx] = CameraUnit(idx, cfg, info.get("Model", "camera"))
        except Exception as e:
            print(f"  camera {idx} failed: {e}", file=sys.stderr)

    if not CAMERAS:
        print("No cameras could be opened.", file=sys.stderr)
        sys.exit(1)

    controller = CaptureController(cfg, storage, CAMERAS)
    # Sensor never blocks startup: no GPIO -> virtual mode, app still runs.
    sensor = SensorUnit(cfg, on_trigger=controller.on_trigger,
                        on_break_end=controller.backfill_break)
    if sensor.available:
        print(f"  sensor on GPIO {cfg.get('sensor.pin')} ready")
    else:
        print(f"  sensor unavailable ({sensor.error}) — running in virtual mode")


def cam(cam_id):
    c = CAMERAS.get(cam_id)
    if c is None:
        abort(404, description=f"no camera {cam_id}")
    return c


# --------------------------------------------------------------------------- #
# Pages / streams
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html",
                           cameras=[{"index": c.index, "model": c.model}
                                    for c in sorted(CAMERAS.values(), key=lambda x: x.index)])


@app.route("/stream/<int:cam_id>")
def stream(cam_id):
    c = cam(cam_id)

    def gen():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        while True:
            with c.frame_cv:
                c.frame_cv.wait(timeout=2)
                frame = c.latest_jpeg
            if frame is None:
                continue
            yield boundary + frame + b"\r\n"

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/media/<path:name>")
def media(name):
    return send_from_directory(MEDIA_BASE, name)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@app.route("/api/state")
def api_state():
    return jsonify({
        "cameras": [c.state() for c in sorted(CAMERAS.values(), key=lambda x: x.index)],
        "sensor": sensor.state(),
        "capture": controller.state(),
        "session": storage.session_info(),
        "disk_free_mb": storage.free_mb(),
        "belt_speed_mm_s": sensor.belt_speed_mm_s(),
    })


@app.route("/api/timeline")
def api_timeline():
    return jsonify(sensor.timeline())


@app.route("/api/events")
def api_events():
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    return jsonify({"events": controller.since(since), "cursor": controller.next_id - 1})


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(cfg.get())
    patch = request.get_json(silent=True) or {}
    before = cfg.get()
    data = cfg.update(patch)

    if "cameras" in patch or "overlays" in patch:
        for c in CAMERAS.values():
            c.apply_controls()
    # Rebuild the GPIO button only when its own wiring params moved.
    s_keys = ("pin", "pull_up", "bounce_time_ms", "enabled")
    if any(k in (patch.get("sensor") or {}) for k in s_keys):
        if before["sensor"] != data["sensor"]:
            sensor.build()
    return jsonify({"ok": True, "config": data, "sensor": sensor.state()})


@app.route("/api/config/preset/<action>", methods=["POST"])
def api_preset(action):
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    if action == "save":
        patch = body.get("patch")
        if patch is None:   # snapshot the live camera + sensor tuning
            full = cfg.get()
            patch = {"cameras": full["cameras"], "sensor": full["sensor"],
                     "capture": full["capture"]}
        return jsonify({"ok": True, "presets": cfg.save_preset(name, patch)})
    if action == "apply":
        data = cfg.apply_preset(name)
        if data is None:
            return jsonify({"ok": False, "error": "no such preset"}), 404
        for c in CAMERAS.values():
            c.apply_controls()
        return jsonify({"ok": True, "config": data})
    if action == "delete":
        return jsonify({"ok": True, "presets": cfg.delete_preset(name)})
    return jsonify({"ok": False, "error": "unknown action"}), 400


# --------------------------------------------------------------------------- #
# Manual camera actions
# --------------------------------------------------------------------------- #
@app.route("/api/photo/<int:cam_id>", methods=["POST"])
def api_photo(cam_id):
    c = cam(cam_id)
    body = request.get_json(silent=True) or {}
    full = bool(body.get("full_res", True))
    try:
        path = c.capture_still(storage.manual_photo_path(cam_id), full_res=full)
        return jsonify({"ok": True, "file": os.path.basename(path),
                        "url": "/media/" + storage.rel(path)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/record/<int:cam_id>/<action>", methods=["POST"])
def api_record(cam_id, action):
    c = cam(cam_id)
    try:
        if action == "start":
            path = c.start_recording(storage.manual_video_path(cam_id))
            return jsonify({"ok": True, "file": os.path.basename(path)})
        if action == "stop":
            r = c.stop_recording()
            if not r:
                return jsonify({"ok": True, "recording": False})
            r["url"] = "/media/" + storage.rel(r["path"])
            return jsonify({"ok": True, **r})
        return jsonify({"ok": False, "error": "unknown action"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --------------------------------------------------------------------------- #
# Run control
# --------------------------------------------------------------------------- #
@app.route("/api/arm", methods=["POST"])
def api_arm():
    body = request.get_json(silent=True) or {}
    armed = controller.set_armed(bool(body.get("armed")))
    return jsonify({"ok": True, "armed": armed, "session": storage.session_info()})


@app.route("/api/run/<action>", methods=["POST"])
def api_run(action):
    """One button for the client: start = open a run and arm; stop = both off.
    Sessions still exist on disk; the simple UI just never says the word."""
    body = request.get_json(silent=True) or {}
    if action == "start":
        label = (body.get("label") or "").strip() or "run"
        cfg.update({"storage": {"session_tag": label}})
        storage.start_session(label, cfg.get())
        controller.set_armed(True)
        return jsonify({"ok": True, "capturing": True, "session": storage.session_info()})
    if action == "stop":
        controller.set_armed(False)
        info = storage.stop_session()
        return jsonify({"ok": True, "capturing": False, "session": info})
    return jsonify({"ok": False, "error": "unknown action"}), 400


@app.route("/api/session/<action>", methods=["POST"])
def api_session(action):
    body = request.get_json(silent=True) or {}
    if action == "start":
        tag = (body.get("tag") or cfg.get("storage.session_tag")).strip()
        cfg.update({"storage": {"session_tag": tag}})
        return jsonify({"ok": True, "session": storage.start_session(tag, cfg.get())})
    if action == "stop":
        controller.set_armed(False)
        return jsonify({"ok": True, "session": storage.stop_session()})
    return jsonify({"ok": False, "error": "unknown action"}), 400


@app.route("/api/test/trigger", methods=["POST"])
def api_test_trigger():
    body = request.get_json(silent=True) or {}
    ms = int(body.get("duration_ms", 120))
    sensor.simulate_break(ms)
    return jsonify({"ok": True, "simulated_ms": ms, "armed": controller.armed})


@app.route("/api/sweep/arm", methods=["POST"])
def api_sweep_arm():
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on", True))
    if "delays_ms" in body:
        cfg.update({"capture": {"sweep_delays_ms": body["delays_ms"]}})
    if on and not controller.armed:
        controller.set_armed(True)
    return jsonify({"ok": True, "sweep_armed": controller.arm_sweep(on)})


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #
@app.route("/api/library")
def api_library():
    kind = request.args.get("kind", "sessions")
    if kind == "photos":
        return jsonify({"items": storage.list_manual("photos")})
    if kind == "videos":
        return jsonify({"items": storage.list_manual("videos")})
    if kind == "events":
        sid = request.args.get("session", "")
        return jsonify({"items": storage.list_session_events(sid)})
    return jsonify({"items": storage.list_sessions(), "usage": storage.usage()})


@app.route("/api/library/delete", methods=["POST"])
def api_library_delete():
    body = request.get_json(silent=True) or {}
    ok = storage.delete(body.get("path", ""))
    return jsonify({"ok": ok}), (200 if ok else 400)


@app.route("/api/session/<sid>/zip")
def api_session_zip(sid):
    p = storage.zip_session(sid)
    if not p:
        abort(404)
    return send_file(p, as_attachment=True, download_name=os.path.basename(p))


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("\nIMX477 Studio — starting")
    init()
    ip = ""
    try:
        ip = os.popen("hostname -I").read().strip().split(" ")[0]
    except Exception:
        pass
    print(f"\n  On the Pi:   http://localhost:{PORT}")
    if ip:
        print(f"  On the LAN:  http://{ip}:{PORT}")
    print("  Ctrl+C to stop\n")
    try:
        # threaded=True is required: MJPEG streams + API calls run concurrently.
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        try:
            if sensor:
                sensor.close()
            if storage.session:
                storage.stop_session()
        finally:
            for c in CAMERAS.values():
                c.close()
