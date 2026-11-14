#!/usr/bin/env python3
"""
Tado Widget - petit widget de bureau pour voir les températures et les
programmations tado en (quasi) temps réel.

Aucune dépendance : Python 3.9+ avec tkinter (inclus dans l'installeur Windows
de python.org).

Utilise l'API web non officielle de tado (celle de l'appli / app.tado.com) :
  - Auth : OAuth2 "device code" sur login.tado.com (on valide dans le navigateur,
    aucun mot de passe n'est stocké, seulement un refresh token local).
  - Données : my.tado.com/api/v2 (tado V3/V3+) ou hops.tado.com (tado X).

Usage :
  python tado_widget.py              # lance le widget
  python tado_widget.py --interval 60
  python tado_widget.py --demo       # données fictives, sans compte
  python tado_widget.py --logout     # oublie le token
"""
from __future__ import annotations

import argparse
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
from datetime import datetime, timezone
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
DAYS_EN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

# --------------------------------------------------------------------------- #
# Thème
# --------------------------------------------------------------------------- #
BG = "#17181c"
CARD = "#22242a"
CARD_HOVER = "#2b2e36"
FG = "#e8e9ec"
MUTED = "#8b8f99"
ACCENT = "#f59e0b"
COLD = "#60a5fa"
OFF = "#3a3d46"
ERR = "#f87171"
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
def http(method: str, url: str, form: dict | None = None, headers: dict | None = None):
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    req.add_header("User-Agent", "tado-widget/1.0")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw.decode(errors="replace")
        return e.code, payload, e.headers


# --------------------------------------------------------------------------- #
# Auth (device code flow + refresh token rotatif)
# --------------------------------------------------------------------------- #
class TadoAuth:
    def __init__(self):
        self.tok: dict = {}
        self.lock = threading.Lock()
        try:
            self.tok = json.loads(TOKEN_FILE.read_text())
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
        if st != 200:
            raise ApiError(st, str(data))
        return data  # device_code, user_code, verification_uri_complete, interval, expires_in

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
                self._store(data)  # le refresh token tourne à chaque usage : on sauve le nouveau
                return data["access_token"]
            if st in (400, 401):
                self.logout()
                raise AuthRequired()
            raise ApiError(st, str(data))


# --------------------------------------------------------------------------- #
# Client API
# --------------------------------------------------------------------------- #
def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def build_week(blocks: list[dict], temp_key: str) -> list[list[dict]]:
    """Transforme les blocs tado (dayType/start/end/setting) en 7 listes (Lun..Dim)."""
    by_type: dict[str, list[dict]] = {}
    for b in blocks:
        s = b.get("setting") or {}
        on = s.get("power") == "ON"
        t = (s.get("temperature") or {}).get(temp_key) if on else None
        by_type.setdefault(b.get("dayType", ""), []).append({
            "start": b.get("start", "00:00"),
            "end": b.get("end", "00:00"),
            "on": on,
            "temp": t,
        })
    week = []
    for i, day in enumerate(DAYS_EN):
        cands = [day]
        if i < 5:
            cands.append("MONDAY_TO_FRIDAY")
        cands.append("MONDAY_TO_SUNDAY")
        week.append(next((by_type[c] for c in cands if c in by_type), []))
    return week


class TadoClient:
    def __init__(self, auth: TadoAuth, home_id: int | None = None):
        self.auth = auth
        self.want_home = home_id
        self.home_id: int | None = None
        self.home_name = ""
        self.is_x = False
        self.zones: list[dict] = []
        self.quota_left: int | None = None
        self.calls = 0

    def get(self, path: str, base: str = API):
        for attempt in (0, 1):
            tok = self.auth.access_token()
            st, data, hdrs = http("GET", base + path, headers={"Authorization": f"Bearer {tok}"})
            self.calls += 1
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

    # -- états ---------------------------------------------------------------
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
            out.append({
                "id": z["id"],
                "name": z["name"],
                "type": z.get("type"),
                "temp": (sens.get("insideTemperature") or {}).get("celsius"),
                "hum": (sens.get("humidity") or {}).get("percentage"),
                "on": setting.get("power") == "ON",
                "target": (setting.get("temperature") or {}).get("celsius"),
                "heat": ((st.get("activityDataPoints") or {}).get("heatingPower") or {}).get("percentage"),
                "manual": st.get("overlay") is not None,
                "away": st.get("tadoMode") == "AWAY",
                "window": bool(st.get("openWindow") or st.get("openWindowDetected")),
                "offline": (st.get("link") or {}).get("state") == "OFFLINE",
                "next_at": parse_iso(nxt.get("start")),
                "next_temp": (nset.get("temperature") or {}).get("celsius") if nset.get("power") == "ON" else None,
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
            out.append({
                "id": r.get("id"),
                "name": r.get("name", "?"),
                "type": "HEATING",
                "temp": (sens.get("insideTemperature") or {}).get("value"),
                "hum": (sens.get("humidity") or {}).get("percentage"),
                "on": setting.get("power") == "ON",
                "target": (setting.get("temperature") or {}).get("value"),
                "heat": (r.get("heatingPower") or {}).get("percentage"),
                "manual": r.get("manualControlTermination") is not None,
                "away": False,
                "window": bool(r.get("openWindow")),
                "offline": (r.get("connection") or {}).get("state") == "DISCONNECTED",
                "next_at": parse_iso(nxt.get("start")),
                "next_temp": (nset.get("temperature") or {}).get("value") if nset.get("power") == "ON" else None,
                "next_on": nset.get("power") == "ON",
            })
        return out

    def weather(self) -> float | None:
        w = self.get(f"/homes/{self.home_id}/weather")
        return ((w or {}).get("outsideTemperature") or {}).get("celsius")

    # -- programmation -------------------------------------------------------
    def schedule(self, zone_id) -> list[list[dict]]:
        if self.is_x:
            data = self.get(f"/homes/{self.home_id}/rooms/{zone_id}/schedule", base=HOPS)
            blocks = data.get("schedule", []) if isinstance(data, dict) else data
            return build_week(blocks or [], "value")
        tt = self.get(f"/homes/{self.home_id}/zones/{zone_id}/schedule/activeTimetable")
        blocks = self.get(f"/homes/{self.home_id}/zones/{zone_id}/schedule/timetables/{tt['id']}/blocks")
        return build_week(blocks or [], "celsius")


# --------------------------------------------------------------------------- #
# Mode démo
# --------------------------------------------------------------------------- #
class DemoClient:
    home_name = "Maison (démo)"
    quota_left = 87
    is_x = False

    def init(self):
        pass

    def states(self):
        now = datetime.now().astimezone()
        nxt = now.replace(minute=0, second=0, microsecond=0)
        nxt = nxt.replace(hour=(now.hour + 2) % 24)
        return [
            dict(id=1, name="Salon", type="HEATING", temp=20.6, hum=48, on=True, target=21.0, heat=35,
                 manual=False, away=False, window=False, offline=False, next_at=nxt, next_temp=18.0, next_on=True),
            dict(id=2, name="Chambre", type="HEATING", temp=18.1, hum=55, on=True, target=17.0, heat=0,
                 manual=True, away=False, window=False, offline=False, next_at=nxt, next_temp=19.0, next_on=True),
            dict(id=3, name="Bureau", type="HEATING", temp=19.4, hum=44, on=False, target=None, heat=0,
                 manual=False, away=False, window=True, offline=False, next_at=nxt, next_temp=20.0, next_on=True),
            dict(id=4, name="Salle de bain", type="HEATING", temp=21.9, hum=67, on=True, target=22.0, heat=80,
                 manual=False, away=False, window=False, offline=False, next_at=None, next_temp=None, next_on=False),
        ]

    def weather(self):
        return 7.5

    def schedule(self, zone_id):
        wk = [
            {"start": "00:00", "end": "06:30", "on": True, "temp": 17.0},
            {"start": "06:30", "end": "08:30", "on": True, "temp": 20.5},
            {"start": "08:30", "end": "17:30", "on": True, "temp": 18.0},
            {"start": "17:30", "end": "22:30", "on": True, "temp": 21.0},
            {"start": "22:30", "end": "00:00", "on": True, "temp": 17.0},
        ]
        we = [
            {"start": "00:00", "end": "08:00", "on": True, "temp": 17.0},
            {"start": "08:00", "end": "23:00", "on": True, "temp": 21.0},
            {"start": "23:00", "end": "00:00", "on": False, "temp": None},
        ]
        return [wk] * 5 + [we] * 2


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
def temp_color(t: float | None) -> str:
    if t is None:
        return OFF
    lo, hi = 15.0, 23.0
    k = max(0.0, min(1.0, (t - lo) / (hi - lo)))
    a = (0x3b, 0x82, 0xf6)  # bleu
    b = (0xf9, 0x73, 0x16)  # orange
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def fmt_t(t) -> str:
    return "--" if t is None else f"{t:.1f}°"


def hm_to_min(s: str, is_end=False) -> int:
    h, m = (int(x) for x in s.split(":")[:2])
    v = h * 60 + m
    return 1440 if is_end and v == 0 else v


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
        self.last_states: list[dict] = []
        self.outside: float | None = None
        self.cfg = self._load_cfg()

        root.title("tado°")
        root.configure(bg=BG)
        root.minsize(300, 120)
        root.attributes("-topmost", self.cfg.get("topmost", False))
        if self.cfg.get("geometry"):
            root.geometry(self.cfg["geometry"])
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # en-tête
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.title_lbl = tk.Label(head, text="tado°", bg=BG, fg=FG, font=(FONT, 12, "bold"))
        self.title_lbl.pack(side="left")
        self.out_lbl = tk.Label(head, text="", bg=BG, fg=MUTED, font=(FONT, 10))
        self.out_lbl.pack(side="left", padx=8)
        self.pin_btn = self._icon_btn(head, "📌", self.toggle_pin)
        self.pin_btn.pack(side="right")
        self._icon_btn(head, "⟳", self.refresh_now).pack(side="right", padx=4)
        self._update_pin()

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=8)

        self.status = tk.Label(root, text="Démarrage…", bg=BG, fg=MUTED, font=(FONT, 8), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(2, 8))

        self.root.after(150, self.pump)
        threading.Thread(target=self.worker, daemon=True).start()

    # -- config --------------------------------------------------------------
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

    def on_close(self):
        self.cfg["geometry"] = self.root.geometry()
        self._save_cfg()
        self.stop.set()
        self.cancel_login.set()
        self.wake.set()
        self.root.destroy()

    def _icon_btn(self, parent, txt, cmd):
        b = tk.Label(parent, text=txt, bg=BG, fg=MUTED, font=(FONT, 11), cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(fg=FG))
        b.bind("<Leave>", lambda e: self._update_pin() if b is self.pin_btn else b.configure(fg=MUTED))
        return b

    def toggle_pin(self):
        self.cfg["topmost"] = not self.cfg.get("topmost", False)
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._update_pin()
        self._save_cfg()

    def _update_pin(self):
        self.pin_btn.configure(fg=ACCENT if self.cfg.get("topmost") else MUTED)

    def refresh_now(self):
        self.wake.set()

    # -- thread réseau -------------------------------------------------------
    def worker(self):
        initialized = False
        cycle = 0
        while not self.stop.is_set():
            delay = self.interval
            try:
                if not initialized:
                    self.client.init()
                    initialized = True
                    self.q.put(("home", self.client.home_name))
                states = self.client.states()
                # météo : 1 cycle sur 3 pour économiser le quota
                if cycle % 3 == 0:
                    try:
                        self.outside = self.client.weather()
                    except Exception:
                        pass
                cycle += 1
                self.q.put(("states", states))
            except AuthRequired:
                initialized = False
                self.q.put(("login", None))
                if not self._do_login():
                    return
                continue
            except ApiError as e:
                if e.status == 429:
                    delay = max(self.interval, 3600)
                    self.q.put(("error", "Quota API tado atteint – réessai dans 1 h"))
                else:
                    self.q.put(("error", str(e)))
                    delay = min(self.interval, 120)
            except Exception as e:  # réseau, etc.
                self.q.put(("error", f"Erreur : {e}"))
                delay = min(self.interval, 120)
            self.wake.wait(delay)
            self.wake.clear()

    def _do_login(self) -> bool:
        try:
            dev = self.auth.start_device_login()
        except Exception as e:
            self.q.put(("error", f"Login impossible : {e}"))
            return False
        self.q.put(("code", dev))
        url = dev.get("verification_uri_complete") or dev.get("verification_uri")
        if url:
            webbrowser.open(url)
        try:
            ok = self.auth.poll_device_login(dev, self.cancel_login)
        except Exception as e:
            self.q.put(("error", f"Login refusé : {e}"))
            return False
        if not ok and not self.stop.is_set():
            self.q.put(("error", "Code expiré – relance le widget"))
        return ok

    def load_schedule(self, zone, win):
        def run():
            try:
                wk = self.client.schedule(zone["id"])
                self.sched_cache[zone["id"]] = wk
                self.q.put(("sched", (win, zone, wk)))
            except Exception as e:
                self.q.put(("sched_err", (win, str(e))))
        threading.Thread(target=run, daemon=True).start()

    # -- boucle UI -----------------------------------------------------------
    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(150, self.pump)

    def on_home(self, name):
        self.title_lbl.configure(text=name or "tado°")

    def on_error(self, msg):
        self.status.configure(text=msg, fg=ERR)

    def on_login(self, _):
        self._clear()
        tk.Label(self.body, text="Connexion à tado requise…", bg=BG, fg=FG, font=(FONT, 10)).pack(pady=10)

    def on_code(self, dev):
        self._clear()
        url = dev.get("verification_uri_complete") or dev.get("verification_uri", "")
        tk.Label(self.body, text="Valide la connexion dans ton navigateur.\nCode :",
                 bg=BG, fg=FG, font=(FONT, 10), justify="center").pack(pady=(10, 2))
        tk.Label(self.body, text=dev.get("user_code", ""), bg=BG, fg=ACCENT,
                 font=(FONT, 20, "bold")).pack()
        link = tk.Label(self.body, text="Ouvrir la page de connexion", bg=BG, fg=COLD,
                        font=(FONT, 9, "underline"), cursor="hand2")
        link.pack(pady=(4, 10))
        link.bind("<Button-1>", lambda e: webbrowser.open(url))
        self.status.configure(text="En attente de validation…", fg=MUTED)

    def on_states(self, states):
        self.last_states = states
        self.out_lbl.configure(text=f"Ext. {fmt_t(self.outside)}" if self.outside is not None else "")
        self._clear()
        for z in states:
            self._zone_card(z)
        txt = f"Mis à jour {datetime.now():%H:%M}"
        if self.client.quota_left is not None:
            txt += f"  ·  quota API restant : {self.client.quota_left}"
        txt += "  ·  clic = programme"
        self.status.configure(text=txt, fg=MUTED)

    def on_sched(self, data):
        win, zone, wk = data
        if win.winfo_exists():
            self._draw_schedule(win, zone, wk)

    def on_sched_err(self, data):
        win, msg = data
        if win.winfo_exists():
            for w in win.winfo_children():
                w.destroy()
            tk.Label(win, text=f"Programme indisponible\n{msg}", bg=BG, fg=ERR,
                     font=(FONT, 9), wraplength=400).pack(padx=20, pady=20)

    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    # -- carte zone ----------------------------------------------------------
    def _zone_card(self, z):
        card = tk.Frame(self.body, bg=CARD, cursor="hand2")
        card.pack(fill="x", pady=3)
        heating = (z.get("heat") or 0) > 0

        right = tk.Frame(card, bg=CARD)  # packé d'abord : la temp n'est jamais tronquée
        right.pack(side="right", padx=10, pady=6)
        left = tk.Frame(card, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=10, pady=6)

        tk.Label(left, text=z["name"], bg=CARD, fg=FG, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")

        # ligne d'état
        bits = []
        if z.get("offline"):
            bits.append("⚠ hors ligne")
        if z.get("window"):
            bits.append("🪟 fenêtre ouverte")
        if z.get("away"):
            bits.append("absent")
        bits.append("manuel" if z.get("manual") else "auto")
        if z.get("next_at"):
            nt = fmt_t(z["next_temp"]) if z.get("next_on") else "off"
            bits.append(f"→ {nt} à {z['next_at']:%H:%M}")
        tk.Label(left, text="  ·  ".join(bits), bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x")

        if z.get("type") == "HOT_WATER":
            big = "ON" if z.get("on") else "OFF"
            tk.Label(right, text=f"💧 {big}", bg=CARD, fg=FG, font=(FONT, 14, "bold")).pack(anchor="e")
        else:
            tk.Label(right, text=fmt_t(z.get("temp")), bg=CARD,
                     fg=ACCENT if heating else FG, font=(FONT, 18, "bold")).pack(anchor="e")
            sub = f"consigne {fmt_t(z['target'])}" if z.get("on") else "chauffage off"
            if heating:
                sub += f" · 🔥{z['heat']:.0f}%"
            if z.get("hum") is not None:
                sub += f" · 💧{z['hum']:.0f}%"
            tk.Label(right, text=sub, bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="e")

        def bind_all(w):
            w.bind("<Button-1>", lambda e: self.open_schedule(z))
            w.bind("<Enter>", lambda e: self._hover(card, True))
            w.bind("<Leave>", lambda e: self._hover(card, False))
            for c in w.winfo_children():
                bind_all(c)
        bind_all(card)

    def _hover(self, card, on):
        col = CARD_HOVER if on else CARD
        def paint(w):
            try:
                w.configure(bg=col)
            except tk.TclError:
                pass
            for c in w.winfo_children():
                paint(c)
        paint(card)

    # -- fenêtre programme ---------------------------------------------------
    def open_schedule(self, zone):
        if zone.get("type") not in (None, "HEATING"):
            return
        win = tk.Toplevel(self.root, bg=BG)
        win.title(f"Programme – {zone['name']}")
        win.attributes("-topmost", self.cfg.get("topmost", False))
        win.resizable(False, False)
        if zone["id"] in self.sched_cache:  # pas de nouvel appel API si déjà chargé
            self._draw_schedule(win, zone, self.sched_cache[zone["id"]])
        else:
            tk.Label(win, text="Chargement…", bg=BG, fg=MUTED, font=(FONT, 10)).pack(padx=40, pady=30)
            self.load_schedule(zone, win)

    def _draw_schedule(self, win, zone, week):
        for w in win.winfo_children():
            w.destroy()
        W, LBL, ROW, GAP, TOP = 640, 44, 30, 6, 26
        H = TOP + 7 * (ROW + GAP) + 8
        cv = tk.Canvas(win, width=W + LBL + 16, height=H, bg=BG, highlightthickness=0)
        cv.pack(padx=12, pady=(12, 4))
        x0 = LBL

        def xm(minute):
            return x0 + W * minute / 1440

        for h in range(0, 25, 3):
            x = xm(h * 60)
            cv.create_line(x, TOP - 4, x, H - 8, fill="#2c2f37")
            cv.create_text(x, TOP - 14, text=f"{h:02d}h", fill=MUTED, font=(FONT, 8))

        today = datetime.now().weekday()
        for i, blocks in enumerate(week):
            y = TOP + i * (ROW + GAP)
            cv.create_text(x0 - 8, y + ROW / 2, text=DAYS_FR[i], anchor="e",
                           fill=ACCENT if i == today else FG, font=(FONT, 9, "bold" if i == today else "normal"))
            if not blocks:
                cv.create_rectangle(xm(0), y, xm(1440), y + ROW, fill=OFF, outline="")
            for b in blocks:
                a, e = hm_to_min(b["start"]), hm_to_min(b["end"], is_end=True)
                col = temp_color(b["temp"]) if b["on"] else OFF
                cv.create_rectangle(xm(a) + 1, y, xm(e) - 1, y + ROW, fill=col, outline="")
                label = fmt_t(b["temp"]) if b["on"] else "off"
                if xm(e) - xm(a) > 38:
                    cv.create_text((xm(a) + xm(e)) / 2, y + ROW / 2, text=label, fill="white",
                                   font=(FONT, 8, "bold"))
        now = datetime.now()
        y = TOP + today * (ROW + GAP)
        xn = xm(now.hour * 60 + now.minute)
        cv.create_line(xn, y - 3, xn, y + ROW + 3, fill="white", width=2)

        tk.Label(win, text="Programme actif (lecture seule). Blocs gris = chauffage coupé.",
                 bg=BG, fg=MUTED, font=(FONT, 8)).pack(pady=(0, 10))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Widget de bureau tado")
    ap.add_argument("--interval", type=int, default=1200,
                    help="secondes entre deux rafraîchissements (défaut 1200 = 20 min, "
                         "adapté au quota gratuit de ~100 requêtes/jour ; avec Auto-Assist : 60)")
    ap.add_argument("--home", type=int, help="ID du logement si plusieurs")
    ap.add_argument("--demo", action="store_true", help="données fictives, sans compte tado")
    ap.add_argument("--logout", action="store_true", help="supprime le token enregistré")
    args = ap.parse_args()

    if args.logout:
        TadoAuth().logout()
        print("Token supprimé.")
        return

    if args.demo:
        client, auth = DemoClient(), None
    else:
        auth = TadoAuth()
        client = TadoClient(auth, args.home)

    root = tk.Tk()
    if sys.platform == "win32":
        try:  # rendu net sur écrans HiDPI
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(root, client, auth, args.interval)
    root.mainloop()


if __name__ == "__main__":
    main()
