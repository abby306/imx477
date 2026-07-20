# IMX477 Studio

Two IMX477 cameras and an IR break-beam sensor on a Raspberry Pi 5. When an
object breaks the beam, both cameras photograph it and the shots are filed into
a timestamped folder. Everything is controlled from one web page that works on
the Pi's own monitor, over SSH, or from any device on the same Wi-Fi.

Every setting lives in the UI. You should never need to edit a config file.

The dashboard is deliberately plain by default — the everyday controls only.
Flip the **Advanced** switch in the top-right for tuning, diagnostics and the
signal timeline. The switch remembers itself, so the client never sees any of
it unless you turn it on.

## Install

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-flask python3-gpiozero
```

Check both cameras are seen before anything else:

```bash
rpicam-hello --list-cameras     # expect two imx477 entries
```

## Run

```bash
cd ~/imx477/app
python3 app.py
```

It prints the URLs. Open `http://localhost:8000` on the Pi, or
`http://<pi-ip>:8000` from a laptop or phone. `Ctrl+C` stops it.

The app boots even with no sensor attached — the Sensor tab says why, and
**Test trigger** still drives the whole capture path, so you can develop and
test with no hardware wired.

## The four tabs

**Cameras** — cameras on their own; the sensor can't take a photo from here.
Live view, a sharpness number for focusing, "Highlight what's in focus", manual
photo (full 12.3 MP) and video.

**Beam sensor** — the sensor on its own; no photos are taken here. A big
red/green light, a "Test the sensor" button, and (under Advanced) the signal
timeline. Safe to check with the lenses covered.

**Capture** — the two joined. One **Start / Stop** button. Starting opens a new
run and begins photographing; stopping closes it. There's no separate "session"
to manage.

**Photos** — everything saved, grouped by run with friendly dates. Download a
whole run as a zip.

## Tuning the trigger (do this once per belt speed)

The beam breaks at the *leading edge* of an object. Fire instantly and you
photograph its front lip at the edge of frame. `trigger delay` is what centres
it.

1. Capture → **Help me pick the wait time**, then send one real item down the belt.
2. The feed shows the same object shot at 0/50/100/150/200 ms.
3. Click the picture where it's centred. That wait time is saved.

Cross-check it: turn on **Advanced** and put the item's length into
`Object length` on the Capture tab. The
status bar then shows belt speed, computed from how long the beam was actually
broken. Every `event.json` records `break_duration_ms` too, so you can re-derive
it later from any batch.

## Two settings that decide your image quality

Both are one-click buttons on the Cameras tab:

- **Freeze motion** — short shutter + gain. With auto exposure on, the Pi picks
  a long shutter indoors and every moving item smears. Nothing downstream
  recovers from motion blur.
- **Lock brightness** — freezes exposure and white balance. Without it, every
  shot has a different brightness and colour cast, which hurts OCR and
  CLIP/FAISS matching. Frame 1 and frame 5000 should look the same.

Watch for the "too bright" warning under the sharpness number when shortening
the shutter.

## Where files go

```
media/
├─ manual/
│  ├─ photos/2026-07-17/143205_123_cam0.jpg
│  └─ videos/2026-07-17/143000_cam0.mp4
└─ sessions/
   └─ 2026-07-17_1430_belt-test-01/
      ├─ session.json          config snapshot + totals
      └─ 0001_143205.123/      one object
         ├─ cam0.jpg
         ├─ cam1.jpg
         └─ event.json         timing, delay, latency, break duration
```

Manual work and triggered runs never mix. A session is the unit you hand
downstream or zip up, and `session.json` records the exact settings used, so a
batch stays reproducible months later.

## Settings reference

All editable in the UI; persisted to `config.json` (gitignored, so `git pull`
won't wipe your tuning). `config.example.json` shows the defaults.

| In the UI | Advanced? | What it does |
|---|---|---|
| Wait before taking the photo | no | ms after the break before firing — centres the item |
| Photos of each object | no | burst count; more gives the classifier extra chances |
| Minimum gap between objects | yes | cooldown; stops one item making many folders |
| Debounce | yes | ignores contact chatter on the signal edge |
| Ignore blocks shorter than | yes | min break — rejects dust and insects |
| Call it a jam after | yes | max break; capture stops and a banner shows |
| Take the photo when the beam | yes | gets blocked / becomes clear / both |
| Photo mode | yes | fast (~40 ms) or full resolution (~1–2 s, misses movers) |
| Camera N extra wait | yes | per-camera offset, if they sit at different points |
| Object length | yes | lets the app work out belt speed |

Note: if "ignore blocks shorter than" is larger than the wait time, the
effective wait is the larger of the two — the object has to be validated before
it's worth a shot. The Capture tab spells out the real figure underneath the
slider.

## Limits and gotchas

- **Full res misses moving objects.** The sensor switches modes (~1–2 s per
  camera) and freezes preview. Fast grab off the live stream is the only mode
  that catches a moving item. Full res is for stationary tests and manual shots.
- **The IMX477 is manual focus.** No software can move the lens; the focus meter
  only tells you when you've got it right by hand.
- **Auto exposure and a moving belt don't mix.** See Freeze motion above.
- Capture auto-disarms below the free-space floor (default 500 MB) rather than
  filling the card mid-run.
- Flask's development server is correct for one operator on a LAN. Don't put the
  MJPEG stream behind waitress — it buffers and breaks streaming.

## If something's wrong

| Symptom | Try |
|---|---|
| One camera missing | `rpicam-hello --list-cameras`; reseat the ribbon (power off first) |
| "Sensor not detected" | `sudo apt install python3-gpiozero`; check the pin under Advanced |
| Light is red when the beam is clear | Advanced → Beam sensor → `invert` |
| One item, many photos | Advanced → raise "minimum gap"; raise debounce if the timeline looks ragged |
| Blurred items | Cameras tab → **Freeze motion** |
| Nothing captured | Check the status bar says **Capturing** — press Start on the Capture tab |
