#!/bin/bash
# Kill any previous instances so the new code actually takes effect
# (otherwise app.py fails to bind port 5000 and the old process keeps serving).
pkill -f "cooldown/app.py"
pkill -f "mitmdump.*cooldown/addon.py"
sleep 1

sudo systemctl start redis
sleep 1
python3 ~/cooldown/app.py &
sleep 1
mitmdump -s ~/cooldown/addon.py --set http2=false
