"""
Filing scheme.

    media/
    |- manual/
    |  |- photos/2026-07-17/143205_123_cam0.jpg
    |  '- videos/2026-07-17/143000_cam0.mp4
    '- sessions/
       '- 2026-07-17_1430_belt-test-01/
          |- session.json              config snapshot + totals
          |- 0001_143205.123/          one object
          |  |- cam0.jpg
          |  |- cam1.jpg
          |  '- event.json
          '- sweep_0002_143210.900/    delay-sweep frames

Top level splits by intent: deliberate (manual) vs triggered (sessions).
A session is the unit you hand downstream or zip up.
"""

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
MANUAL_PHOTOS = os.path.join(BASE, "manual", "photos")
MANUAL_VIDEOS = os.path.join(BASE, "manual", "videos")
SESSIONS = os.path.join(BASE, "sessions")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(s, fallback="run"):
    s = _SAFE.sub("-", (s or "").strip()).strip("-")
    return s[:40] or fallback


def _day():
    return datetime.now().strftime("%Y-%m-%d")


def _stamp(ms=False):
    fmt = "%H%M%S_%f" if ms else "%H%M%S"
    s = datetime.now().strftime(fmt)
    return s[:-3] if ms else s


class Storage:
    def __init__(self, base=BASE):
        self.base = base
        self.manual_photos = os.path.join(base, "manual", "photos")
        self.manual_videos = os.path.join(base, "manual", "videos")
        self.sessions = os.path.join(base, "sessions")
        self.lock = threading.RLock()
        self.session = None          # dict: id, dir, tag, started, count
        for d in (self.manual_photos, self.manual_videos, self.sessions):
            os.makedirs(d, exist_ok=True)

    # ---- manual ------------------------------------------------------------
    def manual_photo_path(self, cam_index):
        d = os.path.join(self.manual_photos, _day())
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{_stamp(ms=True)}_cam{cam_index}.jpg")

    def manual_video_path(self, cam_index):
        d = os.path.join(self.manual_videos, _day())
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{_stamp()}_cam{cam_index}.mp4")

    # ---- sessions ----------------------------------------------------------
    def start_session(self, tag, config_snapshot):
        with self.lock:
            if self.session:
                self.stop_session()
            sid = f"{_day()}_{datetime.now().strftime('%H%M')}_{_slug(tag)}"
            path = os.path.join(self.sessions, sid)
            # collision if two sessions start in the same minute with same tag
            n = 2
            while os.path.exists(path):
                path = os.path.join(self.sessions, f"{sid}-{n}")
                n += 1
            os.makedirs(path, exist_ok=True)
            sid = os.path.basename(path)
            self.session = {
                "id": sid,
                "dir": path,
                "tag": tag,
                "started": datetime.now().isoformat(timespec="seconds"),
                "count": 0,
            }
            self._write_session_json(config_snapshot)
            return self.session_info()

    def stop_session(self):
        with self.lock:
            if not self.session:
                return None
            self.session["stopped"] = datetime.now().isoformat(timespec="seconds")
            self._write_session_json()
            info = self.session_info()
            self.session = None
            return info

    def _write_session_json(self, config_snapshot=None):
        if not self.session:
            return
        p = os.path.join(self.session["dir"], "session.json")
        doc = {}
        if os.path.exists(p):
            try:
                with open(p) as f:
                    doc = json.load(f)
            except Exception:
                doc = {}
        doc.update({
            "id": self.session["id"],
            "tag": self.session["tag"],
            "started": self.session["started"],
            "events": self.session["count"],
        })
        if "stopped" in self.session:
            doc["stopped"] = self.session["stopped"]
        if config_snapshot is not None:
            # Snapshot the exact params used, so a batch is reproducible later.
            doc["config"] = config_snapshot
        with open(p, "w") as f:
            json.dump(doc, f, indent=2)

    def session_info(self):
        with self.lock:
            if not self.session:
                return None
            return {k: self.session[k] for k in ("id", "tag", "started", "count")}

    def new_event_dir(self, prefix=""):
        """Allocate the next numbered event folder in the current session."""
        with self.lock:
            if not self.session:
                return None, None
            self.session["count"] += 1
            seq = self.session["count"]
            name = f"{prefix}{seq:04d}_{_stamp(ms=True).replace('_', '.')}"
            path = os.path.join(self.session["dir"], name)
            os.makedirs(path, exist_ok=True)
            self._write_session_json()
            return path, seq

    def write_event_json(self, event_dir, doc):
        with open(os.path.join(event_dir, "event.json"), "w") as f:
            json.dump(doc, f, indent=2)

    # ---- disk --------------------------------------------------------------
    def free_mb(self):
        try:
            st = os.statvfs(self.base)
            return int(st.f_bavail * st.f_frsize / (1024 * 1024))
        except Exception:
            return -1

    def usage(self):
        out = {}
        for label, d in (("manual photos", self.manual_photos),
                         ("manual videos", self.manual_videos),
                         ("sessions", self.sessions)):
            total = 0
            for root, _dirs, files in os.walk(d):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            out[label] = round(total / (1024 * 1024), 1)
        return out

    # ---- library -----------------------------------------------------------
    def rel(self, path):
        return os.path.relpath(path, self.base).replace(os.sep, "/")

    def list_manual(self, kind="photos", limit=200):
        root = self.manual_photos if kind == "photos" else self.manual_videos
        items = []
        for day in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
            dpath = os.path.join(root, day)
            if not os.path.isdir(dpath):
                continue
            for f in sorted(os.listdir(dpath), reverse=True):
                p = os.path.join(dpath, f)
                if not os.path.isfile(p):
                    continue
                items.append({
                    "name": f,
                    "day": day,
                    "url": "/media/" + self.rel(p),
                    "size_mb": round(os.path.getsize(p) / (1024 * 1024), 2),
                })
                if len(items) >= limit:
                    return items
        return items

    def list_sessions(self):
        out = []
        if not os.path.isdir(self.sessions):
            return out
        for sid in sorted(os.listdir(self.sessions), reverse=True):
            sdir = os.path.join(self.sessions, sid)
            if not os.path.isdir(sdir):
                continue
            meta = {}
            try:
                with open(os.path.join(sdir, "session.json")) as f:
                    meta = json.load(f)
            except Exception:
                pass
            events = [d for d in os.listdir(sdir) if os.path.isdir(os.path.join(sdir, d))]
            out.append({
                "id": sid,
                "tag": meta.get("tag", ""),
                "started": meta.get("started", ""),
                "stopped": meta.get("stopped"),
                "events": len(events),
                "active": bool(self.session and self.session["id"] == sid),
            })
        return out

    def list_session_events(self, sid, limit=60):
        sdir = os.path.join(self.sessions, os.path.basename(sid))
        if not os.path.isdir(sdir):
            return []
        names = sorted([d for d in os.listdir(sdir)
                        if os.path.isdir(os.path.join(sdir, d))], reverse=True)[:limit]
        out = []
        for n in names:
            edir = os.path.join(sdir, n)
            doc = {}
            try:
                with open(os.path.join(edir, "event.json")) as f:
                    doc = json.load(f)
            except Exception:
                pass
            shots = sorted([f for f in os.listdir(edir) if f.lower().endswith((".jpg", ".png"))])
            out.append({
                "name": n,
                "session": sid,
                "meta": doc,
                "shots": [{"name": s, "url": "/media/" + self.rel(os.path.join(edir, s))}
                          for s in shots],
            })
        return out

    def zip_session(self, sid):
        sdir = os.path.join(self.sessions, os.path.basename(sid))
        if not os.path.isdir(sdir):
            return None
        out = os.path.join("/tmp", f"{os.path.basename(sid)}")
        return shutil.make_archive(out, "zip", root_dir=self.sessions, base_dir=os.path.basename(sid))

    def delete(self, relpath):
        """Delete a file or a whole session/event folder. Refuses to escape media/."""
        target = os.path.abspath(os.path.join(self.base, relpath))
        if not target.startswith(os.path.abspath(self.base) + os.sep):
            return False
        if os.path.isfile(target):
            os.remove(target)
            return True
        if os.path.isdir(target):
            if self.session and os.path.abspath(self.session["dir"]) == target:
                return False  # don't delete the running session out from under us
            shutil.rmtree(target)
            return True
        return False
