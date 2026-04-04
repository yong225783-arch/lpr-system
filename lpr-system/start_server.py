#!/usr/bin/env python3
import os
os.environ['FLASK_DEBUG'] = 'false'

from werkzeug.serving import run_simple
from main import app

print("=" * 50)
print("  車牌辨識開門系統")
print("  網頁管理: http://localhost:5000")
print("  預設帳號: admin")
print("  預設密碼: admin123")
print("=" * 50)

run_simple('0.0.0.0', 5000, app, use_reloader=False, threaded=True)
