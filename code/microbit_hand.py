# 골든타임 지킴이 - 손에 쥐는 마이크로비트 (2대: 왼손용 / 오른손용)
# python.microbit.org 에 붙여넣고 올리기
# 오른손용으로 올릴 때는 아래 HAND = "L" 을 HAND = "R" 로 바꾸기
#
# 보내는 메시지
#   D,손,처짐각,회전각   팔 처짐 검사 중 0.1초마다
#   T,손,누른횟수        손가락 두드리기 검사 중 버튼(왼손 B, 오른손 A)을 누를 때마다
from microbit import *
import radio
import math

HAND = "L"
RAD2DEG = 57.2958
# 두드리기 버튼: 왼손은 B(화면 오른쪽 버튼, 왼손 엄지가 닿기 쉬움), 오른손은 A(화면 왼쪽 버튼)
TAP_BTN = button_b if HAND == "L" else button_a

radio.config(group=23, length=64, power=7)   # 받는 쪽과 그룹 번호가 같아야 함
radio.on()
display.show(HAND)

mode = ""
last_send = 0
count = 0


def angles():
    # 중력 방향으로 기울기 계산: 화면을 위로 해서 평평하게 쥐면 둘 다 0도
    x = accelerometer.get_x()
    y = accelerometer.get_y()
    z = accelerometer.get_z()
    pitch = math.atan2(y, math.sqrt(x * x + z * z)) * RAD2DEG   # 앞뒤로 기울어짐 (팔 처짐)
    roll = math.atan2(x, math.sqrt(y * y + z * z)) * RAD2DEG    # 좌우로 돌아감 (손바닥 회전)
    return pitch, roll


while True:
    msg = radio.receive()
    if msg == "DRIFT":
        mode = "DRIFT"
        display.show(Image.SQUARE)
    elif msg == "TAP_" + HAND:
        mode = "TAP"
        count = 0
        TAP_BTN.get_presses()           # 이전에 눌린 횟수 비우기
        display.show(Image.TARGET)
    elif msg == "STOP":
        mode = ""
        display.show(HAND)

    if mode == "DRIFT" and running_time() - last_send >= 100:
        last_send = running_time()
        p, r = angles()
        radio.send("D," + HAND + "," + str(round(p, 1)) + "," + str(round(r, 1)))
    elif mode == "TAP":
        n = TAP_BTN.get_presses()
        if n:
            count += n
            radio.send("T," + HAND + "," + str(count))
            display.show(str(count % 10))
    sleep(10)
