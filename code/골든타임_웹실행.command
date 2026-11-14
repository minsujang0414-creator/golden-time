#!/bin/sh
# 더블클릭하면 골든타임 지킴이 웹 버전이 크롬에서 열립니다.
# (마이크로비트 연결·카메라·마이크를 쓰려면 http://localhost 주소로 열어야 해서 작은 웹서버를 함께 켭니다)
cd "$(dirname "$0")/web"
PORT=8777
if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
  python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &
  SERVER=$!
  sleep 1
fi
open -a "Google Chrome" "http://localhost:$PORT/" 2>/dev/null || open "http://localhost:$PORT/"
echo ""
echo "골든타임 지킴이 웹 버전이 크롬에서 열렸어요: http://localhost:$PORT/"
echo "이 창을 닫으면 웹서버도 꺼집니다. 다 쓰고 나서 닫으세요."
[ -n "$SERVER" ] && wait $SERVER
