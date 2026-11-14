# 골든타임 지킴이 - 노트북에 USB로 꽂는 수신용 마이크로비트 (1대)
# 손 마이크로비트의 라디오 메시지 → 노트북(시리얼)으로 전달
# 노트북 명령(DRIFT, TAP_L, TAP_R, STOP) → 라디오로 손 마이크로비트에 전달
# 노트북 결과(OK, WARN, ALARM) → 화면 아이콘과 소리
from microbit import *
import radio
import music

radio.config(group=23, length=64, power=7)
radio.on()
display.show(Image.HEART)
buf = ""

while True:
    msg = radio.receive()
    if msg:
        print(msg)

    if uart.any():
        data = uart.read()
        if data:
            buf += str(data, "utf-8")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line == "ALARM":
                    display.show(Image.NO)
                    music.play(["C6:2", "R:1"] * 6, wait=False)
                elif line == "OK":
                    display.show(Image.YES)
                elif line == "WARN":
                    display.show(Image.CONFUSED)
                elif line:
                    radio.send(line)
    sleep(5)
