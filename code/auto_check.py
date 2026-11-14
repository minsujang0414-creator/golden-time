# -*- coding: utf-8 -*-
"""
골든타임 지킴이 - 얼굴·말 자동 판단 모듈 (혼자 사는 사람을 위해)

얼굴 (카메라, 노트북 안에서만 처리)
  1) 가만히 있는 얼굴과 2) '이~' 하고 웃는 얼굴을 비교해서
  왼쪽/오른쪽 입꼬리가 각각 얼마나 올라갔는지 잰다.
  한쪽만 덜 올라가면(좌우 비율이 낮으면) '얼굴 한쪽 처짐'으로 본다.
  - MediaPipe Face Mesh: 얼굴 위 468개 점을 찾아 주는 AI 모델
  - 눈꼬리 두 점을 잇는 선을 기준으로 삼아 고개가 기울어도 높이를 공정하게 비교
  - 눈 사이 거리로 나눠서 카메라와의 거리가 달라도 같은 기준으로 비교

말 (마이크, 음성 인식은 Google 음성 인식 서버 사용 → 인터넷 필요)
  화면의 문장을 따라 읽으면 음성 인식 결과와 원래 문장을 자모(ㅇ,ㅗ,ㄴ...) 단위로 비교해
  발음 정확도(%)를 낸다. 정확도가 낮으면 '말이 어눌함'으로 본다.
"""
import difflib
import math
import random
import unicodedata

try:
    import cv2
    import numpy as np
    import mediapipe as mp
    FACE_READY, FACE_ERROR = True, ""
except Exception as e:          # 라이브러리가 없으면 수동 판단으로 대체
    FACE_READY, FACE_ERROR = False, str(e)

try:                     # 맥: 카메라 목록·권한 확인 (아이폰 연속성 카메라 말고 맥북 카메라 고르기)
    import AVFoundation as AV
except Exception:
    AV = None

try:
    import io
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    import speech_recognition as sr

    def _flac_data(self, convert_rate=None, convert_width=None):
        """음성 인식 라이브러리에 딸린 flac 변환 프로그램이 애플 실리콘 맥에서 안 돌아서 직접 변환"""
        raw = self.get_raw_data(convert_rate, 2)
        buf = io.BytesIO()
        sf.write(buf, np.frombuffer(raw, dtype=np.int16), convert_rate or self.sample_rate,
                 format="FLAC", subtype="PCM_16")
        return buf.getvalue()

    sr.AudioData.get_flac_data = _flac_data
    SPEECH_READY, SPEECH_ERROR = True, ""
except Exception as e:
    SPEECH_READY, SPEECH_ERROR = False, str(e)

# ───────── 기준값 (오늘 실험으로 보정하기) ─────────
FACE_RATIO_MIN = 0.55     # 약한 쪽 입꼬리 상승 / 강한 쪽 입꼬리 상승 이 값보다 작으면 이상
FACE_MIN_LIFT = 0.025     # 이보다 덜 올라가면 '웃음이 감지되지 않음' (눈 사이 거리 대비)
SPEECH_SCORE_MIN = 0.75   # 발음 정확도 75% 미만이면 이상
RECORD_SEC = 5.0          # 녹음 시간
RATE = 16000

SENTENCES = [
    "오늘은 날씨가 맑습니다",
    "나는 아침마다 물을 마십니다",
    "우리 집 강아지는 귀엽습니다",
    "할머니가 시장에 가십니다",
]

# 얼굴 점 번호 (MediaPipe Face Mesh)
R_EYE, L_EYE = 33, 263          # 오른쪽/왼쪽 눈 바깥 꼬리 (본인 기준)
R_MOUTH, L_MOUTH = 61, 291      # 오른쪽/왼쪽 입꼬리 (본인 기준)


def pick_sentence():
    return random.choice(SENTENCES)


# ───────── 말: 발음 정확도 ─────────
def to_jamo(text):
    """'맑습니다' → 'ㅁㅏㄺㅅㅡㅂㄴㅣㄷㅏ' 처럼 자모 단위로 분해 (띄어쓰기·문장부호 제거)"""
    only = "".join(ch for ch in text if ch.isalnum())
    return unicodedata.normalize("NFD", only)


def speech_score(target, heard):
    """원래 문장과 들린 문장의 자모 유사도 (0~1)"""
    if not heard:
        return 0.0
    return difflib.SequenceMatcher(None, to_jamo(target), to_jamo(heard)).ratio()


def builtin_mic():
    """맥북 내장 마이크 번호 (아이폰 연속성 마이크 말고). 못 찾으면 None = 기본 마이크"""
    try:
        mics = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return None
    for i, name in mics:
        if any(k in name.lower() for k in ("macbook", "built-in", "내장")):
            return i
    for i, name in mics:
        if not any(k in name for k in ("‘", "’", "iPhone", "연속성")):
            return i
    return None


def record(seconds=RECORD_SEC):
    """마이크로 녹음 → (16bit 원본 바이트, 소리 크기 RMS)"""
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="int16", device=builtin_mic())
    sd.wait()
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return audio.tobytes(), rms


def recognize(raw):
    """녹음 → 한국어 문장. 못 알아들으면 '' / 인터넷 문제는 RuntimeError"""
    data = sr.AudioData(raw, RATE, 2)
    try:
        return sr.Recognizer().recognize_google(data, language="ko-KR")
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        raise RuntimeError(f"음성 인식 서버 연결 실패: {e}")


# ───────── 카메라 고르기·권한 (맥) ─────────
def _video_devices():
    """OpenCV(맥)와 같은 순서의 카메라 목록: 영상+복합 장치를 고유 ID(uniqueID) 순으로 정렬"""
    devs = list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo))
    devs += list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeMuxed))
    return sorted(devs, key=lambda d: str(d.uniqueID()))


def builtin_camera():
    """(OpenCV 카메라 번호, 이름): 맥북 내장 카메라를 찾는다. 못 찾으면 (0, '')"""
    if AV is None:
        return 0, ""
    try:
        devs = _video_devices()
        for i, d in enumerate(devs):
            if d.deviceType() == AV.AVCaptureDeviceTypeBuiltInWideAngleCamera:
                return i, str(d.localizedName())
    except Exception:
        pass
    return 0, ""


def camera_permission():
    """'allowed' / 'denied' / 'ask' (아직 안 물어봄) / 'unknown'(맥이 아님)"""
    if AV is None:
        return "unknown"
    st = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo)
    return {0: "ask", 1: "denied", 2: "denied", 3: "allowed"}.get(int(st), "unknown")


def request_camera_permission():
    """맥의 '카메라 접근 허용' 창을 띄운다 (결과는 camera_permission()으로 다시 확인)"""
    if AV is not None:
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeVideo, lambda ok: None)


# ───────── 얼굴: 입꼬리 좌우 대칭 ─────────
class FaceChecker:
    def __init__(self):
        self.cap = None
        self.mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=False,
            min_detection_confidence=0.5, min_tracking_confidence=0.5)

    def open(self):
        self.index, self.name = builtin_camera()
        if AV is not None:
            self.cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        else:
            self.cap = cv2.VideoCapture(self.index)
        return self.cap.isOpened()

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def read(self):
        """카메라 한 장 → (화면에 보여줄 RGB 이미지, 측정값 (오른쪽 높이, 왼쪽 높이) 또는 None)"""
        ok, img = self.cap.read()
        if not ok:
            return None, None
        h, w = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = self.mesh.process(rgb)
        m = None
        if res.multi_face_landmarks:
            lm = res.multi_face_landmarks[0].landmark
            pts = {i: (lm[i].x * w, lm[i].y * h) for i in (R_EYE, L_EYE, R_MOUTH, L_MOUTH)}
            m = mouth_heights(pts)
            for i in (R_EYE, L_EYE):
                cv2.circle(rgb, tuple(int(v) for v in pts[i]), 4, (80, 160, 255), -1)
            for i in (R_MOUTH, L_MOUTH):
                cv2.circle(rgb, tuple(int(v) for v in pts[i]), 6, (255, 90, 40), -1)
            cv2.line(rgb, tuple(int(v) for v in pts[R_EYE]), tuple(int(v) for v in pts[L_EYE]), (80, 160, 255), 2)
        return cv2.flip(rgb, 1), m       # 거울처럼 보이게 좌우 반전 (측정은 반전 전에 끝남)


def mouth_heights(pts):
    """눈꼬리 선을 수평으로 돌린 뒤, 눈 선에서 각 입꼬리까지의 세로 거리 / 눈 사이 거리"""
    (rx, ry), (lx, ly) = pts[R_EYE], pts[L_EYE]
    iod = math.hypot(lx - rx, ly - ry)
    ang = math.atan2(ly - ry, lx - rx)
    cx, cy = (rx + lx) / 2, (ry + ly) / 2
    cos, sin = math.cos(-ang), math.sin(-ang)

    def rel_y(p):
        x, y = p[0] - cx, p[1] - cy
        return (x * sin + y * cos) / iod      # 회전 후 y (아래가 +)

    return rel_y(pts[R_MOUTH]), rel_y(pts[L_MOUTH])


def judge_face(neutral, smile):
    """neutral, smile: [(오른쪽 높이, 왼쪽 높이), ...]
    반환: dict(ok, ratio, lift_r, lift_l, weak, reason)"""
    if len(neutral) < 5 or len(smile) < 5:
        return {"ok": None, "reason": "얼굴을 찾지 못했어요"}
    n_r = float(np.median([m[0] for m in neutral]))
    n_l = float(np.median([m[1] for m in neutral]))
    lifts = sorted(((n_r - m[0], n_l - m[1]) for m in smile), key=lambda t: t[0] + t[1], reverse=True)
    top = lifts[:max(3, len(lifts) // 3)]           # 가장 활짝 웃은 순간들
    lift_r = float(np.median([t[0] for t in top]))
    lift_l = float(np.median([t[1] for t in top]))
    big, small = max(lift_r, lift_l), min(lift_r, lift_l)
    if big < FACE_MIN_LIFT:
        return {"ok": None, "reason": "웃음이 감지되지 않았어요", "lift_r": lift_r, "lift_l": lift_l}
    ratio = max(small, 0.0) / big
    weak = "오른쪽" if lift_r < lift_l else "왼쪽"
    ok = ratio >= FACE_RATIO_MIN
    reason = f"입꼬리 좌우 비율 {ratio:.0%}" + ("" if ok else f" ({weak} 입꼬리가 덜 올라감)")
    return {"ok": ok, "ratio": ratio, "lift_r": lift_r, "lift_l": lift_l, "weak": weak, "reason": reason}
