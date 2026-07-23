"""
Cameras and the triggered-capture pipeline.

Latency is the whole game here. A beam-triggered shot uses capture_request()
off the already-running main stream (~40ms, preview unaffected). The full-res
mode switch (~1-2s, freezes preview) is manual-only — on a moving belt it would
photograph an empty frame.

Each camera runs ONE preview thread producing a shared JPEG; extra browser
viewers cost nothing. All libcamera calls per camera are serialised behind a
lock so a trigger can never race the preview.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput


class CameraUnit:
    def __init__(self, index, cfg, model="imx477"):
        self.index = index
        self.cfg = cfg
        self.model = model
        self.lock = threading.RLock()
        self.frame_cv = threading.Condition()
        self.latest_jpeg = None

        self.focus_score = 0.0
        self.focus_peak = 1.0
        self.clipping = 0.0
        self.recording = False
        self.record_start = 0.0
        self.record_path = None
        self.encoder = None
        self.error = None

        st = cfg.get("stream")
        self.preview_size = tuple(st["preview_size"])
        self.record_size = tuple(st["record_size"])
        self.still_size = tuple(st["still_size"])

        self.picam2 = Picamera2(index)
        self._configure()
        self.picam2.start()
        self.apply_controls()

        self._running = True
        self.thread = threading.Thread(target=self._preview_loop, daemon=True)
        self.thread.start()

    def _configure(self):
        st = self.cfg.get("stream")
        self.video_config = self.picam2.create_video_configuration(
            main={"size": tuple(st["record_size"]), "format": "YUV420"},
            lores={"size": tuple(st["preview_size"]), "format": "YUV420"},
            controls={"FrameRate": st["framerate"]},
            buffer_count=6,
        )
        self.still_config = self.picam2.create_still_configuration(
            main={"size": tuple(st["still_size"])}
        )
        self.picam2.configure(self.video_config)

    # ---- controls ----------------------------------------------------------
    def apply_controls(self):
        c = self.cfg.camera(self.index)
        ctrls = {
            "Brightness": float(c["brightness"]),
            "Contrast": float(c["contrast"]),
            "Saturation": float(c["saturation"]),
            "Sharpness": float(c["sharpness"]),
            "AeEnable": bool(c["auto_exposure"]),
            "AwbEnable": bool(c["auto_wb"]),
        }
        if not c["auto_exposure"]:
            ctrls["ExposureTime"] = int(c["exposure_us"])
            ctrls["AnalogueGain"] = float(c["gain"])
        with self.lock:
            try:
                self.picam2.set_controls(ctrls)
                self.error = None
            except Exception as e:
                self.error = str(e)
                print(f"[cam{self.index}] set_controls: {e}", file=sys.stderr)

    # ---- preview -----------------------------------------------------------
    def _preview_loop(self):
        while self._running:
            st = self.cfg.get("stream")
            interval = 1.0 / max(1, st["preview_fps"])
            t0 = time.time()
            try:
                with self.lock:
                    yuv = self.picam2.capture_array("lores")
            except Exception as e:
                print(f"[cam{self.index}] preview: {e}", file=sys.stderr)
                time.sleep(0.2)
                continue

            w, h = self.preview_size
            gray = yuv[:h, :w]
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV420p2BGR)

            self._focus(gray)
            self._overlays(bgr, gray)

            ok, buf = cv2.imencode(".jpg", bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), st["preview_quality"]])
            if ok:
                with self.frame_cv:
                    self.latest_jpeg = buf.tobytes()
                    self.frame_cv.notify_all()

            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)

    def _focus(self, gray):
        h, w = gray.shape
        bw, bh = int(w * 0.4), int(h * 0.4)
        x0, y0 = (w - bw) // 2, (h - bh) // 2
        roi = gray[y0:y0 + bh, x0:x0 + bw]
        self.focus_score = float(cv2.Laplacian(roi, cv2.CV_64F).var())
        self.focus_peak = max(self.focus_score, self.focus_peak * 0.995, 1.0)
        # Blown highlights matter once you go short-shutter + high gain.
        self.clipping = float((gray >= 250).sum()) / gray.size * 100.0

    def _overlays(self, bgr, gray):
        o = self.cfg.get("overlays")
        h, w = gray.shape
        if o["peaking"]:
            lap = cv2.convertScaleAbs(cv2.Laplacian(gray, cv2.CV_16S, ksize=3))
            bgr[lap > o["peak_threshold"]] = (0, 80, 255)
        if o["grid"]:
            for i in (1, 2):
                cv2.line(bgr, (w * i // 3, 0), (w * i // 3, h), (255, 255, 255), 1)
                cv2.line(bgr, (0, h * i // 3), (w, h * i // 3), (255, 255, 255), 1)
        if o["crosshair"]:
            cx, cy = w // 2, h // 2
            cv2.line(bgr, (cx - 16, cy), (cx + 16, cy), (0, 255, 200), 1)
            cv2.line(bgr, (cx, cy - 16), (cx, cy + 16), (0, 255, 200), 1)
        if o["focusbox"]:
            bw, bh = int(w * 0.4), int(h * 0.4)
            x0, y0 = (w - bw) // 2, (h - bh) // 2
            cv2.rectangle(bgr, (x0, y0), (x0 + bw, y0 + bh), (0, 255, 200), 1)

    # ---- fast grab ---------------------------------------------------------
    def grab(self, count=1, interval_ms=60):
        """Pull `count` frames off the live main stream. Returns BGR arrays.

        Buffers are copied and released immediately — holding requests would
        starve the camera's pool (buffer_count=6).
        """
        frames = []
        for i in range(max(1, count)):
            with self.lock:
                req = self.picam2.capture_request()
                try:
                    arr = req.make_array("main").copy()
                finally:
                    req.release()
            frames.append(arr)
            if i < count - 1:
                time.sleep(interval_ms / 1000.0)
        return [cv2.cvtColor(a, cv2.COLOR_YUV420p2BGR) for a in frames]

    def save_bgr(self, bgr, path):
        q = self.cfg.get("capture.jpeg_quality")
        cv2.imwrite(path, bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(q)])
        return path

    # ---- manual still ------------------------------------------------------
    def capture_still(self, path, full_res=True):
        with self.lock:
            if full_res and not self.recording:
                self.picam2.switch_mode_and_capture_file(self.still_config, path)
                return path
        bgr = self.grab(1)[0]
        return self.save_bgr(bgr, path)

    # ---- recording ---------------------------------------------------------
    def start_recording(self, path):
        with self.lock:
            if self.recording:
                return self.record_path
            self.encoder = H264Encoder(bitrate=10_000_000)
            # start_encoder (not start_recording) keeps the preview alive.
            self.picam2.start_encoder(self.encoder, FfmpegOutput(path), name="main")
            self.recording = True
            self.record_start = time.time()
            self.record_path = path
        return path

    def stop_recording(self):
        with self.lock:
            if not self.recording:
                return None
            self.picam2.stop_encoder(self.encoder)
            self.recording = False
            self.encoder = None
            path = self.record_path
        dur = int(time.time() - self.record_start)
        size = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
        return {"file": os.path.basename(path), "path": path,
                "duration": dur, "size_mb": round(size, 1)}

    # ---- state -------------------------------------------------------------
    def state(self):
        return {
            "index": self.index,
            "model": self.model,
            "focus_score": round(self.focus_score, 1),
            "focus_pct": round(min(self.focus_score / self.focus_peak, 1.0) * 100),
            "clipping_pct": round(self.clipping, 2),
            "recording": self.recording,
            "record_seconds": int(time.time() - self.record_start) if self.recording else 0,
            "error": self.error,
        }

    def close(self):
        self._running = False
        try:
            if self.recording:
                self.stop_recording()
            self.picam2.stop()
            self.picam2.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
class CaptureController:
    """Turns a sensor trigger into a filed event. Owns the event log."""

    def __init__(self, cfg, storage, cameras):
        self.cfg = cfg
        self.storage = storage
        self.cameras = cameras
        self.armed = False
        self.lock = threading.RLock()
        self.events = []
        self.next_id = 1
        self.sweep_armed = False
        self.last_error = None
        self.disarm_reason = None      # why capture auto-stopped, for the UI banner
        self._armed_before_sweep = False
        self._session_before_sweep = False
        self._awaiting_duration = []   # events captured mid-break, pending duration

    # ---- arming ------------------------------------------------------------
    def set_armed(self, armed):
        with self.lock:
            if armed and not self.storage.session:
                self.storage.start_session(self.cfg.get("storage.session_tag"),
                                           self.cfg.get())
            self.armed = bool(armed)
            if self.armed:
                self.disarm_reason = None   # a fresh run clears any old stop banner
            return self.armed

    def arm_sweep(self, on=True):
        with self.lock:
            if on and not self.sweep_armed:
                # Remember what to restore to: a sweep is a calibration one-shot
                # and must never leave a run capturing on its own.
                self._armed_before_sweep = self.armed
                self._session_before_sweep = bool(self.storage.session)
            self.sweep_armed = bool(on)
            return self.sweep_armed

    # ---- the trigger -------------------------------------------------------
    def on_trigger(self, kind, t0, break_ms):
        if not self.armed:
            return
        free = self.storage.free_mb()
        floor = self.cfg.get("storage.min_free_mb")
        if free >= 0 and free < floor:
            self.set_armed(False)
            self.disarm_reason = ("Capturing stopped automatically — only %d MB of "
                                  "space left (the safety floor is %d MB). Free up "
                                  "space, then press Start again." % (free, floor))
            self._log_note("disarmed: disk below %d MB" % floor)
            return
        try:
            if self.sweep_armed:
                self._do_sweep(kind, t0, break_ms)
                self.arm_sweep(False)
                # Calibration finished: put the run state back exactly as we
                # found it, so the sweep can never silently keep capturing.
                if not self._armed_before_sweep:
                    self.set_armed(False)
                    if not self._session_before_sweep:
                        self.storage.stop_session()
            else:
                self._do_event(kind, t0, break_ms)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[capture] {self.last_error}", file=sys.stderr)

    def _targets(self):
        want = self.cfg.get("capture.cameras")
        return [c for c in self.cameras.values() if c.index in want]

    def _do_event(self, kind, t0, break_ms):
        cap = self.cfg.get("capture")
        edir, seq = self.storage.new_event_dir()
        if edir is None:
            return
        fired = time.monotonic()
        results = {}
        errors = {}

        def shoot(cam):
            try:
                off = int(cap["per_camera_delay_ms"].get(str(cam.index), 0))
                if off > 0:
                    time.sleep(off / 1000.0)
                t_shot = time.monotonic()
                # Self-describing names: object number + camera (+ shot no. for
                # a burst), so a file still makes sense once it's out of its folder.
                if cap["mode"] == "full_res" and not cam.recording:
                    p = os.path.join(edir, f"obj{seq:04d}_cam{cam.index}.jpg")
                    cam.capture_still(p, full_res=True)
                    results[cam.index] = {"files": [os.path.basename(p)],
                                          "latency_ms": round((t_shot - t0) * 1000, 1)}
                    return
                frames = cam.grab(cap["burst_count"], cap["burst_interval_ms"])
                names = []
                for i, f in enumerate(frames):
                    name = (f"obj{seq:04d}_cam{cam.index}.jpg" if len(frames) == 1
                            else f"obj{seq:04d}_cam{cam.index}_{i + 1}.jpg")
                    cam.save_bgr(f, os.path.join(edir, name))
                    names.append(name)
                results[cam.index] = {"files": names,
                                      "latency_ms": round((t_shot - t0) * 1000, 1)}
            except Exception as e:
                errors[cam.index] = f"{type(e).__name__}: {e}"

        # Fire cameras in parallel so inter-camera skew is ms, not sequential.
        threads = [threading.Thread(target=shoot, args=(c,)) for c in self._targets()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lat = [r["latency_ms"] for r in results.values()] or [0]
        doc = {
            "seq": seq,
            "trigger": kind,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "break_duration_ms": round(break_ms, 1) if break_ms else None,
            "trigger_delay_ms": self.cfg.get("sensor.trigger_delay_ms"),
            "capture_mode": cap["mode"],
            "burst_count": cap["burst_count"],
            "latency_ms": round(sum(lat) / len(lat), 1),
            "cameras": {str(k): v for k, v in results.items()},
            "errors": {str(k): v for k, v in errors.items()} or None,
            "session": self.storage.session["id"] if self.storage.session else None,
        }
        self.storage.write_event_json(edir, doc)
        self._push(doc, edir)
        if kind == "break" and doc["break_duration_ms"] is None:
            with self.lock:
                self._awaiting_duration.append((edir, doc))
        self._webhook(doc, edir)

    def _do_sweep(self, kind, t0, break_ms):
        """Fire one camera-pair per configured delay, so you can pick the right one."""
        cap = self.cfg.get("capture")
        delays = sorted(set(int(d) for d in cap["sweep_delays_ms"]))[:8]
        edir, seq = self.storage.new_event_dir(prefix="sweep_")
        if edir is None:
            return
        shots = []
        base = time.monotonic()
        for d in delays:
            wait = (d / 1000.0) - (time.monotonic() - base)
            if wait > 0:
                time.sleep(wait)
            for cam in self._targets():
                try:
                    f = cam.grab(1)[0]
                    name = f"cam{cam.index}_d{d}.jpg"
                    cam.save_bgr(f, os.path.join(edir, name))
                    shots.append({"delay_ms": d, "camera": cam.index, "file": name})
                except Exception as e:
                    print(f"[sweep] cam{cam.index} d={d}: {e}", file=sys.stderr)
        doc = {
            "seq": seq,
            "trigger": kind,
            "type": "sweep",
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "break_duration_ms": round(break_ms, 1) if break_ms else None,
            "delays_ms": delays,
            "shots": shots,
            "session": self.storage.session["id"] if self.storage.session else None,
        }
        self.storage.write_event_json(edir, doc)
        self._push(doc, edir)

    # ---- event log (cursor-polled by the browser) --------------------------
    def _push(self, doc, edir):
        with self.lock:
            doc = dict(doc)
            doc["id"] = self.next_id
            self.next_id += 1
            doc["dir"] = self.storage.rel(edir)
            doc["urls"] = []
            try:
                for f in sorted(os.listdir(edir)):
                    if f.lower().endswith((".jpg", ".png")):
                        doc["urls"].append("/media/" + self.storage.rel(os.path.join(edir, f)))
            except OSError:
                pass
            self.events.append(doc)
            if len(self.events) > 300:
                self.events = self.events[-300:]

    def backfill_break(self, duration_ms):
        """The beam just cleared. Any event shot while the item was still in it
        can now learn its break duration — that's the belt-speed input."""
        with self.lock:
            pending, self._awaiting_duration = self._awaiting_duration, []
        for edir, doc in pending:
            doc["break_duration_ms"] = round(duration_ms, 1)
            try:
                self.storage.write_event_json(edir, doc)
            except Exception as e:
                print(f"[capture] backfill: {e}", file=sys.stderr)
            with self.lock:
                for e in reversed(self.events):
                    if e.get("seq") == doc.get("seq") and e.get("type") != "sweep":
                        e["break_duration_ms"] = doc["break_duration_ms"]
                        break

    def _log_note(self, text):
        with self.lock:
            self.events.append({
                "id": self.next_id, "type": "note", "note": text,
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            self.next_id += 1

    def since(self, cursor):
        with self.lock:
            return [e for e in self.events if e["id"] > cursor]

    def _webhook(self, doc, edir):
        url = (self.cfg.get("integration.webhook_url") or "").strip()
        if not url:
            return

        def post():
            try:
                import urllib.request
                payload = json.dumps({"event": doc, "dir": edir}).encode()
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                print(f"[webhook] {e}", file=sys.stderr)

        threading.Thread(target=post, daemon=True).start()

    def state(self):
        with self.lock:
            return {"armed": self.armed, "sweep_armed": self.sweep_armed,
                    "cursor": self.next_id - 1, "last_error": self.last_error,
                    "disarm_reason": self.disarm_reason}
