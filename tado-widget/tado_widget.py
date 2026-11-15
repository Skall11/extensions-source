#!/usr/bin/env python3
"""
Tado Widget - widget de bureau pour tado (tado X et V3/V3+).

  - Températures, humidité, consigne, chauffe, prochain changement, programme.
  - Réglage de la consigne (durée : prochain créneau / 1 h / 3 h / permanent),
    reprise du programme, édition du programme hebdo (tado X), actions rapides.
  - Fenêtre sans bordure déplaçable, icône dans la barre système, lancement
    au démarrage de Windows.

API web non officielle de tado :
  - Auth : OAuth2 "device code" sur login.tado.com (aucun mot de passe stocké).
  - Données : hops.tado.com (tado X) ou my.tado.com/api/v2 (V3/V3+), détecté
    automatiquement.

Usage :
  TadoWidget.exe / pythonw tado_widget.py
  --interval 60   rafraîchissement en secondes (défaut 1200)
  --demo          données fictives, sans compte
  --logout        oublie la connexion
"""
from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk

# --------------------------------------------------------------------------- #
# Constantes API
# --------------------------------------------------------------------------- #
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"  # client public de l'appli web tado
DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API = "https://my.tado.com/api/v2"
HOPS = "https://hops.tado.com"  # API tado X

CONF_DIR = Path.home() / ".tado_widget"
TOKEN_FILE = CONF_DIR / "token.json"
CONFIG_FILE = CONF_DIR / "config.json"

DAYS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
DAYS_FR_LONG = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAYS_EN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

T_MIN, T_MAX, T_STEP = 5.0, 25.0, 0.5
MIN_BLOCK = 15  # minutes
TERMINATIONS = [
    ("NEXT_TIME_BLOCK", "Jusqu'au prochain créneau", None),
    ("TIMER", "1 h", 3600),
    ("TIMER", "3 h", 10800),
    ("MANUAL", "Permanent", None),
]

# --------------------------------------------------------------------------- #
# Thème
# --------------------------------------------------------------------------- #
BG = "#17181c"
CARD = "#22242a"
CARD_HOVER = "#2b2e36"
BORDER = "#34373f"
FG = "#e8e9ec"
MUTED = "#8b8f99"
ACCENT = "#f59e0b"
COLD = "#60a5fa"
OFF = "#3a3d46"
ERR = "#f87171"
OK = "#4ade80"
SELECT = "#4b4f5a"  # fond des cases : coche visible noire ou blanche
FONT = "Segoe UI" if sys.platform == "win32" else "Helvetica"


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


class AuthRequired(Exception):
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http(method: str, url: str, form: dict | None = None, body=None, headers: dict | None = None):
    h = {"User-Agent": "tado-widget/2.0", "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif method in ("POST", "PUT"):
        data = b""
    req = urllib.request.Request(url, data=data, method=method, headers=h)

    def decode(raw: bytes):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw.decode(errors="replace")

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, decode(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, decode(e.read()), e.headers


# --------------------------------------------------------------------------- #
# Auth (device code flow + refresh token rotatif)
# --------------------------------------------------------------------------- #
class TadoAuth:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.tok: dict = json.loads(TOKEN_FILE.read_text())
        except Exception:
            self.tok = {}

    def has_refresh(self) -> bool:
        return bool(self.tok.get("refresh_token"))

    def _store(self, data: dict):
        data["expires_at"] = time.time() + int(data.get("expires_in", 600)) - 30
        self.tok = data
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(data))
        try:
            TOKEN_FILE.chmod(0o600)
        except Exception:
            pass

    def logout(self):
        self.tok = {}
        TOKEN_FILE.unlink(missing_ok=True)

    def start_device_login(self) -> dict:
        st, data, _ = http("POST", DEVICE_AUTH_URL, {"client_id": CLIENT_ID, "scope": "offline_access"})
        if st != 200 or not isinstance(data, dict):
            raise ApiError(st, str(data))
        return data

    def poll_device_login(self, dev: dict, cancel: threading.Event) -> bool:
        interval = int(dev.get("interval", 5))
        deadline = time.time() + int(dev.get("expires_in", 300))
        while time.time() < deadline and not cancel.is_set():
            cancel.wait(interval)
            st, data, _ = http("POST", TOKEN_URL, {
                "client_id": CLIENT_ID,
                "device_code": dev["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if st == 200 and isinstance(data, dict) and "access_token" in data:
                with self.lock:
                    self._store(data)
                return True
            err = data.get("error") if isinstance(data, dict) else None
            if err == "slow_down":
                interval += 5
            elif err not in ("authorization_pending", None):
                raise ApiError(st, str(data))
        return False

    def access_token(self) -> str:
        with self.lock:
            if self.tok.get("access_token") and time.time() < self.tok.get("expires_at", 0):
                return self.tok["access_token"]
            if not self.has_refresh():
                raise AuthRequired()
            st, data, _ = http("POST", TOKEN_URL, {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.tok["refresh_token"],
            })
            if st == 200 and isinstance(data, dict) and "access_token" in data:
                self._store(data)  # refresh token rotatif : on garde le nouveau
                return data["access_token"]
            if st in (400, 401):
                self.logout()
                raise AuthRequired()
            raise ApiError(st, str(data))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def hm_to_min(s: str, is_end: bool = False) -> int:
    h, m = (int(x) for x in str(s).split(":")[:2])
    v = h * 60 + m
    return 1440 if (is_end and v == 0) or v >= 1440 else v


def min_to_hm(m: int) -> str:
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


def temp_of(setting: dict) -> float | None:
    t = setting.get("temperature") or {}
    v = t.get("value", t.get("celsius"))
    return float(v) if v is not None else None


def flatten_x_schedule(raw) -> list[dict]:
    """Le GET programme tado X peut être plat ou imbriqué (dayType + daySchedule)."""
    items = raw if isinstance(raw, list) else (raw or {}).get("schedule") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if isinstance(it.get("daySchedule"), list):
            for b in it["daySchedule"]:
                out.append({**b, "dayType": b.get("dayType") or it.get("dayType")})
        else:
            out.append(it)
    return out


def build_week(blocks: list[dict]) -> list[list[dict]]:
    """Blocs tado -> 7 listes (Lun..Dim) de {start, end, on, temp} en minutes."""
    by_type: dict[str, list[dict]] = {}
    for b in blocks:
        s = b.get("setting") or {}
        on = s.get("power") == "ON"
        by_type.setdefault(b.get("dayType", ""), []).append({
            "start": hm_to_min(b.get("start", "00:00")),
            "end": hm_to_min(b.get("end", "00:00"), is_end=True),
            "on": on,
            "temp": temp_of(s) if on else None,
        })
    week = []
    for i, day in enumerate(DAYS_EN):
        cands = [day] + (["MONDAY_TO_FRIDAY"] if i < 5 else []) + ["MONDAY_TO_SUNDAY"]
        week.append(sorted(next((by_type[c] for c in cands if c in by_type), []), key=lambda x: x["start"]))
    return week


def clamp_t(t: float) -> float:
    return max(T_MIN, min(T_MAX, round(t * 2) / 2))


# --------------------------------------------------------------------------- #
# Client API
# --------------------------------------------------------------------------- #
class TadoClient:
    def __init__(self, auth: TadoAuth, home_id: int | None = None):
        self.auth = auth
        self.want_home = home_id
        self.home_id: int | None = None
        self.home_name = ""
        self.is_x = False
        self.zones: list[dict] = []
        self.quota_left: int | None = None

    @property
    def can_edit_schedule(self) -> bool:
        return self.is_x

    @property
    def has_quick_actions(self) -> bool:
        return self.is_x

    def request(self, method: str, path: str, base: str = API, body=None):
        for attempt in (0, 1):
            tok = self.auth.access_token()
            st, data, hdrs = http(method, base + path, body=body, headers={"Authorization": f"Bearer {tok}"})
            rl = hdrs.get("ratelimit") if hdrs else None
            if rl:
                m = re.search(r"r=(\d+)", rl)
                if m:
                    self.quota_left = int(m.group(1))
            if st == 401 and attempt == 0:
                self.auth.tok["expires_at"] = 0  # force refresh puis réessaie
                continue
            if st >= 400:
                raise ApiError(st, str(data)[:200])
            return data
        raise ApiError(401, "unauthorized")

    def get(self, path: str, base: str = API):
        return self.request("GET", path, base)

    def init(self):
        me = self.get("/me")
        homes = me.get("homes") or []
        if not homes:
            raise ApiError(404, "aucun logement sur ce compte")
        home = next((h for h in homes if h["id"] == self.want_home), homes[0])
        self.home_id = home["id"]
        self.home_name = home.get("name", "")
        info = self.get(f"/homes/{self.home_id}")
        self.is_x = info.get("generation") == "LINE_X"
        if not self.is_x:
            self.zones = [{"id": z["id"], "name": z["name"], "type": z.get("type")}
                          for z in self.get(f"/homes/{self.home_id}/zones")]

    # -- lecture -------------------------------------------------------------
    def states(self) -> list[dict]:
        return self._states_x() if self.is_x else self._states_v3()

    def _states_v3(self) -> list[dict]:
        zs = (self.get(f"/homes/{self.home_id}/zoneStates") or {}).get("zoneStates", {})
        out = []
        for z in self.zones:
            st = zs.get(str(z["id"]))
            if st is None:
                continue
            sens = st.get("sensorDataPoints") or {}
            setting = st.get("setting") or {}
            nxt = st.get("nextScheduleChange") or {}
            nset = nxt.get("setting") or {}
            overlay = st.get("overlay")
            out.append({
                "id": z["id"], "name": z["name"], "type": z.get("type"),
                "temp": (sens.get("insideTemperature") or {}).get("celsius"),
                "hum": (sens.get("humidity") or {}).get("percentage"),
                "on": setting.get("power") == "ON",
                "target": temp_of(setting),
                "heat": ((st.get("activityDataPoints") or {}).get("heatingPower") or {}).get("percentage"),
                "manual": overlay is not None,
                "manual_until": parse_iso(((overlay or {}).get("termination") or {}).get("projectedExpiry")),
                "boost": False,
                "away": st.get("tadoMode") == "AWAY",
                "window": bool(st.get("openWindow") or st.get("openWindowDetected")),
                "offline": (st.get("link") or {}).get("state") == "OFFLINE",
                "next_at": parse_iso(nxt.get("start")),
                "next_temp": temp_of(nset) if nset.get("power") == "ON" else None,
                "next_on": nset.get("power") == "ON",
            })
        return out

    def _states_x(self) -> list[dict]:
        rooms = self.get(f"/homes/{self.home_id}/rooms", base=HOPS) or []
        out = []
        for r in rooms:
            sens = r.get("sensorDataPoints") or {}
            setting = r.get("setting") or {}
            nxt = r.get("nextScheduleChange") or {}
            nset = nxt.get("setting") or {}
            mct = r.get("manualControlTermination")
            out.append({
                "id": r.get("id"), "name": r.get("name", "?"), "type": "HEATING",
                "temp": (sens.get("insideTemperature") or {}).get("value"),
                "hum": (sens.get("humidity") or {}).get("percentage"),
                "on": setting.get("power") == "ON",
                "target": temp_of(setting),
                "heat": (r.get("heatingPower") or {}).get("percentage"),
                "manual": mct is not None,
                "manual_until": parse_iso((mct or {}).get("projectedExpiry")),
                "boost": r.get("boostMode") is not None,
                "away": False,
                "window": bool((r.get("openWindow") or {}).get("activated")),
                "offline": (r.get("connection") or {}).get("state") == "DISCONNECTED",
                "next_at": parse_iso(nxt.get("start")),
                "next_temp": temp_of(nset) if nset.get("power") == "ON" else None,
                "next_on": nset.get("power") == "ON",
            })
        return out

    def weather(self) -> float | None:
        w = self.get(f"/homes/{self.home_id}/weather")
        return ((w or {}).get("outsideTemperature") or {}).get("celsius")

    def schedule(self, zone_id) -> list[list[dict]]:
        if self.is_x:
            raw = self.get(f"/homes/{self.home_id}/rooms/{zone_id}/schedule", base=HOPS)
            return build_week(flatten_x_schedule(raw))
        tt = self.get(f"/homes/{self.home_id}/zones/{zone_id}/schedule/activeTimetable")
        blocks = self.get(f"/homes/{self.home_id}/zones/{zone_id}/schedule/timetables/{tt['id']}/blocks")
        return build_week(blocks or [])

    # -- écriture ------------------------------------------------------------
    def set_room(self, zone_id, power: bool, temp: float | None, term: str, seconds: int | None):
        if self.is_x:
            setting = {"power": "ON", "isBoost": False, "temperature": {"value": temp}} if power else {"power": "OFF"}
            termination = {"type": term}
            if term == "TIMER":
                termination["durationInSeconds"] = seconds
            self.request("POST", f"/homes/{self.home_id}/rooms/{zone_id}/manualControl", HOPS,
                         {"setting": setting, "termination": termination})
        else:
            setting = {"type": "HEATING", "power": "ON" if power else "OFF"}
            if power:
                setting["temperature"] = {"celsius": temp}
            termination = {"typeSkillBasedApp": term}
            if term == "TIMER":
                termination["durationInSeconds"] = seconds
            self.request("PUT", f"/homes/{self.home_id}/zones/{zone_id}/overlay", API,
                         {"setting": setting, "termination": termination})

    def resume_room(self, zone_id):
        if self.is_x:
            self.request("DELETE", f"/homes/{self.home_id}/rooms/{zone_id}/manualControl", HOPS)
        else:
            self.request("DELETE", f"/homes/{self.home_id}/zones/{zone_id}/overlay", API)

    def quick(self, action: str):  # boost | allOff | resumeSchedule
        self.request("POST", f"/homes/{self.home_id}/quickActions/{action}", HOPS, {})

    def save_day(self, zone_id, day: int, blocks: list[dict]):
        """blocks : [{start, end, on, temp}] en minutes, couvrant 0..1440."""
        d = DAYS_EN[day]
        sched = []
        for b in blocks:
            s = {"power": "ON", "temperature": {"value": b["temp"]}} if b["on"] else {"power": "OFF"}
            sched.append({"start": min_to_hm(b["start"]), "end": min_to_hm(b["end"]), "dayType": d, "setting": s})
        self.request("POST", f"/homes/{self.home_id}/rooms/{zone_id}/schedule", HOPS,
                     {"dayType": d, "daySchedule": sched})


# --------------------------------------------------------------------------- #
# Mode démo (écritures simulées en mémoire)
# --------------------------------------------------------------------------- #
class DemoClient:
    home_name = "Maison (démo)"
    quota_left = 87
    is_x = True
    can_edit_schedule = True
    has_quick_actions = True

    def __init__(self):
        nxt = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
        nxt = nxt.replace(hour=(nxt.hour + 2) % 24)
        base = dict(type="HEATING", manual=False, manual_until=None, boost=False, away=False, window=False,
                    offline=False, next_at=nxt, next_on=True)
        self.rooms = [
            dict(base, id=1, name="Salon", temp=20.6, hum=48, on=True, target=21.0, heat=35, next_temp=18.0),
            dict(base, id=2, name="Chambre", temp=18.1, hum=55, on=True, target=17.0, heat=0, next_temp=19.0),
            dict(base, id=3, name="Bureau", temp=19.4, hum=44, on=False, target=None, heat=0, next_temp=20.0,
                 window=True),
            dict(base, id=4, name="Salle de bain", temp=21.9, hum=67, on=True, target=22.0, heat=80,
                 next_at=None, next_temp=None, next_on=False),
        ]
        self.saved: dict = {}
        wk = [(0, 390, 17.0), (390, 510, 20.5), (510, 1050, 18.0), (1050, 1350, 21.0), (1350, 1440, 17.0)]
        we = [(0, 480, 17.0), (480, 1380, 21.0), (1380, 1440, None)]
        mk = lambda l: [{"start": a, "end": b, "on": t is not None, "temp": t} for a, b, t in l]
        self.week = [mk(wk)] * 5 + [mk(we)] * 2

    def init(self):
        time.sleep(0.2)

    def states(self):
        return copy.deepcopy(self.rooms)

    def weather(self):
        return 7.5

    def schedule(self, zone_id):
        return copy.deepcopy(self.saved.get(zone_id, self.week))

    def _room(self, zid):
        return next(r for r in self.rooms if r["id"] == zid)

    def set_room(self, zone_id, power, temp, term, seconds):
        time.sleep(0.3)
        r = self._room(zone_id)
        r.update(on=power, target=temp if power else None, manual=True, boost=False,
                 manual_until=None if term == "MANUAL" else r["next_at"])
        r["heat"] = 50 if power and temp and temp > r["temp"] else 0

    def resume_room(self, zone_id):
        time.sleep(0.3)
        self._room(zone_id).update(manual=False, manual_until=None, boost=False, on=True, target=19.0)

    def quick(self, action):
        time.sleep(0.3)
        for r in self.rooms:
            if action == "boost":
                r.update(on=True, target=25.0, boost=True, heat=100)
            elif action == "allOff":
                r.update(on=False, target=None, manual=True, heat=0)
            else:
                r.update(manual=False, boost=False, on=True, target=19.0)

    def save_day(self, zone_id, day, blocks):
        time.sleep(0.2)
        wk = self.saved.setdefault(zone_id, copy.deepcopy(self.week))
        wk[day] = copy.deepcopy(blocks)


# --------------------------------------------------------------------------- #
# Icône (barre système + .exe)
# --------------------------------------------------------------------------- #
def make_icon(size: int = 64):
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=(245, 158, 11, 255))
    # thermomètre stylisé
    w = max(2, s // 9)
    cx = s // 2
    d.rounded_rectangle((cx - w, s * 0.16, cx + w, s * 0.64), radius=w, fill=(255, 255, 255, 255))
    r = s * 0.17
    d.ellipse((cx - r, s * 0.80 - r, cx + r, s * 0.80 + r), fill=(255, 255, 255, 255))
    return img


# --------------------------------------------------------------------------- #
# Démarrage auto Windows
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "TadoWidget"


def autostart_cmd() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def autostart_get() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def autostart_set(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def temp_color(t: float | None) -> str:
    if t is None:
        return OFF
    k = max(0.0, min(1.0, (t - 15.0) / 8.0))
    a, b = (0x3b, 0x82, 0xf6), (0xf9, 0x73, 0x16)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def fmt_t(t) -> str:
    return "--" if t is None else f"{t:.1f}°"


def button(parent, text, cmd, accent=False, small=False, bg=None, size=None):
    base_bg = bg or (ACCENT if accent else CARD)
    hover = "#fbbf24" if accent else CARD_HOVER
    b = tk.Label(parent, text=text, bg=base_bg, fg="#111" if accent else FG, cursor="hand2",
                 font=(FONT, size or (8 if small else 9), "bold" if accent else "normal"),
                 padx=6 if small else 12, pady=2 if small else 5)
    b._enabled = True

    def click(_e):
        if b._enabled:
            cmd()
    b.bind("<Button-1>", click)
    b.bind("<Enter>", lambda e: b._enabled and b.configure(bg=hover))
    b.bind("<Leave>", lambda e: b.configure(bg=base_bg))

    def set_enabled(on: bool):
        b._enabled = on
        b.configure(fg=("#111" if accent else FG) if on else MUTED, cursor="hand2" if on else "arrow")
    b.set_enabled = set_enabled
    return b


def style_spin(sb):
    sb.configure(bg=CARD, fg=FG, buttonbackground=CARD, insertbackground=FG, relief="flat",
                 readonlybackground=CARD, highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT, font=(FONT, 9), justify="center")


def draw_week(cv: tk.Canvas, week, width=560, on_click=None):
    cv.delete("all")
    LBL, ROW, GAP, TOP = 40, 26, 6, 22
    H = TOP + 7 * (ROW + GAP) + 4
    cv.configure(width=width + LBL + 10, height=H)
    xm = lambda m: LBL + width * m / 1440
    for h in range(0, 25, 3):
        x = xm(h * 60)
        cv.create_line(x, TOP - 4, x, H - 4, fill="#2c2f37")
        cv.create_text(x, TOP - 12, text=f"{h:02d}h", fill=MUTED, font=(FONT, 7))
    today = datetime.now().weekday()
    for i, blocks in enumerate(week):
        y = TOP + i * (ROW + GAP)
        tag = f"day{i}"
        cv.create_rectangle(0, y - 2, LBL + width + 10, y + ROW + 2, fill=BG, outline="", tags=tag)
        cv.create_text(LBL - 8, y + ROW / 2, text=DAYS_FR[i], anchor="e", tags=tag,
                       fill=ACCENT if i == today else FG, font=(FONT, 9, "bold" if i == today else "normal"))
        if not blocks:
            cv.create_rectangle(xm(0), y, xm(1440), y + ROW, fill=OFF, outline="", tags=tag)
        for b in blocks:
            a, e = b["start"], b["end"]
            cv.create_rectangle(xm(a) + 1, y, xm(e) - 1, y + ROW, outline="", tags=tag,
                                fill=temp_color(b["temp"]) if b["on"] else OFF)
            if xm(e) - xm(a) > 34:
                cv.create_text((xm(a) + xm(e)) / 2, y + ROW / 2, fill="white", font=(FONT, 8, "bold"),
                               text=fmt_t(b["temp"]) if b["on"] else "off", tags=tag)
        if on_click:
            cv.tag_bind(tag, "<Button-1>", lambda e, d=i: on_click(d))
            cv.tag_bind(tag, "<Enter>", lambda e: cv.configure(cursor="hand2"))
            cv.tag_bind(tag, "<Leave>", lambda e: cv.configure(cursor=""))
    now = datetime.now()
    y = TOP + today * (ROW + GAP)
    xn = xm(now.hour * 60 + now.minute)
    cv.create_line(xn, y - 3, xn, y + ROW + 3, fill="white", width=2)


# --------------------------------------------------------------------------- #
# Widget principal
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, client, auth: TadoAuth | None, interval: int):
        self.root = root
        self.client = client
        self.auth = auth
        self.interval = max(30, interval)
        self.q: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        self.cancel_login = threading.Event()
        self.stop = threading.Event()
        self.sched_cache: dict = {}
        self.states: list[dict] = []
        self.room_windows: dict = {}
        self.outside: float | None = None
        self.ready = False
        self.tray = None
        self.cfg = self._load_cfg()

        root.title("tado°")
        root.configure(bg=BORDER)
        root.overrideredirect(True)  # pas de barre de titre : vrai widget
        root.attributes("-topmost", self.cfg.get("topmost", False))
        root.attributes("-alpha", self.cfg.get("alpha", 0.96))
        self._set_icon(root)

        self.frame = tk.Frame(root, bg=BG, width=320)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(self.frame, bg=BG, cursor="fleur")
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.title_lbl = tk.Label(head, text="tado°", bg=BG, fg=FG, font=(FONT, 12, "bold"), cursor="fleur")
        self.title_lbl.pack(side="left")
        self.out_lbl = tk.Label(head, text="", bg=BG, fg=MUTED, font=(FONT, 10), cursor="fleur")
        self.out_lbl.pack(side="left", padx=8)
        self._icon_btn(head, "✕", self.close_btn).pack(side="right")
        self._icon_btn(head, "⋯", self.show_menu).pack(side="right", padx=(6, 6))
        self._icon_btn(head, "⟳", self.refresh_now).pack(side="right")
        for w in (head, self.title_lbl, self.out_lbl):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())

        self.body = tk.Frame(self.frame, bg=BG)
        self.body.pack(fill="both", expand=True, padx=8)
        tk.Label(self.body, text="Connexion…", bg=BG, fg=MUTED, font=(FONT, 10), width=36).pack(pady=14)

        self.status = tk.Label(self.frame, text="", bg=BG, fg=MUTED, font=(FONT, 8), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(2, 8))

        for w in (self.frame, self.body, self.status):
            w.bind("<Button-3>", lambda e: self.show_menu(e))
        self._build_menu()
        self._restore_pos()

        self.root.after(100, self.pump)
        threading.Thread(target=self.worker, daemon=True).start()

    # -- config / fenêtre ----------------------------------------------------
    def _load_cfg(self) -> dict:
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(self.cfg))
        except Exception:
            pass

    def _set_icon(self, win):
        try:
            from PIL import ImageTk
            self._icon_img = ImageTk.PhotoImage(make_icon(64))
            win.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _restore_pos(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = self.cfg.get("pos", [sw - 360, 60])
        x = max(0, min(x, sw - 120))
        y = max(0, min(y, sh - 80))
        self.root.geometry(f"+{x}+{y}")

    def _save_pos(self):
        self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        self._save_cfg()

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _icon_btn(self, parent, txt, cmd):
        b = tk.Label(parent, text=txt, bg=BG, fg=MUTED, font=(FONT, 11), cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd(e) if cmd == self.show_menu else cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=FG))
        b.bind("<Leave>", lambda e: b.configure(fg=MUTED))
        return b

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#111",
                    font=(FONT, 9))
        self.v_top = tk.BooleanVar(value=self.cfg.get("topmost", False))
        self.v_auto = tk.BooleanVar(value=autostart_get())
        self.v_alpha = tk.DoubleVar(value=self.cfg.get("alpha", 0.96))
        self.quick_menu = tk.Menu(m, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT,
                                  activeforeground="#111", font=(FONT, 9))
        self.quick_menu.add_command(label="🔥 Boost partout (30 min)", command=lambda: self.quick("boost"))
        self.quick_menu.add_command(label="❄ Tout éteindre (hors-gel)", command=lambda: self.quick("allOff"))
        self.quick_menu.add_command(label="↺ Reprendre le programme partout",
                                    command=lambda: self.quick("resumeSchedule"))
        m.add_cascade(label="Actions rapides", menu=self.quick_menu)
        m.add_separator()
        m.add_command(label="Rafraîchir", command=self.refresh_now)
        m.add_checkbutton(label="Toujours au premier plan", variable=self.v_top, command=self.toggle_top)
        op = tk.Menu(m, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#111")
        for a in (1.0, 0.9, 0.8, 0.7):
            op.add_radiobutton(label=f"{int(a * 100)} %", value=a, variable=self.v_alpha, command=self.set_alpha)
        m.add_cascade(label="Opacité", menu=op)
        if sys.platform == "win32":
            m.add_checkbutton(label="Lancer au démarrage de Windows", variable=self.v_auto,
                              command=self.toggle_autostart)
        m.add_separator()
        if self.auth:
            m.add_command(label="Se déconnecter", command=self.logout)
        m.add_command(label="Quitter", command=self.quit)
        self.menu = m

    def show_menu(self, e=None):
        state = "normal" if self.ready and self.client.has_quick_actions else "disabled"
        for i in range(3):
            self.quick_menu.entryconfigure(i, state=state)
        x = e.x_root if e else self.root.winfo_rootx() + 200
        y = e.y_root if e else self.root.winfo_rooty() + 30
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def toggle_top(self):
        self.cfg["topmost"] = self.v_top.get()
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._save_cfg()

    def set_alpha(self):
        self.cfg["alpha"] = self.v_alpha.get()
        self.root.attributes("-alpha", self.cfg["alpha"])
        self._save_cfg()

    def toggle_autostart(self):
        try:
            autostart_set(self.v_auto.get())
        except Exception as e:
            self.set_status(f"Démarrage auto impossible : {e}", ERR)
            self.v_auto.set(autostart_get())

    def logout(self):
        if self.auth:
            self.auth.logout()
        self.ready = False
        self.wake.set()

    def close_btn(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
        else:
            self.root.withdraw()

    def quit(self):
        self._save_pos()
        self.stop.set()
        self.cancel_login.set()
        self.wake.set()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def refresh_now(self):
        self.wake.set()

    def set_status(self, msg, color=MUTED):
        self.status.configure(text=msg, fg=color)

    # -- threads -------------------------------------------------------------
    def post(self, fn, *args):
        """Exécute fn(*args) dans le thread Tk."""
        self.q.put((fn, args))

    def run_bg(self, fn, on_ok=None, on_err=None):
        def run():
            try:
                res = fn()
                if on_ok:
                    self.post(on_ok, res)
            except AuthRequired:
                self.ready = False
                self.wake.set()
                if on_err:
                    self.post(on_err, "Session expirée, reconnexion…")
            except ApiError as e:
                msg = "Quota API tado atteint" if e.status == 429 else str(e)
                if on_err:
                    self.post(on_err, msg)
            except Exception as e:
                if on_err:
                    self.post(on_err, f"Erreur : {e}")
        threading.Thread(target=run, daemon=True).start()

    def pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(100, self.pump)

    def worker(self):
        cycle = 0
        while not self.stop.is_set():
            delay = self.interval
            try:
                if not self.ready:
                    self.client.init()
                    self.ready = True
                    self.post(self.on_home, self.client.home_name)
                states = self.client.states()
                if cycle % 3 == 0:  # météo 1 cycle sur 3 (quota)
                    try:
                        self.outside = self.client.weather()
                    except Exception:
                        pass
                cycle += 1
                self.post(self.on_states, states)
            except AuthRequired:
                self.ready = False
                self.post(self.on_login_needed)
                if not self._do_login():
                    return
                continue
            except ApiError as e:
                if e.status == 429:
                    delay = max(self.interval, 3600)
                    self.post(self.set_status, "Quota API tado atteint – réessai dans 1 h", ERR)
                else:
                    self.post(self.set_status, str(e), ERR)
                    delay = min(self.interval, 120)
            except Exception as e:
                self.post(self.set_status, f"Erreur : {e}", ERR)
                delay = min(self.interval, 120)
            self.wake.wait(delay)
            self.wake.clear()

    def _do_login(self) -> bool:
        try:
            dev = self.auth.start_device_login()
        except Exception as e:
            self.post(self.set_status, f"Login impossible : {e}", ERR)
            return False
        self.post(self.on_code, dev)
        url = dev.get("verification_uri_complete") or dev.get("verification_uri")
        if url:
            webbrowser.open(url)
        try:
            ok = self.auth.poll_device_login(dev, self.cancel_login)
        except Exception as e:
            self.post(self.set_status, f"Login refusé : {e}", ERR)
            return False
        if not ok and not self.stop.is_set():
            self.post(self.set_status, "Code expiré – relance le widget", ERR)
        return ok

    # -- actions -------------------------------------------------------------
    def quick(self, action):
        labels = {"boost": "Boost", "allOff": "Tout éteint", "resumeSchedule": "Programme repris"}
        self.set_status("Envoi…")
        self.run_bg(lambda: self.client.quick(action),
                    lambda _: (self.set_status(f"{labels[action]} ✓", OK), self.refresh_now()),
                    lambda m: self.set_status(m, ERR))

    def get_schedule(self, zone_id, cb, err):
        if zone_id in self.sched_cache:
            cb(self.sched_cache[zone_id])
            return

        def done(wk):
            self.sched_cache[zone_id] = wk
            cb(wk)
        self.run_bg(lambda: self.client.schedule(zone_id), done, err)

    def open_room(self, z):
        w = self.room_windows.get(z["id"])
        if w and w.winfo_exists():
            w.deiconify()
            w.lift()
            return
        self.room_windows[z["id"]] = RoomWindow(self, z)

    # -- callbacks UI --------------------------------------------------------
    def on_home(self, name):
        self.title_lbl.configure(text=name or "tado°")

    def on_login_needed(self):
        self._clear()
        tk.Label(self.body, text="Connexion à tado requise…", bg=BG, fg=FG, font=(FONT, 10),
                 width=36).pack(pady=10)

    def on_code(self, dev):
        self._clear()
        url = dev.get("verification_uri_complete") or dev.get("verification_uri", "")
        tk.Label(self.body, text="Valide la connexion dans ton navigateur.\nCode :", width=36,
                 bg=BG, fg=FG, font=(FONT, 10), justify="center").pack(pady=(10, 2))
        tk.Label(self.body, text=dev.get("user_code", ""), bg=BG, fg=ACCENT, font=(FONT, 20, "bold")).pack()
        link = tk.Label(self.body, text="Ouvrir la page de connexion", bg=BG, fg=COLD,
                        font=(FONT, 9, "underline"), cursor="hand2")
        link.pack(pady=(4, 10))
        link.bind("<Button-1>", lambda e: webbrowser.open(url))
        self.set_status("En attente de validation…")

    def on_states(self, states):
        self.states = states
        self.out_lbl.configure(text=f"Ext. {fmt_t(self.outside)}" if self.outside is not None else "")
        self._clear()
        for z in states:
            self._zone_card(z)
            w = self.room_windows.get(z["id"])
            if w and w.winfo_exists():
                w.update_zone(z)
        txt = f"Mis à jour {datetime.now():%H:%M}"
        if self.client.quota_left is not None:
            txt += f"  ·  quota restant {self.client.quota_left}"
        self.set_status(txt + "  ·  clic = régler")

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _zone_card(self, z):
        card = tk.Frame(self.body, bg=CARD, cursor="hand2")
        card.pack(fill="x", pady=3)
        heating = (z.get("heat") or 0) > 0

        right = tk.Frame(card, bg=CARD)
        right.pack(side="right", padx=10, pady=6)
        left = tk.Frame(card, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=10, pady=6)

        tk.Label(left, text=z["name"], bg=CARD, fg=FG, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
        bits = []
        if z.get("offline"):
            bits.append("⚠ hors ligne")
        if z.get("window"):
            bits.append("fenêtre ouverte")
        if z.get("away"):
            bits.append("absent")
        if z.get("boost"):
            bits.append("boost")
        elif z.get("manual"):
            bits.append("manuel" + (f" → {z['manual_until']:%H:%M}" if z.get("manual_until") else ""))
        else:
            bits.append("auto")
            if z.get("next_at"):
                nt = fmt_t(z["next_temp"]) if z.get("next_on") else "off"
                bits.append(f"{nt} à {z['next_at']:%H:%M}")
        tk.Label(left, text="  ·  ".join(bits), bg=CARD, fg=ACCENT if z.get("manual") or z.get("boost") else MUTED,
                 font=(FONT, 8), anchor="w").pack(fill="x")

        if z.get("type") == "HOT_WATER":
            tk.Label(right, text=f"💧 {'ON' if z.get('on') else 'OFF'}", bg=CARD, fg=FG,
                     font=(FONT, 14, "bold")).pack(anchor="e")
        else:
            tk.Label(right, text=fmt_t(z.get("temp")), bg=CARD, fg=ACCENT if heating else FG,
                     font=(FONT, 18, "bold")).pack(anchor="e")
            sub = f"→ {fmt_t(z['target'])}" if z.get("on") else "off"
            if heating:
                sub += f" · 🔥{z['heat']:.0f}%"
            if z.get("hum") is not None:
                sub += f" · 💧{z['hum']:.0f}%"
            tk.Label(right, text=sub, bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="e")

        def paint(w, col):
            try:
                w.configure(bg=col)
            except tk.TclError:
                pass
            for c in w.winfo_children():
                paint(c, col)

        def bind_all(w):
            w.bind("<Button-1>", lambda e: self.open_room(z))
            w.bind("<Button-3>", lambda e: self.show_menu(e))
            w.bind("<Enter>", lambda e: paint(card, CARD_HOVER))
            w.bind("<Leave>", lambda e: paint(card, CARD))
            for c in w.winfo_children():
                bind_all(c)
        bind_all(card)


# --------------------------------------------------------------------------- #
# Fenêtre pièce : réglage + programme
# --------------------------------------------------------------------------- #
class RoomWindow(tk.Toplevel):
    def __init__(self, app: App, zone: dict):
        super().__init__(app.root, bg=BG)
        self.app, self.zone = app, zone
        self.title(f"{zone['name']} – tado°")
        self.resizable(False, False)
        self.attributes("-topmost", app.cfg.get("topmost", False))
        self.week = None

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(top, text=zone["name"], bg=BG, fg=FG, font=(FONT, 14, "bold")).pack(side="left")
        self.now_lbl = tk.Label(top, text="", bg=BG, fg=MUTED, font=(FONT, 10))
        self.now_lbl.pack(side="right")

        # --- réglage consigne
        ctl = tk.Frame(self, bg=CARD)
        ctl.pack(fill="x", padx=16, pady=6)
        row = tk.Frame(ctl, bg=CARD)
        row.pack(fill="x", padx=12, pady=(10, 4))
        self.power = bool(zone.get("on"))
        self.target = clamp_t(zone.get("target") or zone.get("next_temp") or 20.0)
        button(row, "−", lambda: self.bump(-T_STEP), size=16).pack(side="left")
        self.t_lbl = tk.Label(row, text="", bg=CARD, fg=FG, font=(FONT, 22, "bold"), width=6)
        self.t_lbl.pack(side="left", padx=4)
        button(row, "+", lambda: self.bump(T_STEP), size=16).pack(side="left")
        self.pw_btn = button(row, "", self.toggle_power)
        self.pw_btn.pack(side="right")

        tk.Label(ctl, text="Durée", bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=12)
        terms = tk.Frame(ctl, bg=CARD)
        terms.pack(fill="x", padx=8, pady=(0, 6))
        self.term = tk.IntVar(value=0)
        for i, (_t, label, _s) in enumerate(TERMINATIONS):
            tk.Radiobutton(terms, text=label, value=i, variable=self.term, bg=CARD, fg=FG, selectcolor=SELECT,
                           activebackground=CARD, activeforeground=FG, font=(FONT, 9), bd=0,
                           highlightthickness=0).pack(side="left", padx=4)

        acts = tk.Frame(ctl, bg=CARD)
        acts.pack(fill="x", padx=12, pady=(2, 12))
        self.apply_btn = button(acts, "Appliquer", self.apply, accent=True)
        self.apply_btn.pack(side="left")
        self.resume_btn = button(acts, "↺ Reprendre le programme", self.resume, bg=BG)
        self.resume_btn.pack(side="left", padx=8)
        self.msg = tk.Label(ctl, text="", bg=CARD, fg=MUTED, font=(FONT, 8))
        self.msg.pack(anchor="w", padx=12, pady=(0, 8))

        # --- programme
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(hdr, text="Programme", bg=BG, fg=FG, font=(FONT, 10, "bold")).pack(side="left")
        hint = "clic sur un jour pour le modifier" if app.client.can_edit_schedule else "lecture seule (V3)"
        tk.Label(hdr, text=hint, bg=BG, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=8)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, width=610, height=60)
        self.cv.pack(padx=12, pady=(4, 14))
        self.cv.create_text(305, 30, text="Chargement…", fill=MUTED, font=(FONT, 9))

        self.update_zone(zone)
        self._refresh_ctl()
        self.reload_schedule()

    def reload_schedule(self):
        def err(m):
            if self.winfo_exists():
                self.cv.delete("all")
                self.cv.create_text(305, 30, text=f"Programme indisponible : {m}", fill=ERR, font=(FONT, 9))
        self.app.get_schedule(self.zone["id"], self.show_week, err)

    def show_week(self, wk):
        if not self.winfo_exists():
            return
        self.week = wk
        draw_week(self.cv, wk, on_click=self.edit_day if self.app.client.can_edit_schedule else None)

    def update_zone(self, z):
        self.zone = z
        txt = f"actuel {fmt_t(z.get('temp'))}"
        if z.get("hum") is not None:
            txt += f"  ·  💧{z['hum']:.0f}%"
        if z.get("heat"):
            txt += f"  ·  🔥{z['heat']:.0f}%"
        self.now_lbl.configure(text=txt)
        self.resume_btn.set_enabled(bool(z.get("manual") or z.get("boost")))

    def _refresh_ctl(self):
        self.t_lbl.configure(text=fmt_t(self.target) if self.power else "OFF",
                             fg=temp_color(self.target) if self.power else MUTED)
        self.pw_btn.configure(text="Chauffage ON" if self.power else "Chauffage OFF")

    def bump(self, d):
        if not self.power:
            self.power = True
        self.target = clamp_t(self.target + d)
        self._refresh_ctl()

    def toggle_power(self):
        self.power = not self.power
        self._refresh_ctl()

    def _busy(self, msg):
        self.msg.configure(text=msg, fg=MUTED)
        self.apply_btn.set_enabled(False)

    def _done(self, msg, color=OK):
        if not self.winfo_exists():
            return
        self.msg.configure(text=msg, fg=color)
        self.apply_btn.set_enabled(True)

    def apply(self):
        term, label, secs = TERMINATIONS[self.term.get()]
        zid, power, target = self.zone["id"], self.power, self.target
        self._busy("Envoi…")
        self.app.run_bg(lambda: self.app.client.set_room(zid, power, target, term, secs),
                        lambda _: (self._done(f"✓ {fmt_t(target) if power else 'OFF'} · {label.lower()}"),
                                   self.app.refresh_now()),
                        lambda m: self._done(m, ERR))

    def resume(self):
        zid = self.zone["id"]
        self._busy("Envoi…")
        self.app.run_bg(lambda: self.app.client.resume_room(zid),
                        lambda _: (self._done("✓ Programme repris"), self.app.refresh_now()),
                        lambda m: self._done(m, ERR))

    def edit_day(self, day):
        if self.week is not None:
            DayEditor(self, day)


# --------------------------------------------------------------------------- #
# Éditeur d'une journée de programme
# --------------------------------------------------------------------------- #
TIMES = [min_to_hm(m) for m in range(MIN_BLOCK, 1440, MIN_BLOCK)]


class DayEditor(tk.Toplevel):
    def __init__(self, room: RoomWindow, day: int):
        super().__init__(room, bg=BG)
        self.room, self.app, self.day = room, room.app, day
        self.title(f"{room.zone['name']} – {DAYS_FR_LONG[day]}")
        self.resizable(False, False)
        self.attributes("-topmost", self.app.cfg.get("topmost", False))
        self.transient(room)

        src = room.week[day] or [{"start": 0, "end": 1440, "on": True, "temp": 19.0}]
        self.blocks = [{"end": b["end"], "on": b["on"], "temp": b["temp"] if b["temp"] is not None else 19.0}
                       for b in src]
        self.blocks[-1]["end"] = 1440

        tk.Label(self, text=f"Programme du {DAYS_FR_LONG[day].lower()}", bg=BG, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(self, text="Chaque créneau commence à la fin du précédent. Minimum 15 min.",
                 bg=BG, fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=16)

        self.preview = tk.Canvas(self, bg=BG, highlightthickness=0, width=520, height=26)
        self.preview.pack(padx=16, pady=(8, 2))
        self.rows = tk.Frame(self, bg=BG)
        self.rows.pack(fill="x", padx=16, pady=4)

        days = tk.Frame(self, bg=BG)
        days.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(days, text="Appliquer à :", bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self.day_vars = []
        for i, d in enumerate(DAYS_FR):
            v = tk.BooleanVar(value=(i == day))
            self.day_vars.append(v)
            tk.Checkbutton(days, text=d, variable=v, bg=BG, fg=FG, selectcolor=SELECT, activebackground=BG,
                           activeforeground=FG, font=(FONT, 9), bd=0, highlightthickness=0,
                           command=self._count).pack(side="left", padx=1)
        presets = tk.Frame(self, bg=BG)
        presets.pack(fill="x", padx=16, pady=(4, 0))
        for label, sel in (("Lun–Ven", range(5)), ("Week-end", range(5, 7)), ("Tous", range(7)),
                           ("Ce jour", [day])):
            button(presets, label, lambda s=sel: self._preset(s), small=True).pack(side="left", padx=(0, 4))

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", padx=16, pady=(12, 14))
        self.save_btn = button(bottom, "Enregistrer", self.save, accent=True)
        self.save_btn.pack(side="left")
        button(bottom, "Annuler", self.destroy).pack(side="left", padx=8)
        self.msg = tk.Label(bottom, text="", bg=BG, fg=MUTED, font=(FONT, 8))
        self.msg.pack(side="left", padx=6)

        self.render()
        self._count()

    # -- modèle --------------------------------------------------------------
    def _sync(self):
        """Lit les champs -> self.blocks (sans valider l'ordre)."""
        for b, r in zip(self.blocks, self.row_vars):
            if r["end"] is not None:
                b["end"] = hm_to_min(r["end"].get(), is_end=True)
            b["on"] = r["on"].get()
            try:
                b["temp"] = clamp_t(float(r["temp"].get().replace(",", ".")))
            except ValueError:
                pass

    def collect(self) -> list[dict]:
        self._sync()
        out, start = [], 0
        for i, b in enumerate(self.blocks):
            end = 1440 if i == len(self.blocks) - 1 else b["end"]
            if end - start < MIN_BLOCK:
                raise ValueError(f"Créneau {min_to_hm(start)} → {min_to_hm(end)} : trop court ou heures pas dans l'ordre")
            out.append({"start": start, "end": end, "on": b["on"], "temp": b["temp"] if b["on"] else None})
            start = end
        return out

    def split(self, i):
        self._sync()
        start = self.blocks[i - 1]["end"] if i else 0
        end = self.blocks[i]["end"]
        if end - start < 2 * MIN_BLOCK:
            self.msg.configure(text="Créneau trop court pour être coupé", fg=ERR)
            return
        mid = start + ((end - start) // 2) // MIN_BLOCK * MIN_BLOCK
        mid = max(start + MIN_BLOCK, min(mid, end - MIN_BLOCK))
        new = dict(self.blocks[i])
        self.blocks[i]["end"] = mid
        self.blocks.insert(i + 1, new)
        self.render()

    def delete(self, i):
        if len(self.blocks) == 1:
            return
        self._sync()
        del self.blocks[i]
        self.blocks[-1]["end"] = 1440
        self.render()

    # -- vue -----------------------------------------------------------------
    def render(self):
        for w in self.rows.winfo_children():
            w.destroy()
        for c, t in enumerate(("Début", "Fin", "Chauffe", "Temp.")):
            tk.Label(self.rows, text=t, bg=BG, fg=MUTED, font=(FONT, 8)).grid(row=0, column=c, padx=4, sticky="w")
        self.row_vars = []
        self.start_lbls = []
        n = len(self.blocks)
        for i, b in enumerate(self.blocks):
            r = i + 1
            sl = tk.Label(self.rows, text="", bg=BG, fg=FG, font=(FONT, 9), width=6)
            sl.grid(row=r, column=0, padx=4, pady=2)
            self.start_lbls.append(sl)
            if i < n - 1:
                ev = tk.StringVar()
                vals = sorted(set(TIMES) | {min_to_hm(b["end"])}, key=hm_to_min)
                sb = tk.Spinbox(self.rows, values=vals, textvariable=ev, width=6, wrap=False,
                                command=self._changed)
                style_spin(sb)
                sb.grid(row=r, column=1, padx=4, pady=2)
                ev.set(min_to_hm(b["end"]))
                sb.bind("<KeyRelease>", lambda e: self._changed())
            else:
                ev = None
                tk.Label(self.rows, text="24:00", bg=BG, fg=MUTED, font=(FONT, 9), width=6).grid(
                    row=r, column=1, padx=4)
            ov = tk.BooleanVar(value=b["on"])
            tk.Checkbutton(self.rows, variable=ov, bg=BG, selectcolor=SELECT, activebackground=BG, bd=0,
                           highlightthickness=0, command=self._changed).grid(row=r, column=2, padx=4)
            tv = tk.StringVar()
            ts = tk.Spinbox(self.rows, from_=T_MIN, to=T_MAX, increment=T_STEP, format="%.1f",
                            textvariable=tv, width=5, command=self._changed)
            style_spin(ts)
            ts.grid(row=r, column=3, padx=4, pady=2)
            tv.set(f"{b['temp']:.1f}")
            ts.bind("<KeyRelease>", lambda e: self._changed())
            button(self.rows, "＋ couper", lambda k=i: self.split(k), small=True).grid(row=r, column=4, padx=(8, 2))
            dl = button(self.rows, "✕", lambda k=i: self.delete(k), small=True)
            dl.grid(row=r, column=5, padx=2)
            dl.set_enabled(n > 1)
            self.row_vars.append({"end": ev, "on": ov, "temp": tv})
        self._changed()

    def _changed(self):
        self._sync()
        start = 0
        for i, b in enumerate(self.blocks):
            self.start_lbls[i].configure(text=min_to_hm(start))
            start = b["end"] if i < len(self.blocks) - 1 else 1440
        try:
            blocks = self.collect()
            self.msg.configure(text="", fg=MUTED)
            self._count()
        except ValueError as e:
            blocks = None
            self.msg.configure(text=str(e), fg=ERR)
        self.preview.delete("all")
        if blocks:
            W = 520
            for b in blocks:
                x0, x1 = W * b["start"] / 1440, W * b["end"] / 1440
                self.preview.create_rectangle(x0 + 1, 2, x1 - 1, 24, outline="",
                                              fill=temp_color(b["temp"]) if b["on"] else OFF)
                if x1 - x0 > 34:
                    self.preview.create_text((x0 + x1) / 2, 13, fill="white", font=(FONT, 8, "bold"),
                                             text=fmt_t(b["temp"]) if b["on"] else "off")

    def _preset(self, sel):
        for i, v in enumerate(self.day_vars):
            v.set(i in sel)
        self._count()

    def _count(self):
        n = sum(v.get() for v in self.day_vars)
        if not self.msg.cget("fg") == ERR:
            self.msg.configure(text=f"{n} jour(s) · {n} requête(s) API", fg=MUTED)

    def save(self):
        try:
            blocks = self.collect()
        except ValueError as e:
            self.msg.configure(text=str(e), fg=ERR)
            return
        days = [i for i, v in enumerate(self.day_vars) if v.get()]
        if not days:
            self.msg.configure(text="Coche au moins un jour", fg=ERR)
            return
        zid = self.room.zone["id"]
        self.save_btn.set_enabled(False)
        self.msg.configure(text="Enregistrement…", fg=MUTED)

        def work():
            for d in days:
                self.app.client.save_day(zid, d, blocks)

        def ok(_):
            wk = self.app.sched_cache.get(zid) or self.room.week
            for d in days:
                wk[d] = copy.deepcopy(blocks)
            self.app.sched_cache[zid] = wk
            if self.room.winfo_exists():
                self.room.show_week(wk)
            self.app.refresh_now()
            if self.winfo_exists():
                self.destroy()

        def err(m):
            if self.winfo_exists():
                self.save_btn.set_enabled(True)
                self.msg.configure(text=m, fg=ERR)
            self.app.sched_cache.pop(zid, None)  # état incertain : on relira le programme
        self.app.run_bg(work, ok, err)


# --------------------------------------------------------------------------- #
# Barre système
# --------------------------------------------------------------------------- #
def start_tray(app: App):
    try:
        import pystray
    except Exception:
        return None
    try:
        icon = pystray.Icon(
            "tado_widget", make_icon(64), "tado°",
            menu=pystray.Menu(
                pystray.MenuItem("Afficher / masquer", lambda: app.post(app.toggle_visible), default=True),
                pystray.MenuItem("Rafraîchir", lambda: app.post(app.refresh_now)),
                pystray.MenuItem("Quitter", lambda: app.post(app.quit)),
            ))
        threading.Thread(target=icon.run, daemon=True).start()
        return icon
    except Exception:
        return None


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Widget de bureau tado")
    ap.add_argument("--interval", type=int, default=1200,
                    help="secondes entre deux rafraîchissements (défaut 1200 ; avec Auto-Assist : 60)")
    ap.add_argument("--home", type=int, help="ID du logement si plusieurs")
    ap.add_argument("--demo", action="store_true", help="données fictives, sans compte tado")
    ap.add_argument("--logout", action="store_true", help="supprime le token enregistré")
    args = ap.parse_args()

    if args.logout:
        TadoAuth().logout()
        return

    if sys.platform == "win32":
        try:  # rendu net sur écrans HiDPI (avant la création de la fenêtre)
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    if args.demo:
        client, auth = DemoClient(), None
    else:
        auth = TadoAuth()
        client = TadoClient(auth, args.home)

    root = tk.Tk()
    app = App(root, client, auth, args.interval)
    app.tray = start_tray(app)
    root.mainloop()


if __name__ == "__main__":
    main()
