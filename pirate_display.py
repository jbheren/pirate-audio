#!/usr/bin/env python3
"""Pirate Audio HAT <-> go-librespot (Spotify Connect)

- Affiche la pochette Spotify sur l'écran 240x240 (ST7789)
- Titre / artiste en bandeau, icône pause, barre de volume
- Boutons : A = lecture/pause, B = volume -, X = suivant (appui long = précédent), Y = volume +

Aucune clé API Spotify n'est nécessaire : l'URL de la pochette est fournie
par go-librespot via son websocket /events.
"""
import io
import json
import os
import threading
import time

import requests
import websocket
from gpiozero.pins.native import NativeFactory
from PIL import Image, ImageDraw, ImageFont
from st7789 import ST7789

API = os.environ.get("GLR_API", "http://localhost:3678")
WS = API.replace("http", "ws", 1) + "/events"
PIN_Y = int(os.environ.get("PIRATE_Y_PIN", "24"))  # 24 sur les cartes récentes, 20 sur les anciennes
VOL_STEP = int(os.environ.get("VOL_STEP", "5"))
SLEEP_AFTER = int(os.environ.get("SLEEP_AFTER", "300"))  # secondes avant extinction écran au repos

SIZE = 240
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


F_TITLE, F_ARTIST, F_BIG = font(FONT_B, 18), font(FONT, 15), font(FONT_B, 22)

# Accès GPIO direct par /dev/gpiomem (gpiozero "native").
# Sur Raspberry Pi OS Trixie 32 bits / Pi Zero W, la réservation de broches via
# gpiod ou lgpio échoue (EINVAL) : on pilote donc les registres directement.
GPIO = NativeFactory()


class _OutPin:
    """Broche de sortie compatible st7789 (tuple (objet, offset) + set_value)."""

    def __init__(self, n):
        self.pin = GPIO.pin(n)
        self.pin.function = "output"

    def set_value(self, _offset, value):
        # st7789 passe gpiod.line.Value.ACTIVE / INACTIVE (ACTIVE est "truthy")
        self.pin.state = 1 if int(getattr(value, "value", value)) else 0


disp = ST7789(port=0, cs=1, dc=(_OutPin(9), 9), backlight=(_OutPin(13), 13),
              rotation=90, spi_speed_hz=80_000_000)
disp.begin()

lock = threading.Lock()
wake = threading.Event()
state = {
    "active": False, "paused": False, "name": "", "artists": "", "cover_url": None,
    "cover": None, "volume": 0, "volume_max": 100, "vol_until": 0.0, "last_activity": time.time(),
}


def touch():
    state["last_activity"] = time.time()
    wake.set()


# ---------------------------------------------------------------- pochette
_cover_cache = {}


def load_cover(url):
    if not url:
        return None
    if url in _cover_cache:
        return _cover_cache[url]
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)
    except Exception as e:  # réseau, format…
        print("pochette KO:", e, flush=True)
        return None
    if len(_cover_cache) > 20:
        _cover_cache.clear()
    _cover_cache[url] = img
    return img


# ---------------------------------------------------------------- rendu
def fit(draw, text, fnt, width):
    if draw.textlength(text, font=fnt) <= width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text + "…"


def render():
    with lock:
        s = dict(state)
    if not s["active"]:
        img = Image.new("RGB", (SIZE, SIZE), (15, 15, 20))
        d = ImageDraw.Draw(img)
        d.text((SIZE // 2, 105), "Pirate Audio", font=F_BIG, fill=(230, 230, 230), anchor="mm")
        d.text((SIZE // 2, 140), "En attente de Spotify…", font=F_ARTIST, fill=(140, 140, 150), anchor="mm")
        return img

    img = s["cover"].copy() if s["cover"] else Image.new("RGB", (SIZE, SIZE), (30, 30, 40))
    over = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)

    # bandeau titre / artiste
    d.rectangle((0, SIZE - 52, SIZE, SIZE), fill=(0, 0, 0, 160))
    d.text((8, SIZE - 47), fit(d, s["name"], F_TITLE, SIZE - 16), font=F_TITLE, fill=(255, 255, 255, 255))
    d.text((8, SIZE - 24), fit(d, s["artists"], F_ARTIST, SIZE - 16), font=F_ARTIST, fill=(200, 200, 200, 255))

    if s["paused"]:
        c = SIZE // 2
        d.ellipse((c - 34, c - 60, c + 34, c + 8), fill=(0, 0, 0, 150))
        d.rectangle((c - 15, c - 44, c - 5, c - 8), fill=(255, 255, 255, 230))
        d.rectangle((c + 5, c - 44, c + 15, c - 8), fill=(255, 255, 255, 230))

    if time.time() < s["vol_until"]:
        frac = s["volume"] / max(1, s["volume_max"])
        d.rectangle((20, 14, SIZE - 20, 34), fill=(0, 0, 0, 170))
        d.rectangle((24, 18, 24 + int((SIZE - 48) * frac), 30), fill=(80, 200, 120, 255))

    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    return img


def display_loop():
    backlight = True
    while True:
        wake.wait(timeout=1.0)
        wake.clear()
        with lock:
            idle = (not state["active"] or state["paused"]) and time.time() - state["last_activity"] > SLEEP_AFTER
        if idle:
            if backlight:
                disp.set_backlight(False)
                backlight = False
            continue
        if not backlight:
            disp.set_backlight(True)
            backlight = True
        disp.display(render())


# ---------------------------------------------------------------- go-librespot
def apply_track(track):
    if not track:
        return
    url = track.get("album_cover_url")
    cover = load_cover(url)
    with lock:
        state.update(active=True, name=track.get("name") or "",
                     artists=", ".join(track.get("artist_names") or []), cover_url=url, cover=cover)


def initial_status():
    try:
        r = requests.get(API + "/status", timeout=5)
        if r.status_code == 200:
            st = r.json()
            with lock:
                state.update(paused=bool(st.get("paused")), volume=st.get("volume", 0),
                             volume_max=st.get("volume_steps", 100))
            apply_track(st.get("track"))
    except Exception as e:
        print("status KO:", e, flush=True)
    touch()


def on_message(_ws, msg):
    ev = json.loads(msg)
    t, data = ev.get("type"), ev.get("data") or {}
    if t == "metadata":
        apply_track(data)
    elif t in ("playing", "will_play"):
        with lock:
            state.update(active=True, paused=False)
    elif t == "paused":
        with lock:
            state["paused"] = True
    elif t in ("inactive", "stopped"):
        with lock:
            state.update(active=False, paused=False)
    elif t == "active":
        with lock:
            state["active"] = True
    elif t == "volume":
        with lock:
            state.update(volume=data.get("value", 0), volume_max=data.get("max", 100),
                         vol_until=time.time() + 2.0)
        threading.Timer(2.1, wake.set).start()
    touch()


def ws_loop():
    while True:
        initial_status()
        ws = websocket.WebSocketApp(WS, on_message=on_message)
        ws.run_forever(ping_interval=30, ping_timeout=10)
        time.sleep(3)  # go-librespot redémarre ? on réessaie


# ---------------------------------------------------------------- boutons
def post(path, body=None):
    try:
        requests.post(API + path, json=body, timeout=5)
    except Exception as e:
        print("API KO:", path, e, flush=True)


def x_held():
    touch()
    post("/player/prev")


def x_short():
    touch()
    post("/player/next")


# broche -> (action appui court, action appui long ou None)
BUTTONS = {
    5: (lambda: (touch(), post("/player/playpause")), None),                                          # A
    6: (lambda: (touch(), post("/player/volume", {"volume": -VOL_STEP, "relative": True})), None),    # B
    16: (x_short, x_held),                                                                            # X
    PIN_Y: (lambda: (touch(), post("/player/volume", {"volume": VOL_STEP, "relative": True})), None),  # Y
}
HOLD = 0.8


def buttons_loop():
    """Lecture des boutons par scrutation (20 ms) : pas besoin d'interruptions GPIO."""
    pins = {}
    for n in BUTTONS:
        p = GPIO.pin(n)
        p.function = "input"
        p.pull = "up"
        pins[n] = p
    down_since = {n: None for n in BUTTONS}
    held_done = {n: False for n in BUTTONS}
    while True:
        now = time.time()
        for n, p in pins.items():
            short, long_ = BUTTONS[n]
            pressed = not p.state  # actif à l'état bas
            if pressed and down_since[n] is None:
                down_since[n] = now
                held_done[n] = False
                if long_ is None:
                    threading.Thread(target=short, daemon=True).start()
            elif pressed and long_ and not held_done[n] and now - down_since[n] >= HOLD:
                held_done[n] = True
                threading.Thread(target=long_, daemon=True).start()
            elif not pressed and down_since[n] is not None:
                if long_ and not held_done[n]:
                    threading.Thread(target=short, daemon=True).start()
                down_since[n] = None
        time.sleep(0.02)


if __name__ == "__main__":
    threading.Thread(target=ws_loop, daemon=True).start()
    threading.Thread(target=buttons_loop, daemon=True).start()
    display_loop()
