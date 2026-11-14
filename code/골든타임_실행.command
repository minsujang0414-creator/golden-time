#!/bin/sh
# 더블클릭하면 골든타임 지킴이 프로그램이 켜집니다 (최신 파이썬 3.12 + 카메라·음성 인식 라이브러리)
cd "$(dirname "$0")"
"$HOME/.local/bin/uv" run --python 3.12 \
  --with pyserial --with "mediapipe==0.10.21" --with opencv-python --with pillow \
  --with sounddevice --with soundfile --with SpeechRecognition --with pyobjc-framework-AVFoundation \
  python golden_time.py "$@"
