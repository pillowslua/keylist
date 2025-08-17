#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoFarm Python — Duolingo Farmer (Rich CLI Dashboard, Multi‑account)

⚠️ Lưu ý pháp lý: Tự động hoá có thể vi phạm Điều khoản của Duolingo. Chỉ dùng
trên tài khoản thử nghiệm. Bạn tự chịu rủi ro.

Điểm MỚI:
- Bỏ Tkinter → UI terminal đẹp bằng **Rich** (màu + icon + bảng cập nhật realtime).
- Đa tài khoản chạy song song, lệnh điều khiển trực tiếp trong terminal:
  * `pause <id>`  `resume <id>`  `stop <id>`  `mode <id> <xp|gem|streak_farm|streak_repair|combo>`
  * `add` (thêm profile), `targets <id> xp=<n> gems=<n> streak+=<n>`, `quit`
- Chế độ **combo** tự xoay vòng XP → Gems → Streak.
- Mục tiêu/giới hạn cho từng profile: đủ là auto-stop.
- Tăng độ ổn định: retry + exponential backoff + jitter (429/5xx), delay riêng từng profile.
- Sửa triệt để lỗi streak/streak_repair (tạo/hoàn tất session đầy đủ; báo lỗi rõ).
- Tổng quan: thống kê XP, Gems, số profile đang chạy; ETA đơn giản.
- **Không ghi file** (JWT backup chỉ trong bộ nhớ; tuyệt đối không lưu ra đĩa).

Cần cài:
    pip install httpx==0.27.2 rich==13.7.1
(Flask API tuỳ chọn nếu bật: `pip install flask==3.0.3` — mặc định tắt)

Chạy:
    python autofarm_cli.py
"""
from __future__ import annotations
import asyncio
import base64
import json
import math
import os
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Tuple

import httpx
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.align import Align
from rich import box

# ---- Optional Flask (only if API enabled) ----
try:
    from flask import Flask, jsonify, request  # type: ignore
except Exception:  # not installed
    Flask = None  # type: ignore

console = Console()

# ===== Config ===== #
USER_AGENT = "AutoFarmPython"
DEFAULT_DELAY = 0.8         # s giữa các request
GLOBAL_MIN_DELAY = 0.4
MAX_RETRIES = 4
BACKOFF_CAP = 7.5
API_PORT = 8787
REFRESH_INTERVAL = 0.3      # s refresh dashboard

EMO = {
    "ok": "🟢", "err": "🔴", "info": "🔷",
    "xp": "⭐", "gem": "💎", "streak": "🔥", "repair": "🧩",
    "run": "▶️", "pause": "⏸️", "resume": "⏯️", "stop": "⏹️", "user": "👤",
}

# ===== Utils ===== #

def _decode_sub(jwt_token: str) -> Optional[str]:
    try:
        payload = jwt_token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub")
    except Exception:
        return None

async def _sleep_with_jitter(base: float):
    await asyncio.sleep(max(GLOBAL_MIN_DELAY, base + random.uniform(-0.08, 0.12)))

# ===== Data ===== #

@dataclass
class UserInfo:
    username: str = "?"
    fromLanguage: str = "?"
    learningLanguage: str = "?"
    streak: int = 0
    totalXp: int = 0
    gems: int = 0
    streakData: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, j: Dict[str, Any]) -> "UserInfo":
        return cls(
            username=j.get("username", "?"),
            fromLanguage=j.get("fromLanguage", "?"),
            learningLanguage=j.get("learningLanguage", "?"),
            streak=int(j.get("streak", 0) or 0),
            totalXp=int(j.get("totalXp", 0) or 0),
            gems=int(j.get("gems", 0) or 0),
            streakData=j.get("streakData") or {},
        )

# ===== API Client ===== #

class DuoClient:
    def __init__(self, jwt_token: str, delay: float = DEFAULT_DELAY):
        self.jwt = jwt_token.strip()
        self.sub = _decode_sub(self.jwt)
        self.headers = {
            "Authorization": f"Bearer {self.jwt}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.user: Optional[UserInfo] = None
        self.delay = max(GLOBAL_MIN_DELAY, float(delay))
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def close(self):
        try:
            await self.http.aclose()
        except Exception:
            pass

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        tries = 0
        while True:
            try:
                r = await self.http.request(method, url, headers=self.headers, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    tries += 1
                    if tries > MAX_RETRIES:
                        return r
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra and ra.isdigit() else min(BACKOFF_CAP, 0.7 * (2 ** (tries-1)))
                    await _sleep_with_jitter(wait)
                    continue
                return r
            except httpx.HTTPError:
                tries += 1
                if tries > MAX_RETRIES:
                    raise
                await _sleep_with_jitter(min(BACKOFF_CAP, 0.5 * (2 ** (tries-1))))

    async def get_user_info(self) -> Optional[UserInfo]:
        if not self.sub:
            return None
        url = (
            f"https://www.duolingo.com/2017-06-30/users/{self.sub}"
            "?fields=id,username,fromLanguage,learningLanguage,streak,totalXp,gems,streakData"
        )
        r = await self._request("GET", url)
        if r.status_code == 200:
            self.user = UserInfo.from_json(r.json())
            return self.user
        return None

    # ---- GEM ---- #
    async def farm_gem_once(self) -> bool:
        if not (self.sub and self.user):
            return False
        reward_id = "SKILL_COMPLETION_BALANCED-dd2495f4_d44e_3fc3_8ac8_94e2191506f0-2-GEMS"
        url = f"https://www.duolingo.com/2017-06-30/users/{self.sub}/rewards/{reward_id}"
        payload = {
            "consumed": True,
            "learningLanguage": self.user.learningLanguage,
            "fromLanguage": self.user.fromLanguage,
        }
        r = await self._request("PATCH", url, json=payload)
        return r.status_code == 200

    # ---- XP ---- #
    async def farm_xp_once(self) -> int:
        if not self.user:
            return 0
        url = f"https://stories.duolingo.com/api2/stories/en-{self.user.fromLanguage}-the-passport/complete"
        payload = {
            "awardXp": True,
            "mode": "READ",
            "isLegendaryMode": True,
            "fromLanguage": self.user.fromLanguage,
            "learningLanguage": "en",
            "startTime": int(time.time()),
            "happyHourBonusXp": 449,
        }
        r = await self._request("POST", url, json=payload)
        if r.status_code == 200:
            data = r.json() or {}
            return int(data.get("awardedXp", 0) or 0)
        return 0

    # ---- Sessions for streak ---- #
    async def _create_session(self) -> Optional[Dict[str, Any]]:
        if not self.user:
            return None
        payload = {
            "challengeTypes": [
                "assist","characterIntro","characterMatch","characterPuzzle","characterSelect","characterTrace",
                "characterWrite","completeReverseTranslation","definition","dialogue","extendedMatch",
                "extendedListenMatch","form","freeResponse","gapFill","judge","listen","listenComplete",
                "listenMatch","match","name","listenComprehension","listenIsolation","listenSpeak",
                "listenTap","orderTapComplete","partialListen","partialReverseTranslate","patternTapComplete",
                "radioBinary","radioImageSelect","radioListenMatch","radioListenRecognize","radioSelect",
                "readComprehension","reverseAssist","sameDifferent","select","selectPronunciation",
                "selectTranscription","svgPuzzle","syllableTap","syllableListenTap","speak","tapCloze",
                "tapClozeTable","tapComplete","tapCompleteTable","tapDescribe","translate","transliterate",
                "transliterationAssist","typeCloze","typeClozeTable","typeComplete","typeCompleteTable",
                "writeComprehension",
            ],
            "fromLanguage": self.user.fromLanguage,
            "isFinalLevel": False,
            "isV2": True,
            "juicy": True,
            "learningLanguage": self.user.learningLanguage,
            "smartTipsVersion": 2,
            "type": "GLOBAL_PRACTICE",
        }
        r = await self._request("POST", "https://www.duolingo.com/2017-06-30/sessions", json=payload)
        if r.status_code == 200:
            return r.json()
        return None

    async def _update_session(self, sess: Dict[str, Any], start_ts: int, end_ts: int) -> Tuple[bool, str]:
        if not sess:
            return False, "no_session"
        sid = sess.get("id")
        if not sid:
            return False, "no_session_id"
        payload = {
            **sess,
            "heartsLeft": 0,
            "startTime": start_ts,
            "endTime": end_ts,
            "failed": False,
            "maxInLessonStreak": 9,
            "shouldLearnThings": True,
        }
        url = f"https://www.duolingo.com/2017-06-30/sessions/{sid}"
        r = await self._request("PUT", url, json=payload)
        if r.status_code == 200:
            return True, "ok"
        try:
            txt = r.text[:200]
        except Exception:
            txt = str(r.status_code)
        return False, f"{r.status_code}:{txt}"

    async def streak_bump_once(self, ts: int) -> Tuple[bool, str]:
        sess = await self._create_session()
        if not sess:
            return False, "create_fail"
        ok, why = await self._update_session(sess, ts, ts + 60)
        return ok, why

    async def streak_repair(self) -> Tuple[int, str]:
        if not self.user:
            return 0, "no_user"
        sd = self.user.streakData or {}
        cur = (sd.get("currentStreak") or {})
        if not cur:
            return 0, "no_currentStreak"
        def _to_ts(date_str: str) -> int:
            try:
                from datetime import datetime
                return int(datetime.fromisoformat(date_str).timestamp())
            except Exception:
                return int(time.time())
        start_ts = _to_ts(cur.get("startDate", ""))
        end_ts = _to_ts(cur.get("endDate", ""))
        expected = max(1, (end_ts - start_ts) // 86400 + 1)
        have = int(self.user.streak)
        need = max(0, expected - have)
        worked = 0
        now = int(time.time())
        for i in range(need):
            ts = now - (i + 1) * 86400
            ok, _ = await self.streak_bump_once(ts)
            if ok:
                worked += 1
                self.user.streak += 1
            await _sleep_with_jitter(self.delay)
        return worked, "ok" if worked else "no_change"

# ===== Worker / State ===== #

@dataclass
class Goals:
    target_xp: Optional[int] = None
    target_gems: Optional[int] = None
    target_streak_inc: Optional[int] = None

@dataclass
class Profile:
    id: int
    label: str
    jwt: str
    mode: str  # 'xp' | 'gem' | 'streak_farm' | 'streak_repair' | 'combo'
    delay: float = DEFAULT_DELAY
    client: Optional[DuoClient] = None
    running: bool = False
    paused: bool = False
    goals: Goals = field(default_factory=Goals)
    baseline: Dict[str, int] = field(default_factory=dict)
    last_msg: str = ""

# ===== CLI App ===== #

class AutoFarmCLI:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.profiles: List[Profile] = []
        self.total_xp = 0
        self.total_gems = 0
        self._cmd_q: "queue.Queue[str]" = queue.Queue()
        self.api_enabled = False
        self._api_app = None
        self._api_thread: Optional[threading.Thread] = None

    # ---------- profile mgmt ---------- #
    def add_profile(self, label: str, jwt: str, mode: str, delay: float = DEFAULT_DELAY) -> Profile:
        p = Profile(id=(len(self.profiles)+1), label=label, jwt=jwt, mode=mode, delay=delay)
        p.client = DuoClient(jwt, delay)
        self.profiles.append(p)
        return p

    # ---------- worker ---------- #
    async def _profile_loop(self, p: Profile):
        try:
            user = await p.client.get_user_info()
            if not user:
                p.last_msg = f"{EMO['err']} JWT/Info fail"
                p.running = False
                return
            p.baseline = {"xp": user.totalXp, "gems": user.gems, "streak": user.streak}
            p.running = True
            p.last_msg = f"{EMO['ok']} Ready as {user.username}"
            while p.running:
                if p.paused:
                    await asyncio.sleep(0.2)
                    continue
                if p.mode == "gem":
                    ok = await p.client.farm_gem_once()
                    if ok:
                        p.client.user.gems += 30
                        p.last_msg = f"{EMO['gem']} +30 gems"
                    else:
                        p.last_msg = f"{EMO['err']} gem fail"
                elif p.mode == "xp":
                    gained = await p.client.farm_xp_once()
                    if gained > 0:
                        p.client.user.totalXp += gained
                        p.last_msg = f"{EMO['xp']} +{gained} XP"
                    else:
                        p.last_msg = f"{EMO['err']} xp fail"
                elif p.mode == "streak_farm":
                    base = int(time.time()) - 86400
                    ts = base - max(0, (p.client.user.streak - p.baseline.get('streak',0))) * 86400
                    ok, why = await p.client.streak_bump_once(ts)
                    if ok:
                        p.client.user.streak += 1
                        p.last_msg = f"{EMO['streak']} +1 streak"
                    else:
                        p.last_msg = f"{EMO['err']} streak: {why}"
                elif p.mode == "streak_repair":
                    fixed, why = await p.client.streak_repair()
                    if fixed > 0:
                        p.last_msg = f"{EMO['repair']} repaired +{fixed}"
                    else:
                        p.last_msg = f"{EMO['err']} repair: {why}"
                    p.running = False  # one‑shot
                elif p.mode == "combo":
                    # xoay vòng: xp -> gem -> streak_bump (1)
                    gained = await p.client.farm_xp_once()
                    if gained > 0:
                        p.client.user.totalXp += gained
                        p.last_msg = f"{EMO['xp']} +{gained} XP"
                    await _sleep_with_jitter(p.client.delay)
                    ok = await p.client.farm_gem_once()
                    if ok:
                        p.client.user.gems += 30
                        p.last_msg = f"{EMO['gem']} +30 gems"
                    base = int(time.time()) - 86400
                    ts = base - max(0, (p.client.user.streak - p.baseline.get('streak',0))) * 86400
                    ok, why = await p.client.streak_bump_once(ts)
                    if ok:
                        p.client.user.streak += 1
                        p.last_msg = f"{EMO['streak']} +1 streak"
                    else:
                        p.last_msg = f"{EMO['err']} streak: {why}"
                # kiểm tra target
                self._check_targets(p)
                await _sleep_with_jitter(p.client.delay)
        except Exception as e:
            p.last_msg = f"{EMO['err']} exception: {e}"; p.running = False
        finally:
            try:
                await p.client.get_user_info()
            except Exception:
                pass

    def _check_targets(self, p: Profile):
        u = p.client.user
        if not u:
            return
        done = False
        if p.goals.target_xp is not None and (u.totalXp - p.baseline.get("xp", 0)) >= p.goals.target_xp:
            done = True
        if p.goals.target_gems is not None and (u.gems - p.baseline.get("gems", 0)) >= p.goals.target_gems:
            done = True
        if p.goals.target_streak_inc is not None and (u.streak - p.baseline.get("streak", 0)) >= p.goals.target_streak_inc:
            done = True
        if done:
            p.running = False
            p.last_msg = f"{EMO['ok']} Targets reached"

    # ---------- API (optional) ---------- #
    def enable_api(self):
        if not Flask:
            return False
        app = Flask(__name__)
        self._api_app = app
        cli = self
        @app.get("/status")
        def status():
            return jsonify([
                {
                    "id": p.id,
                    "label": p.label,
                    "mode": p.mode,
                    "running": p.running,
                    "paused": p.paused,
                    "user": (p.client.user.username if p.client and p.client.user else None),
                    "xp": (p.client.user.totalXp if p.client and p.client.user else None),
                    "gems": (p.client.user.gems if p.client and p.client.user else None),
                    "streak": (p.client.user.streak if p.client and p.client.user else None),
                    "msg": p.last_msg,
                } for p in cli.profiles
            ])
        @app.post("/cmd")
        def cmd():
            data = request.json or {}
            cli._cmd_q.put_nowait(data.get("cmd", ""))
            return {"ok": True}
        def _run():
            app.run(port=API_PORT, debug=False, use_reloader=False)
        self._api_thread = threading.Thread(target=_run, daemon=True)
        self._api_thread.start()
        self.api_enabled = True
        return True

    # ---------- dashboard ---------- #
    def _render(self) -> Panel:
        tbl = Table(box=box.ROUNDED, expand=True)
        tbl.add_column("ID", justify="right")
        tbl.add_column("Profile")
        tbl.add_column("User")
        tbl.add_column("Mode")
        tbl.add_column("Langs")
        tbl.add_column(f"{EMO['streak']} Streak", justify="right")
        tbl.add_column(f"{EMO['xp']} XP", justify="right")
        tbl.add_column(f"{EMO['gem']} Gems", justify="right")
        tbl.add_column("Status")
        running_count = 0
        total_xp = 0
        total_gems = 0
        for p in self.profiles:
            u = p.client.user if p.client else None
            if p.running and not p.paused:
                running_count += 1
            if u:
                total_xp += max(0, u.totalXp - p.baseline.get("xp", 0))
                total_gems += max(0, u.gems - p.baseline.get("gems", 0))
            tbl.add_row(
                str(p.id),
                p.label,
                (u.username if u else "…"),
                (p.mode + (" (paused)" if p.paused else "")),
                (f"{u.fromLanguage}->{u.learningLanguage}" if u else "…"),
                str(u.streak if u else 0),
                str(u.totalXp if u else 0),
                str(u.gems if u else 0),
                p.last_msg or "",
            )
        head = f"{EMO['ok']} AutoFarm Python — {running_count} running | ΔXP={total_xp} ΔGems={total_gems}"
        help_line = "Commands: add | pause <id> | resume <id> | stop <id> | mode <id> <xp|gem|streak_farm|streak_repair|combo> | targets <id> xp=<n> gems=<n> streak+=<n> | api on | quit"
        return Panel(Align.center(tbl), title=head, subtitle=help_line)

    # ---------- input thread ---------- #
    def _read_input_thread(self):
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    time.sleep(0.1); continue
                self._cmd_q.put(line.strip())
            except Exception:
                break

    # ---------- command handler ---------- #
    async def _handle_cmd(self, cmd: str):
        if not cmd:
            return
        parts = cmd.split()
        if parts[0] == "quit":
            for p in self.profiles:
                p.running = False
            raise SystemExit
        if parts[0] == "add":
            label = f"profile{len(self.profiles)+1}"
            console.print("Nhập JWT token:", style="bold")
            jwt = sys.stdin.readline().strip()
            console.print("Chọn mode (xp/gem/streak_farm/streak_repair/combo):", style="bold")
            mode = sys.stdin.readline().strip() or "xp"
            console.print("Delay (s) [0.8]:", style="bold")
            try:
                delay = float(sys.stdin.readline().strip() or DEFAULT_DELAY)
            except Exception:
                delay = DEFAULT_DELAY
            p = self.add_profile(label, jwt, mode, delay)
            self.loop.create_task(self._profile_loop(p))
            return
        if parts[0] in ("pause","resume","stop") and len(parts) >= 2:
            try:
                pid = int(parts[1])
            except Exception:
                return
            p = next((x for x in self.profiles if x.id == pid), None)
            if not p:
                return
            if parts[0] == "pause":
                p.paused = True; p.last_msg = f"{EMO['pause']} paused"
            elif parts[0] == "resume":
                p.paused = False; p.last_msg = f"{EMO['resume']} resumed"
            else:
                p.running = False; p.last_msg = f"{EMO['stop']} stopped"
            return
        if parts[0] == "mode" and len(parts) >= 3:
            pid = int(parts[1]); new_mode = parts[2]
            p = next((x for x in self.profiles if x.id == pid), None)
            if p:
                p.mode = new_mode; p.last_msg = f"mode → {new_mode}"
            return
        if parts[0] == "targets" and len(parts) >= 2:
            pid = int(parts[1])
            p = next((x for x in self.profiles if x.id == pid), None)
            if not p:
                return
            for kv in parts[2:]:
                if kv.startswith("xp="):
                    p.goals.target_xp = int(kv.split("=",1)[1])
                elif kv.startswith("gems="):
                    p.goals.target_gems = int(kv.split("=",1)[1])
                elif kv.startswith("streak+="):
                    p.goals.target_streak_inc = int(kv.split("=",1)[1])
            p.last_msg = f"targets set"
            return
        if parts[0] == "api" and len(parts) >= 2 and parts[1] == "on":
            if self.enable_api():
                console.print(f"API http://127.0.0.1:{API_PORT} [/status, /cmd]", style="green")
            else:
                console.print("Không thể bật API (chưa cài Flask).", style="red")
            return

    # ---------- main ---------- #
    async def main(self):
        console.rule("[bold]Kin Hub AutoFarm")
        try:
            n = int(console.input("Nhập số profile: "))
        except Exception:
            n = 1
        for i in range(n):
            console.print(f"{EMO['user']} Profile {i+1} — nhập JWT token:", style="bold")
            jwt = sys.stdin.readline().strip()
            mode = console.input("Mode (xp/gem/streak_farm/streak_repair/combo): ") or "xp"
            try:
                delay = float(console.input("Delay (s) [0.8]: ") or DEFAULT_DELAY)
            except Exception:
                delay = DEFAULT_DELAY
            p = self.add_profile(f"profile{i+1}", jwt, mode, delay)
        # start workers
        for p in self.profiles:
            self.loop.create_task(self._profile_loop(p))
        # input thread
        t = threading.Thread(target=self._read_input_thread, daemon=True)
        t.start()
        # dashboard
        with Live(self._render(), console=console, refresh_per_second=int(1/REFRESH_INTERVAL)) as live:
            while True:
                # drain commands
                try:
                    while True:
                        cmd = self._cmd_q.get_nowait()
                        await self._handle_cmd(cmd)
                except queue.Empty:
                    pass
                live.update(self._render())
                await asyncio.sleep(REFRESH_INTERVAL)

# ===== Entrypoint ===== #

def main():
    app = AutoFarmCLI()
    try:
        app.loop.run_until_complete(app.main())
    except SystemExit:
        pass
    finally:
        for p in app.profiles:
            if p.client:
                try:
                    app.loop.run_until_complete(p.client.close())
                except Exception:
                    pass

if __name__ == "__main__":
    main()
