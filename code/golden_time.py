# -*- coding: utf-8 -*-
"""
골든타임 지킴이 - 1분 뇌졸중 자가 체크 (노트북 프로그램, Python 3)

검사 순서 (음성 안내)
  1단계 팔 처짐 검사 10초   : 양손 마이크로비트 가속도 → 처짐 각도, 손바닥 회전 각도
  2단계 손가락 두드리기 10초 : 오른손은 A 버튼, 왼손은 B 버튼 연타 → 좌우 속도 비율
  3단계 얼굴 확인, 4단계 말 확인 : 본인 또는 가족이 버튼으로 응답
판정
  기록이 3회 미만이면 기본 기준, 정상 기록이 3회 이상 쌓이면 '개인 기준선'(평균 ± 표준편차)과 비교
  정상 → '마지막 정상 확인 시각' 기록 / 재검 → 1분 뒤 다시 / 위험 → 119·115 안내 + 골든타임 표시

실행
  pip3 install pyserial
  python3 golden_time.py            # 수신용 마이크로비트 자동 찾기
  python3 golden_time.py COM5       # 포트 직접 지정
마이크로비트 없이도 '시뮬레이션' 버튼으로 전체 흐름을 확인할 수 있다.
"""
import os
import platform
import queue
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

try:                         # 얼굴·말 자동 판단 (없으면 버튼으로 직접 판단)
    import auto_check as ac
except Exception:
    ac = None
try:
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None

# ───────── 설정 (오늘 실험 결과로 보정하기) ─────────
PREP_DRIFT = 9          # 팔 처짐 검사 전 자세 잡는 시간(초) - 음성 안내 길이 포함
PREP_TAP = 15           # 손가락 검사 전 준비 시간(초): 손마다 15초
TEST_SEC = 10           # 각 검사 시간(초)
GOLDEN_HOURS = 4.5      # 정맥 혈전용해 치료 가능 시간(마지막 정상 시각 기준)

LIMIT_DROP = 10.0       # 한쪽 팔 처짐 각도(°)
LIMIT_DIFF = 8.0        # 좌우 팔 처짐 차이(°)
LIMIT_ROT = 15.0        # 손바닥 안쪽 회전 각도(°)
LIMIT_TAP_RATIO = 0.70  # 느린 손 횟수 / 빠른 손 횟수
BASELINE_MIN = 3        # 개인 기준선을 쓰기 위한 최소 정상 기록 수
Z_LIMIT = 3.0           # 평소보다 표준편차 몇 배 이상 벗어나면 이상으로 볼지

RECEIVER_ID = "990536020005283386510139"   # 우리 팀 수신용 마이크로비트 고유번호 앞부분 (DETAILS.TXT)
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_time.db")
HAND_KO = {"L": "왼손", "R": "오른손"}
TAP_BTN = {"L": "B 버튼(화면 오른쪽 버튼)", "R": "A 버튼(화면 왼쪽 버튼)"}

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
FONT = "Malgun Gothic" if IS_WIN else ("Apple SD Gothic Neo" if IS_MAC else "NanumGothic")
COLORS = {"idle": "#1e293b", "step": "#1d4ed8", "run": "#0f766e",
          "ok": "#15803d", "warn": "#b45309", "bad": "#b91c1c"}
G_W, G_H, G_RANGE = 600, 250, 30     # 그래프 크기, 세로축 범위(±30°)


# ───────── 판정 로직 (화면과 분리해서 설명하기 쉽게) ─────────
def angle_change(samples):
    """samples: [(시간, 처짐각, 회전각)] → (처짐 변화, 회전 변화)
    처음 1초 평균 자세와 마지막 1초 평균 자세의 차이 (순간 흔들림은 평균으로 걸러냄)"""
    if len(samples) < 6:
        return None
    start, end = samples[0][0], samples[-1][0]
    # 처음 1초는 팔을 올리며 자세를 잡는 시간 → 1~2초 평균을 기준 자세로 사용
    first = [s for s in samples if start + 1.0 <= s[0] < start + 2.0]
    if len(first) < 3:
        first = [s for s in samples if s[0] <= start + 1.0]
    last = [s for s in samples if s[0] >= end - 1.0]
    dp = statistics.mean(s[1] for s in last) - statistics.mean(s[1] for s in first)
    dr = statistics.mean(s[2] for s in last) - statistics.mean(s[2] for s in first)
    return abs(dp), abs(dr)


def derive(m):
    """측정값 → 판정에 쓰는 4가지 지표"""
    fast = max(m["tap_l"], m["tap_r"])
    return {
        "drop_max": max(m["drop_l"], m["drop_r"]),
        "diff": abs(m["drop_l"] - m["drop_r"]),
        "rot_max": max(m["rot_l"], m["rot_r"]),
        "tap_ratio": min(m["tap_l"], m["tap_r"]) / fast if fast else 0.0,
    }


def judge(m, history):
    """m: 오늘 측정값, history: 같은 사람의 과거 '정상' 기록 지표 목록
    반환: (등급 '정상'/'재검'/'위험', 이유 목록, 지표)"""
    d = derive(m)
    weak = "왼" if m["drop_l"] >= m["drop_r"] else "오른"
    rot = "왼" if m["rot_l"] >= m["rot_r"] else "오른"
    slow = "왼" if m["tap_l"] <= m["tap_r"] else "오른"
    text = {
        "drop_max": f"{weak}팔 처짐 {d['drop_max']:.0f}°",
        "diff": f"좌우 팔 높이 차이 {d['diff']:.0f}°",
        "rot_max": f"{rot}손 안쪽 회전 {d['rot_max']:.0f}°",
        "tap_ratio": f"{slow}손 두드리기 느림 ({d['tap_ratio']:.0%})",
    }
    use_base = len(history) >= BASELINE_MIN
    flags = []

    # 각도 지표: 클수록 나쁨
    for key, limit in (("drop_max", LIMIT_DROP), ("diff", LIMIT_DIFF), ("rot_max", LIMIT_ROT)):
        x = d[key]
        if use_base:
            vals = [h[key] for h in history]
            mu, sd = statistics.mean(vals), max(statistics.pstdev(vals), 2.0)
            bad = (x - mu) / sd > Z_LIMIT or x > limit * 2      # 평소보다 크게 벗어남 / 안전 상한
        else:
            bad = x > limit
        if bad:
            flags.append(key)

    # 두드리기 비율: 작을수록 나쁨
    x = d["tap_ratio"]
    if use_base:
        vals = [h["tap_ratio"] for h in history]
        mu, sd = statistics.mean(vals), max(statistics.pstdev(vals), 0.05)
        bad = (mu - x) / sd > Z_LIMIT or x < 0.5
    else:
        bad = x < LIMIT_TAP_RATIO
    if bad:
        flags.append("tap_ratio")

    reasons = [text[k] for k in flags]
    if not m["speech_ok"]:
        reasons.insert(0, "말이 어눌함")
    if not m["face_ok"]:
        reasons.insert(0, "얼굴 한쪽 처짐")

    if not m["face_ok"] or not m["speech_ok"] or len(flags) >= 2:
        level = "위험"
    elif flags:
        level = "재검"
    else:
        level = "정상"
    d["mode"] = f"개인 기준선 적용 (정상 기록 {len(history)}회)" if use_base else "기본 기준 적용"
    return level, reasons, d


def fmt_td(td):
    s = max(int(td.total_seconds()), 0)
    return f"{s // 3600}시간 {s % 3600 // 60:02d}분 {s % 60:02d}초"


class Speaker:
    """운영체제 기본 음성으로 한국어를 읽는다 (추가 설치 필요 없음)"""

    def __init__(self):
        self.proc = None
        self.mac_voice = None
        if IS_MAC:
            try:
                voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
                if "Yuna" in voices:
                    self.mac_voice = "Yuna"
            except OSError:
                pass

    def say(self, text):
        print("[음성]", text)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        try:
            if IS_MAC:
                cmd = ["say"] + (["-v", self.mac_voice] if self.mac_voice else []) + [text]
                self.proc = subprocess.Popen(cmd)
            elif IS_WIN:
                safe = text.replace("'", "''")
                ps = ("Add-Type -AssemblyName System.Speech; "
                      "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet, "
                      "[System.Speech.Synthesis.VoiceAge]::NotSet, 0, "
                      "[System.Globalization.CultureInfo]'ko-KR') } catch {}; "
                      f"$s.Speak('{safe}')")
                self.proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                             creationflags=0x08000000)
        except OSError:
            pass

    def wait(self, timeout=15):
        """지금 읽고 있는 안내가 끝날 때까지 기다림 (녹음 전에 사용)"""
        end = time.time() + timeout
        while self.proc and self.proc.poll() is None and time.time() < end:
            time.sleep(0.1)

    @staticmethod
    def beep():
        try:
            if IS_MAC:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Ping.aiff"])
            elif IS_WIN:
                import winsound
                winsound.Beep(1200, 250)
        except Exception:
            pass


class App:
    def __init__(self, root, port=None):
        self.root = root
        self.q = queue.Queue()
        self.speaker = Speaker()
        self.ser = None
        self.sim = None            # None(실제 기기) / "normal" / "left_weak"
        self.phase = None          # "DRIFT" / "TAP" / None
        self.tap_hand = None
        self.t0 = 0.0
        self.samples = {"L": [], "R": []}
        self.taps = {"L": 0, "R": 0}
        self.drift = {}
        self.answers = {}
        self.q_key = None
        self.running = False
        self.last_yellow = None
        self.alarm_job = None
        self.alarm_reasons = []
        self.detail = []
        self.lines = {"L": None, "R": None}
        self.uiq = queue.Queue()        # 다른 스레드(녹음·음성 인식) → 화면 작업
        self.face = None                # 카메라 얼굴 측정기 (처음 쓸 때 준비)
        self.face_ratio = self.speech_score = self.heard = None

        self.db = sqlite3.connect(DB_FILE)
        self.db.execute("""CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT,
            drop_l REAL, drop_r REAL, rot_l REAL, rot_r REAL, tap_l INTEGER, tap_r INTEGER,
            face_ok INTEGER, speech_ok INTEGER, result TEXT, reasons TEXT)""")
        for col, typ in (("face_ratio", "REAL"), ("speech_score", "REAL"), ("heard", "TEXT")):
            try:
                self.db.execute(f"ALTER TABLE checks ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass                    # 이미 있는 열
        self.db.commit()

        self.user = tk.StringVar(value="할머니")
        auto_ok = bool(ac and ImageTk and (ac.FACE_READY or ac.SPEECH_READY))
        self.auto_var = tk.BooleanVar(value=auto_ok)
        self.build_ui()
        self.connect(port)
        self.idle()
        self.refresh()
        self.root.after(30, self.poll)

    # ───────── 화면 ─────────
    def build_ui(self):
        r = self.root
        r.title("골든타임 지킴이 - 1분 뇌졸중 자가 체크")
        r.geometry("1220x780")
        r.configure(bg="white")

        self.panel = tk.Frame(r, bg=COLORS["idle"], height=240)
        self.panel.pack(fill="x")
        self.panel.pack_propagate(False)
        self.lbl_main = tk.Label(self.panel, font=(FONT, 40, "bold"), fg="white", bg=COLORS["idle"])
        self.lbl_main.pack(pady=(40, 8))
        self.lbl_sub = tk.Label(self.panel, font=(FONT, 21), fg="white", bg=COLORS["idle"],
                                wraplength=1150, justify="center")
        self.lbl_sub.pack()

        bottom = tk.Frame(r, bg="#e2e8f0")
        bottom.pack(fill="x", side="bottom")
        self.btn_start = tk.Button(bottom, text="▶ 체크 시작", font=(FONT, 16, "bold"),
                                   command=lambda: self.start_check(None))
        self.btn_start.pack(side="left", padx=10, pady=8)
        tk.Button(bottom, text="시뮬레이션: 정상", font=(FONT, 12),
                  command=lambda: self.start_check("normal")).pack(side="left", padx=3)
        tk.Button(bottom, text="시뮬레이션: 왼팔 마비", font=(FONT, 12),
                  command=lambda: self.start_check("left_weak")).pack(side="left", padx=3)
        tk.Button(bottom, text="경보 해제", font=(FONT, 12),
                  command=self.clear_alarm).pack(side="left", padx=12)
        tk.Button(bottom, text="이 사용자 기록 초기화", font=(FONT, 12),
                  command=self.reset_user).pack(side="left", padx=3)
        tk.Checkbutton(bottom, text="얼굴·말 자동 판단", variable=self.auto_var, bg="#e2e8f0",
                       font=(FONT, 12)).pack(side="left", padx=10)
        self.lbl_conn = tk.Label(bottom, bg="#e2e8f0", font=(FONT, 11))
        self.lbl_conn.pack(side="right", padx=10)

        body = tk.Frame(r, bg="white")
        body.pack(fill="both", expand=True, padx=20, pady=12)

        left = tk.Frame(body, bg="white")
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="팔 처짐 실시간 그래프 (처음 자세 대비 각도 변화)",
                 font=(FONT, 14, "bold"), bg="white").pack(anchor="w")
        self.canvas = tk.Canvas(left, width=G_W, height=G_H, bg="#f8fafc", highlightthickness=0)
        self.canvas.pack(anchor="w", pady=4)
        self.cam_label = tk.Label(left, bg="black")     # 얼굴 확인 때만 그래프 자리에 표시
        self.lbl_detail = tk.Label(left, font=(FONT, 14), bg="white", justify="left", anchor="w")
        self.lbl_detail.pack(fill="x")
        qf = tk.Frame(left, bg="white")
        qf.pack(anchor="w", pady=8)
        self.btn_yes = tk.Button(qf, text="정상이에요", font=(FONT, 16), width=12, state="disabled",
                                 command=lambda: self.answer(True))
        self.btn_no = tk.Button(qf, text="이상해요", font=(FONT, 16), width=12, state="disabled",
                                command=lambda: self.answer(False))
        self.btn_yes.pack(side="left", padx=4)
        self.btn_no.pack(side="left", padx=4)

        right = tk.Frame(body, bg="white")
        right.pack(side="right", fill="y")
        row = tk.Frame(right, bg="white")
        row.pack(anchor="w")
        tk.Label(row, text="사용자:", font=(FONT, 14), bg="white").pack(side="left")
        ent = tk.Entry(row, textvariable=self.user, font=(FONT, 14), width=12)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self.refresh())
        ent.bind("<FocusOut>", lambda e: self.refresh())
        tk.Label(right, text="마지막 정상 확인 시각", font=(FONT, 15, "bold"), bg="white").pack(anchor="w", pady=(12, 0))
        self.lbl_lkw = tk.Label(right, font=(FONT, 26, "bold"), fg="#15803d", bg="white")
        self.lbl_lkw.pack(anchor="w")
        tk.Label(right, text="체크 기록 (DB)", font=(FONT, 15, "bold"), bg="white").pack(anchor="w", pady=(12, 0))
        self.logbox = tk.Listbox(right, width=46, height=12, font=(FONT, 12))
        self.logbox.pack()
        self.draw_axes()

    def gx(self, t):
        return 45 + min(t, TEST_SEC) / TEST_SEC * (G_W - 60)

    def gy(self, v):
        v = max(-G_RANGE, min(G_RANGE, v))
        return G_H / 2 - v / G_RANGE * (G_H / 2 - 12)

    def draw_axes(self):
        c = self.canvas
        c.delete("all")
        c.create_line(45, self.gy(0), G_W - 15, self.gy(0), fill="#94a3b8")
        for v in (LIMIT_DROP, -LIMIT_DROP):
            c.create_line(45, self.gy(v), G_W - 15, self.gy(v), fill="#ef4444", dash=(4, 3))
            c.create_text(40, self.gy(v), text=f"{v:+.0f}°", anchor="e", fill="#ef4444", font=(FONT, 9))
        c.create_text(40, self.gy(0), text="0°", anchor="e", fill="#64748b", font=(FONT, 9))
        c.create_text(G_W - 15, G_H - 6, text=f"{TEST_SEC}초", anchor="se", fill="#64748b", font=(FONT, 9))
        c.create_text(G_W - 15, 10, text="━ 왼손", anchor="ne", fill="#2563eb", font=(FONT, 11, "bold"))
        c.create_text(G_W - 15, 28, text="━ 오른손", anchor="ne", fill="#ea580c", font=(FONT, 11, "bold"))
        self.lines = {"L": None, "R": None}

    def plot(self, h):
        s = self.samples[h]
        if len(s) < 2:
            return
        base = s[0][1]
        pts = []
        for t, p, _ in s:
            pts += [self.gx(t), self.gy(p - base)]
        if self.lines[h] is None:
            color = "#2563eb" if h == "L" else "#ea580c"
            self.lines[h] = self.canvas.create_line(*pts, fill=color, width=3)
        else:
            self.canvas.coords(self.lines[h], *pts)

    def show(self, kind, main, sub=""):
        for w in (self.panel, self.lbl_main, self.lbl_sub):
            w.config(bg=COLORS[kind])
        self.lbl_main.config(text=main)
        self.lbl_sub.config(text=sub)

    def idle(self):
        self.show("idle", "골든타임 지킴이",
                  "매일 아침·저녁 1분 체크로 '마지막 정상 시각'을 기록하세요  →  [체크 시작]")

    def refresh(self):
        lkw = self.last_normal()
        self.lbl_lkw.config(text=lkw.strftime("%m/%d  %H:%M") if lkw else "기록 없음")
        self.logbox.delete(0, "end")
        for ts, res, dl, dr, tl, tr in self.db.execute(
                "SELECT ts, result, drop_l, drop_r, tap_l, tap_r FROM checks WHERE user=? "
                "ORDER BY id DESC LIMIT 12", (self.uname(),)):
            self.logbox.insert("end", f"{ts[5:16]}  {res}   처짐 왼{dl:.0f}° 오른{dr:.0f}°   두드리기 왼{tl} 오른{tr}")

    # ───────── 검사 진행 ─────────
    def start_check(self, sim):
        if self.running:
            return
        if sim is None and self.ser is None:
            self.show("warn", "마이크로비트가 연결되지 않았어요",
                      "수신용 마이크로비트를 USB로 꽂고 프로그램을 다시 켜거나, 시뮬레이션으로 체험해 보세요")
            return
        self.clear_alarm(silent=True)
        self.running, self.sim = True, sim
        self.samples, self.taps, self.answers, self.detail = {"L": [], "R": []}, {"L": 0, "R": 0}, {}, []
        self.face_ratio = self.speech_score = self.heard = None
        self.q_key = None
        self.hide_cam()
        self.draw_axes()
        self.lbl_detail.config(text="")
        self.btn_start.config(state="disabled")
        self.show("step", "1단계  팔 처짐 검사",
                  "양손에 기기를 쥐고, 손바닥을 위로 한 채 팔을 앞으로 쭉 뻗으세요. 그리고 눈을 감으세요.")
        self.speaker.say("팔 처짐 검사입니다. 양손에 기기를 쥐고, 손바닥을 위로 한 채 팔을 앞으로 쭉 뻗으세요. "
                         "그리고 눈을 감으세요.")
        self.countdown(PREP_DRIFT, "1단계  팔 처짐 검사", self.drift_run)

    def countdown(self, n, title, then):
        if not self.running:
            return
        if n <= 0:
            then()
            return
        self.lbl_main.config(text=f"{title}   {n}")
        self.root.after(1000, lambda: self.countdown(n - 1, title, then))

    def begin_phase(self, phase, hand, cmd):
        self.phase, self.tap_hand, self.t0 = phase, hand, time.time()
        self.send(cmd)
        if self.sim:
            self.root.after(100, self.sim_tick)

    def end_phase(self):
        self.phase = None
        self.send("STOP")

    def drift_run(self):
        self.show("run", f"그대로 버티세요 ({TEST_SEC}초)", "눈을 감고, 팔을 움직이지 마세요")
        self.speaker.say("시작. 그대로 버티세요.")
        self.begin_phase("DRIFT", None, "DRIFT")
        self.root.after(TEST_SEC * 1000, self.drift_end)

    def drift_end(self):
        self.end_phase()
        res = {h: angle_change(self.samples[h]) for h in "LR"}
        missing = [HAND_KO[h] for h in "LR" if res[h] is None]
        if missing:
            self.abort(f"{', '.join(missing)} 기기 신호가 없어요", "손 마이크로비트 전원과 라디오 그룹(23)을 확인하세요")
            return
        self.speaker.say("끝. 팔을 내리세요.")
        self.drift = res
        self.detail.append(f"팔 처짐   왼 {res['L'][0]:.1f}°  /  오른 {res['R'][0]:.1f}°")
        self.detail.append(f"손 회전   왼 {res['L'][1]:.1f}°  /  오른 {res['R'][1]:.1f}°")
        self.lbl_detail.config(text="\n".join(self.detail))
        self.root.after(2500, lambda: self.tap_prep("R"))

    def tap_prep(self, hand):
        if not self.running:
            return
        title = f"2단계  {HAND_KO[hand]} 두드리기"
        self.show("step", title, f"{HAND_KO[hand]}에 기기를 쥐고, 엄지로 {TAP_BTN[hand]}을 최대한 빨리 누르세요 ({TEST_SEC}초)")
        self.speaker.say(f"{HAND_KO[hand]} 검사입니다. 엄지로 {TAP_BTN[hand][:4]}을 최대한 빨리 누르세요.")
        self.countdown(PREP_TAP, title, lambda: self.tap_run(hand))

    def tap_run(self, hand):
        self.show("run", f"{HAND_KO[hand]}  0회", "계속 빠르게 누르세요!")
        self.speaker.say("시작!")
        self.begin_phase("TAP", hand, f"TAP_{hand}")
        self.root.after(TEST_SEC * 1000, lambda: self.tap_end(hand))

    def tap_end(self, hand):
        self.end_phase()
        if hand == "R":
            self.root.after(1500, lambda: self.tap_prep("L"))
            return
        if max(self.taps.values()) == 0:
            self.abort("버튼 입력이 없어요", "왼손 L은 B 버튼, 오른손 R은 A 버튼을 누르는지, 전원이 켜져 있는지 확인하세요")
            return
        self.detail.append(f"두드리기  왼 {self.taps['L']}회  /  오른 {self.taps['R']}회")
        self.lbl_detail.config(text="\n".join(self.detail))
        self.root.after(1500, self.ask_face)

    def ask(self, key, title, sub, voice):
        self.q_key = key
        self.show("step", title, sub)
        self.speaker.say(voice)
        self.btn_yes.config(state="normal")
        self.btn_no.config(state="normal")
        if self.sim:                       # 시뮬레이션에서는 자동으로 '정상' 응답
            self.root.after(2500, lambda: self.answer(True, key))

    def auto_on(self, kind):
        if self.sim or not self.auto_var.get() or ac is None:
            return False
        return (ac.FACE_READY and ImageTk is not None) if kind == "face" else ac.SPEECH_READY

    def note(self, line):
        self.detail.append(line)
        self.lbl_detail.config(text="\n".join(self.detail))

    # ── 3단계 얼굴 ──
    def ask_face(self):
        if self.auto_on("face"):
            self.face_auto()
        else:
            self.ask_face_manual()

    def ask_face_manual(self):
        self.ask("face", "3단계  얼굴 확인",
                 "거울을 보고 '이~' 하고 웃어 보세요. 입꼬리가 한쪽만 처지나요?  (본인 또는 가족이 응답)",
                 "얼굴 확인입니다. 거울을 보고 이 하고 웃어 보세요. 입꼬리가 한쪽만 처지나요?")

    def face_auto(self, retry=False, tries=0):
        if not self.running:
            return
        perm = ac.camera_permission()
        if perm == "ask":                       # 아직 안 물어봤으면 맥 허용 창을 띄우고 답을 기다림
            if tries == 0:
                ac.request_camera_permission()
                self.show("warn", "카메라 허용이 필요해요", "맥에 뜬 창에서 [허용]을 눌러 주세요")
                self.speaker.say("카메라 사용을 허용해 주세요.")
            if tries < 30:
                self.lbl_main.config(text=f"카메라 허용이 필요해요   {30 - tries}")
                self.root.after(1000, lambda: self.face_auto(retry, tries + 1))
                return
        elif perm == "denied":
            self.note("얼굴: 카메라 권한 꺼짐 (시스템 설정 → 개인정보 보호 및 보안 → 카메라) → 직접 판단")
            self.show("warn", "카메라 권한이 꺼져 있어요",
                      "시스템 설정 → 개인정보 보호 및 보안 → 카메라 → '터미널' 켜기 → 프로그램 다시 실행")
            self.speaker.say("카메라 권한이 꺼져 있어요. 이번에는 직접 판단으로 할게요.")
            self.root.after(3500, self.ask_face_manual)
            return
        try:
            if self.face is None:
                self.face = ac.FaceChecker()
            self.face.close()
            opened = self.face.open()
        except Exception as e:
            opened, self.face = False, None
            print("[카메라 오류]", e)
        if not opened:
            if tries < 15:      # 맥의 카메라 허용 창을 누를 시간을 줌 (15초)
                if tries == 0:
                    self.show("warn", "카메라 허용이 필요해요",
                              "맥에 뜬 창에서 [허용]을 누르세요.\n창이 없으면: 시스템 설정 → 개인정보 보호 및 보안 → 카메라 → '터미널' 켜기")
                    self.speaker.say("카메라 사용을 허용해 주세요.")
                self.lbl_main.config(text=f"카메라 허용이 필요해요   {15 - tries}")
                self.root.after(1000, lambda: self.face_auto(retry, tries + 1))
                return
            self.note("얼굴: 카메라 권한이 없어 직접 판단으로 전환")
            self.ask_face_manual()
            return
        self.q_key = "face_auto"
        self.face_data = {"neutral": [], "smile": []}
        self.canvas.pack_forget()
        self.cam_label.pack(anchor="w", pady=4, before=self.lbl_detail)
        self.face_phase, self.phase_t0, self.face_retry = "prep", time.time(), retry
        cam = getattr(self.face, "name", "") or "기본 카메라"
        self.show("step", "3단계  얼굴 확인 (자동)", f"카메라를 정면으로 보고 가만히 계세요\n(사용 중: {cam})")
        self.speaker.say("얼굴 확인입니다. 카메라를 정면으로 보고 가만히 계세요.")
        self.cam_tick()

    def cam_tick(self):
        if self.q_key != "face_auto" or not self.running:
            return
        img, m = self.face.read()
        if img is not None:
            pic = Image.fromarray(img)
            pic.thumbnail((G_W, 340))
            self.cam_photo = ImageTk.PhotoImage(pic)
            self.cam_label.config(image=self.cam_photo)
        t = time.time() - self.phase_t0
        ph = self.face_phase
        if m and ph in ("neutral", "smile"):
            self.face_data[ph].append(m)
        if ph == "prep" and t > 3.5:
            self.face_phase, self.phase_t0 = "neutral", time.time()
            self.show("run", "그대로 가만히", "평소 얼굴을 재는 중 (2초)")
        elif ph == "neutral" and t > 2.5:
            self.face_phase, self.phase_t0 = "smile", time.time()
            self.show("run", "'이~' 하고 크게 웃으세요!", "입꼬리를 양쪽으로 쭉 올려 주세요 (3초)")
            self.speaker.say("이 하고 크게 웃으세요.")
        elif ph == "smile" and t > 3.5:
            self.face_done()
            return
        self.root.after(40, self.cam_tick)

    def hide_cam(self):
        if self.face:
            self.face.close()
        self.cam_label.pack_forget()
        self.canvas.pack(anchor="w", pady=4, before=self.lbl_detail)

    def face_done(self):
        self.hide_cam()
        r = ac.judge_face(self.face_data["neutral"], self.face_data["smile"])
        if r["ok"] is None:
            if not self.face_retry:
                self.speaker.say(f"{r['reason']}. 한 번 더 해 볼게요.")
                self.show("warn", r["reason"], "한 번 더 해 볼게요")
                self.root.after(2500, lambda: self.face_auto(retry=True))
            else:
                self.note(f"얼굴: {r['reason']} → 직접 판단으로 전환")
                self.ask_face_manual()
            return
        self.q_key = None
        self.face_ratio = r["ratio"]
        self.answers["face"] = r["ok"]
        self.note(f"얼굴(자동)  {r['reason']}")
        if r["ok"]:
            self.show("ok", "얼굴 대칭 정상", r["reason"])
            self.speaker.say("얼굴은 정상입니다.")
        else:
            self.show("warn", "얼굴 한쪽이 처져 보여요", r["reason"])
            self.speaker.say(f"{r['weak']} 입꼬리가 덜 올라갔어요.")
        self.root.after(2200, self.ask_speech)

    # ── 4단계 말 ──
    def ask_speech(self):
        if not self.running:
            return
        if self.auto_on("speech"):
            self.speech_auto()
        else:
            self.ask_speech_manual()

    def ask_speech_manual(self):
        self.ask("speech", "4단계  말 확인",
                 "따라 읽어 보세요: “오늘은 날씨가 맑습니다.”  발음이 어눌하거나 말이 잘 안 나오나요?",
                 "말 확인입니다. 따라 읽어 보세요. 오늘은 날씨가 맑습니다.")

    def speech_auto(self, retry=False):
        self.q_key = "speech_auto"
        self.sentence = ac.pick_sentence()
        self.show("step", "4단계  말 확인 (자동)", f"‘삐’ 소리가 나면 따라 읽어 주세요\n“{self.sentence}.”")
        self.speaker.say(f"말 확인입니다. 삐 소리가 나면 따라 읽어 주세요. {self.sentence}.")
        threading.Thread(target=self.speech_worker, args=(retry,), daemon=True).start()

    def speech_worker(self, retry):
        """별도 스레드: 안내가 끝나면 녹음 → 음성 인식 (화면 작업은 uiq로 넘김)"""
        self.speaker.wait(15)
        time.sleep(0.3)
        self.speaker.beep()
        self.uiq.put(lambda: self.show("run", "지금 읽어 주세요", f"“{self.sentence}.”   (5초 녹음 중)"))
        heard, rms, err = "", 0.0, None
        try:
            raw, rms = ac.record()
            self.uiq.put(lambda: self.show("step", "듣고 있어요…", "발음을 분석하는 중"))
            heard = ac.recognize(raw) if rms > 120 else ""
        except Exception as e:
            err = str(e)
        self.uiq.put(lambda: self.speech_done(heard, rms, err, retry))

    def speech_done(self, heard, rms, err, retry):
        if self.q_key != "speech_auto" or not self.running:
            return
        if err:
            print("[음성 인식 오류]", err)
            self.note("말: 마이크·인터넷 문제로 직접 판단으로 전환")
            self.ask_speech_manual()
            return
        score = ac.speech_score(self.sentence, heard)
        if score < ac.SPEECH_SCORE_MIN and not retry:      # 한 번은 다시 기회
            msg = "잘 안 들렸어요" if not heard else "한 번 더 또박또박 읽어 주세요"
            self.show("warn", msg, f"들린 말: “{heard or '(없음)'}”")
            self.speaker.say(f"{msg}. 다른 문장으로 한 번 더 해 볼게요.")
            self.root.after(3500, lambda: self.speech_auto(retry=True))
            return
        ok = score >= ac.SPEECH_SCORE_MIN
        self.q_key = None
        self.speech_score, self.heard = score, heard
        self.answers["speech"] = ok
        self.note(f"말(자동)  발음 정확도 {score:.0%}  ·  들린 말: “{heard or '(없음)'}”")
        if ok:
            self.show("ok", f"발음 정확도 {score:.0%}", f"들린 말: “{heard}”")
            self.speaker.say("말도 정상입니다.")
        else:
            self.show("warn", f"발음 정확도 {score:.0%}", f"들린 말: “{heard or '(없음)'}”  → 말이 어눌하게 들려요")
            self.speaker.say("발음이 어눌하게 들려요.")
        self.root.after(2000, self.finish)

    def answer(self, ok, key=None):
        if not self.running or self.q_key is None or (key and key != self.q_key):
            return
        done = self.q_key
        self.answers[done] = ok
        self.q_key = None
        self.btn_yes.config(state="disabled")
        self.btn_no.config(state="disabled")
        if done == "face":
            self.ask_speech()
        else:
            self.finish()

    def finish(self):
        self.running = False
        self.btn_start.config(state="normal")
        m = {"drop_l": self.drift["L"][0], "drop_r": self.drift["R"][0],
             "rot_l": self.drift["L"][1], "rot_r": self.drift["R"][1],
             "tap_l": self.taps["L"], "tap_r": self.taps["R"],
             "face_ok": self.answers.get("face", True), "speech_ok": self.answers.get("speech", True)}
        level, reasons, d = judge(m, self.baseline_history())

        now = time.time()
        if level == "재검" and self.last_yellow and now - self.last_yellow < 15 * 60:
            level = "위험"
            reasons.append("재검에서도 이상 신호")
        self.last_yellow = now if level == "재검" else None

        self.db.execute(
            "INSERT INTO checks (ts, user, drop_l, drop_r, rot_l, rot_r, tap_l, tap_r, face_ok, speech_ok, "
            "result, reasons, face_ratio, speech_score, heard) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.uname(), m["drop_l"], m["drop_r"],
             m["rot_l"], m["rot_r"], m["tap_l"], m["tap_r"], int(m["face_ok"]), int(m["speech_ok"]),
             level, " / ".join(reasons), self.face_ratio, self.speech_score, self.heard))
        self.db.commit()
        self.detail.append(f"판정 기준: {d['mode']}")
        self.lbl_detail.config(text="\n".join(self.detail))
        self.refresh()

        if level == "정상":
            self.show("ok", "정상입니다 ✔",
                      f"마지막 정상 확인 시각이 {datetime.now():%H:%M}로 기록되었어요. 오늘도 건강하세요!")
            self.speaker.say("정상입니다. 마지막 정상 시각이 기록되었어요.")
            self.send("OK")
        elif level == "재검":
            self.show("warn", "재검이 필요해요",
                      f"{' · '.join(reasons)}\n1분 쉬고 다시 체크하세요. 다시 이상이 나오면 바로 병원에 연락하세요.")
            self.speaker.say("이상 신호가 하나 있어요. 1분 쉬고 다시 체크해 주세요.")
            self.send("WARN")
        else:
            self.alarm_reasons = reasons
            self.send("ALARM")
            self.speaker.say("뇌졸중이 의심됩니다. 지금 바로 119, 베트남에서는 115에 전화하세요.")
            self.alarm_tick()

    def alarm_tick(self):
        lkw = self.last_normal()
        if lkw:
            elapsed = datetime.now() - lkw
            remain = timedelta(hours=GOLDEN_HOURS) - elapsed
            golden = (f"골든타임 남은 시간(추정) {fmt_td(remain)}" if remain.total_seconds() > 0
                      else "추정 골든타임이 지났어도 지금 바로 병원으로!")
            t = f"마지막 정상 확인 {lkw:%H:%M} ({fmt_td(elapsed)} 전)  ·  {golden}"
        else:
            t = "마지막 정상 기록이 없어요 → 증상을 처음 본 시각을 의료진에게 꼭 알리세요"
        self.show("bad", "뇌졸중 의심!  지금 119 / 115 전화",
                  f"{' · '.join(self.alarm_reasons)}\n{t}")
        self.alarm_job = self.root.after(1000, self.alarm_tick)

    def clear_alarm(self, silent=False):
        if self.alarm_job:
            self.root.after_cancel(self.alarm_job)
            self.alarm_job = None
        if not silent and not self.running:
            self.idle()

    def abort(self, main, sub):
        self.end_phase()
        self.running = False
        self.q_key = None
        self.hide_cam()
        self.btn_start.config(state="normal")
        self.show("warn", main, sub)
        self.speaker.say(main)

    def reset_user(self):
        self.db.execute("DELETE FROM checks WHERE user=?", (self.uname(),))
        self.db.commit()
        self.last_yellow = None
        self.clear_alarm()
        self.refresh()

    # ───────── DB 도우미 ─────────
    def uname(self):
        return self.user.get().strip() or "사용자"

    def last_normal(self):
        row = self.db.execute("SELECT ts FROM checks WHERE user=? AND result='정상' ORDER BY id DESC LIMIT 1",
                              (self.uname(),)).fetchone()
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") if row else None

    def baseline_history(self):
        rows = self.db.execute(
            "SELECT drop_l, drop_r, rot_l, rot_r, tap_l, tap_r FROM checks WHERE user=? AND result='정상' "
            "ORDER BY id DESC LIMIT 14", (self.uname(),)).fetchall()
        keys = ("drop_l", "drop_r", "rot_l", "rot_r", "tap_l", "tap_r")
        return [derive(dict(zip(keys, row))) for row in rows]

    # ───────── 마이크로비트 통신 ─────────
    def connect(self, port):
        if serial is None:
            self.lbl_conn.config(text="pyserial 없음 → 시뮬레이션만 가능")
            return
        if port is None:
            keys = ("micro", "mbed", "daplink", "cmsis", "usbmodem")
            found = [p for p in serial.tools.list_ports.comports()
                     if any(k in f"{p.device} {p.description}".lower() for k in keys)]
            # 마이크로비트가 여러 대 꽂혀 있으면 수신용 고유번호를 가진 것을 먼저 고른다
            mine = [p for p in found if (p.serial_number or "").upper().startswith(RECEIVER_ID)]
            if mine:
                port = mine[0].device
            elif len(found) == 1:
                port = found[0].device
            elif len(found) > 1:
                self.lbl_conn.config(text="수신용 마이크로비트가 안 보여요 → '수신' 마이크로비트를 USB에 꽂고 다시 실행")
                return
        if port is None:
            self.lbl_conn.config(text="마이크로비트 없음 → 시뮬레이션만 가능")
            return
        try:
            self.ser = serial.Serial(port, 115200, timeout=1)
        except Exception as e:
            self.lbl_conn.config(text=f"{port} 연결 실패: {str(e)[:40]}")
            return
        self.lbl_conn.config(text=f"마이크로비트 연결됨: {port}")
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        while True:
            try:
                line = self.ser.readline().decode("utf-8", "ignore").strip()
            except Exception:
                self.q.put("__LOST__")
                return
            if line:
                self.q.put(line)

    def send(self, cmd):
        """라디오 신호가 가끔 빠지므로 손 기기에 가는 명령은 두 번 보낸다 (두 번 받아도 결과는 같음)"""
        if self.ser and not self.sim:
            try:
                times = 2 if cmd in ("DRIFT", "STOP", "TAP_L", "TAP_R") else 1
                for i in range(times):
                    self.ser.write((cmd + "\n").encode())
                    if i + 1 < times:
                        time.sleep(0.03)
            except Exception:
                pass

    def poll(self):
        while not self.q.empty():
            self.handle(self.q.get())
        while not self.uiq.empty():
            self.uiq.get()()
        self.root.after(30, self.poll)

    def handle(self, line):
        if line == "__LOST__":
            self.ser = None
            self.lbl_conn.config(text="마이크로비트 연결 끊김 → 프로그램 다시 실행")
            return
        p = line.split(",")
        try:
            if p[0] == "D" and len(p) == 4 and self.phase == "DRIFT" and p[1] in self.samples:
                self.samples[p[1]].append((time.time() - self.t0, float(p[2]), float(p[3])))
                self.plot(p[1])
            elif p[0] == "T" and len(p) == 3 and self.phase == "TAP" and p[1] == self.tap_hand:
                self.taps[p[1]] = int(p[2])
                self.lbl_main.config(text=f"{HAND_KO[p[1]]}  {p[2]}회")
        except ValueError:
            pass

    # ───────── 시뮬레이션 (기기 없이 시연/테스트) ─────────
    def sim_tick(self):
        if not self.sim or self.phase is None:
            return
        t = time.time() - self.t0
        k = min(t / TEST_SEC, 1.0)
        weak = self.sim == "left_weak"
        if self.phase == "DRIFT":
            for h in "LR":
                if weak and h == "L":
                    pitch, roll = -20 * k ** 1.3, 24 * k          # 왼팔이 점점 처지고 안쪽으로 돌아감
                else:
                    pitch, roll = -1.5 * k, 1.0 * k
                pitch += random.gauss(0, 0.7) + (3 if h == "L" else -2)
                roll += random.gauss(0, 0.7)
                self.q.put(f"D,{h},{pitch:.1f},{roll:.1f}")
        elif self.phase == "TAP":
            h = self.tap_hand
            rate = 2.6 if (weak and h == "L") else (5.6 if h == "R" else 5.0)
            self.q.put(f"T,{h},{int(t * rate)}")
        self.root.after(100, self.sim_tick)


if __name__ == "__main__":
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()
