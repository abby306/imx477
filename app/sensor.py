"""
IR break-beam sensor.

Wraps the tested wiring: gpiozero Button on GPIO 17, pull_up=True, where a
broken beam reads as "pressed". `invert` flips that if a replacement part idles
the other way.

If gpiozero/lgpio is unavailable or the pin is busy, the unit drops into
VIRTUAL mode: no hardware, but simulate_break() still drives the full trigger
path. That means the capture pipeline stays testable with no sensor wired, and
the app never fails to boot because of GPIO.
"""

import threading
import time
from collections import deque


class SensorUnit:
    def __init__(self, cfg, on_trigger, on_state=None, on_break_end=None):
        """
        cfg          — Config instance
        on_trigger   — callable(kind, t0_monotonic, break_ms|None); fires a capture
        on_state     — callable(broken: bool); UI/state hook, called on every edge
        on_break_end — callable(duration_ms); every clear edge, so a capture taken
                       while the item was still in the beam can learn how long the
                       break turned out to be (that's what gives you belt speed).
        """
        self.cfg = cfg
        self.on_trigger = on_trigger
        self.on_state = on_state or (lambda b: None)
        self.on_break_end = on_break_end or (lambda ms: None)

        self.available = False
        self.virtual = True
        self.error = None
        self.button = None

        self.broken = False
        self.break_start = None
        self.last_break_ms = None
        self.break_count = 0
        self.trigger_count = 0
        self.skipped_count = 0
        self.last_trigger_at = 0.0
        self.jam = False

        # Edge log powers the Sensor tab timeline — this is how you see chatter
        # and set debounce from evidence rather than guessing.
        self.edges = deque(maxlen=400)
        self.t_origin = time.monotonic()

        self.lock = threading.RLock()
        self._running = True
        self.build()

        self._watchdog = threading.Thread(target=self._watch_jam, daemon=True)
        self._watchdog.start()

    # ---- hardware ----------------------------------------------------------
    def build(self):
        """(Re)create the gpiozero Button from current config. Safe to re-call."""
        s = self.cfg.get("sensor")
        self.teardown()
        if not s.get("enabled", True):
            self.available = False
            self.virtual = True
            self.error = "sensor disabled in settings"
            return
        try:
            from gpiozero import Button
            bounce = s["bounce_time_ms"] / 1000.0
            self.button = Button(
                s["pin"],
                pull_up=bool(s["pull_up"]),
                bounce_time=bounce if bounce > 0 else None,
            )
            self.button.when_pressed = self._on_pressed
            self.button.when_released = self._on_released
            self.available = True
            self.virtual = False
            self.error = None
            # Adopt the real current level so the UI isn't lying at startup.
            self.broken = self._level_to_broken(self.button.is_pressed)
        except Exception as e:
            self.button = None
            self.available = False
            self.virtual = True
            self.error = f"{type(e).__name__}: {e}"

    def teardown(self):
        if self.button is not None:
            try:
                self.button.close()
            except Exception:
                pass
            self.button = None

    def _level_to_broken(self, pressed):
        return (not pressed) if self.cfg.get("sensor.invert") else bool(pressed)

    # ---- edges -------------------------------------------------------------
    def _on_pressed(self):
        self._edge(self._level_to_broken(True))

    def _on_released(self):
        self._edge(self._level_to_broken(False))

    def _edge(self, broken):
        now = time.monotonic()
        with self.lock:
            if broken == self.broken:
                return
            self.broken = broken
            self.edges.append({"t": round(now - self.t_origin, 4), "broken": broken})
            if broken:
                self.break_start = now
                self.break_count += 1
                dur = None
            else:
                dur = (now - self.break_start) * 1000.0 if self.break_start else None
                self.last_break_ms = round(dur, 1) if dur else None
                if self.jam:
                    self.jam = False
        self.on_state(broken)
        if not broken and dur is not None:
            try:
                self.on_break_end(dur)
            except Exception:
                pass
        self._maybe_trigger("break" if broken else "clear", now, dur)

    # ---- trigger gating ----------------------------------------------------
    def _maybe_trigger(self, kind, t0, break_ms):
        s = self.cfg.get("sensor")
        edge = s["trigger_edge"]
        if edge != "both" and edge != kind:
            return
        now = time.monotonic()
        if (now - self.last_trigger_at) * 1000.0 < s["cooldown_ms"]:
            with self.lock:
                self.skipped_count += 1
            return
        if self.jam:
            with self.lock:
                self.skipped_count += 1
            return
        # Claim the slot at schedule time so a burst of edges can't queue up.
        self.last_trigger_at = now
        threading.Thread(
            target=self._delayed_fire, args=(kind, t0, break_ms), daemon=True
        ).start()

    def _delayed_fire(self, kind, t0, break_ms):
        s = self.cfg.get("sensor")
        delay = s["trigger_delay_ms"] / 1000.0
        min_break = s["min_break_ms"] / 1000.0

        if kind == "break" and min_break > 0:
            # Validate it's a real object before spending a capture on it.
            time.sleep(min_break)
            if not self.broken:
                with self.lock:
                    self.skipped_count += 1
                return
            remaining = delay - min_break
            if remaining > 0:
                time.sleep(remaining)
        elif delay > 0:
            time.sleep(delay)

        if self.jam:
            return
        # If the item has already left the beam by the time we fire (common once
        # trigger_delay exceeds the break), the duration is known now — use it.
        if kind == "break" and break_ms is None and not self.broken:
            break_ms = self.last_break_ms
        with self.lock:
            self.trigger_count += 1
        self.on_trigger(kind, t0, break_ms)

    def effective_delay_ms(self):
        """What the delay actually works out to once noise-rejection is applied."""
        s = self.cfg.get("sensor")
        if s["trigger_edge"] == "clear":
            return s["trigger_delay_ms"]
        return max(s["trigger_delay_ms"], s["min_break_ms"])

    # ---- jam ---------------------------------------------------------------
    def _watch_jam(self):
        while self._running:
            try:
                s = self.cfg.get("sensor")
                with self.lock:
                    if (self.broken and self.break_start
                            and not self.jam
                            and (time.monotonic() - self.break_start) * 1000.0 > s["max_break_ms"]):
                        self.jam = True
            except Exception:
                pass
            time.sleep(0.1)

    # ---- test path ---------------------------------------------------------
    def simulate_break(self, duration_ms=120):
        """Drive the full trigger chain in software — works with no hardware."""
        def run():
            self._edge(True)
            time.sleep(duration_ms / 1000.0)
            self._edge(False)
        threading.Thread(target=run, daemon=True).start()

    # ---- state -------------------------------------------------------------
    def state(self):
        s = self.cfg.get("sensor")
        with self.lock:
            held_ms = None
            if self.broken and self.break_start:
                held_ms = round((time.monotonic() - self.break_start) * 1000.0)
            return {
                "available": self.available,
                "virtual": self.virtual,
                "error": self.error,
                "broken": self.broken,
                "held_ms": held_ms,
                "last_break_ms": self.last_break_ms,
                "break_count": self.break_count,
                "trigger_count": self.trigger_count,
                "skipped_count": self.skipped_count,
                "jam": self.jam,
                "pin": s["pin"],
                "effective_delay_ms": self.effective_delay_ms(),
            }

    def timeline(self):
        with self.lock:
            return {"origin": round(time.monotonic() - self.t_origin, 3),
                    "edges": list(self.edges)}

    def belt_speed_mm_s(self):
        """Item length / break duration. Gives you the delay you actually want."""
        length = self.cfg.get("storage.item_length_mm") or 0
        if not length or not self.last_break_ms:
            return None
        return round(length / (self.last_break_ms / 1000.0), 1)

    def close(self):
        self._running = False
        self.teardown()
