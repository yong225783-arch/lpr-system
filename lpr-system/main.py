"""
車牌辨識開門系統 - 主程式
Flask + OpenCV + EasyOCR + USB Relay
"""

import os
import sys
import io
import time
import shutil
import logging
import threading
import cv2
import numpy as np
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, send_file, flash, make_response
)
from werkzeug.security import generate_password_hash, check_password_hash

# 設定時區為台北
os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset()
except:
    pass
import database as db

# ============ 登入 Rate Limiting ============

login_attempts = {}  # {IP: (count, last_attempt_time)}
alerts = []  # 系統通知列表 [{type, message, timestamp, read}]

def add_alert(type, message):
    """新增系統通知"""
    now = datetime.now()
    alerts.insert(0, {
        'type': type,  # 'danger', 'warning', 'info'
        'message': message,
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'read': False
    })
    # 只保留最近 50 條通知
    if len(alerts) > 50:
        alerts.pop()

def check_login_rate_limit(ip):
    """檢查 IP 是否被限制"""
    now = time.time()
    if ip in login_attempts:
        count, last_time = login_attempts[ip]
        # 超過 5 分鐘重置
        if now - last_time > 300:
            login_attempts[ip] = (0, now)
            return True
        # 5 分鐘內超過 5 次嘗試
        if count >= 5:
            remaining = int(300 - (now - last_time))
            return False, remaining
    return True

def record_failed_login(ip):
    """記錄失敗的登入嘗試"""
    now = time.time()
    if ip in login_attempts:
        count, last_time = login_attempts[ip]
        if now - last_time > 300:
            login_attempts[ip] = (1, now)
        else:
            login_attempts[ip] = (count + 1, now)
    else:
        login_attempts[ip] = (1, now)

def clear_login_attempts(ip):
    """清除登入嘗試記錄（成功後）"""
    if ip in login_attempts:
        del login_attempts[ip]

def engineer_required(f):
    """裝飾器：需要工程商密碼驗證"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        engineer_hash = db.get_setting('engineer_password_hash', '')
        if engineer_hash and not session.get('engineer_mode'):
            return jsonify({'success': False, 'error': '需要工程商驗證'}), 403
        return f(*args, **kwargs)
    return decorated

# ============ 資料庫備份 ============

def backup_database():
    """自動備份資料庫"""
    try:
        backup_dir = 'backups'
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_file = os.path.join(backup_dir, f'lpr_backup_{timestamp}.db')
        
        # 複製資料庫
        shutil.copy2('lpr.db', backup_file)
        
        # 只保留最近 10 個備份
        backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])
        while len(backups) > 10:
            oldest = backups.pop(0)
            os.remove(os.path.join(backup_dir, oldest))
            logger.info(f'刪除舊備份: {oldest}')
        
        logger.info(f'資料庫已備份: {backup_file}')
        return True
    except Exception as e:
        logger.error(f'資料庫備份失敗: {e}')
        return False

def restore_database(backup_file):
    """從備份還原資料庫"""
    try:
        shutil.copy2(backup_file, 'lpr.db')
        # 確保索引和遷移在還原後重新套用
        db.init_db()
        logger.info(f'資料庫已還原: {backup_file}')
        return True
    except Exception as e:
        logger.error(f'資料庫還原失敗: {e}')
        return False


# Ulrixon OCR 模型類別（36 類：0-9, A-Z, I/O 排除）
OCR_CLASSES = ['-', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
               'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K', 'L', 'M',
               'N', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z']

# ============ Flask App ============

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32).hex())
app.config['SESSION_COOKIE_SECURE'] = False  # 區網 HTTP 相容
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# ============ Context Processor ============

@app.context_processor
def inject_project_name():
    """所有模板都注入 project_name"""
    return {
        'project_name': db.get_setting('project_name', '車牌辨識開門系統')
    }

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
try:
    from logging.handlers import RotatingFileHandler
    log_file = os.environ.get('LOG_FILE', 'lpr.log')
    rh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    rh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logging.root.addHandler(rh)
except Exception:
    pass
logger = logging.getLogger(__name__)

# ============ LPR Module ============

class PlateRecognizer:
    def __init__(self):
        self.camera_in = None   # 進口攝影機
        self.camera_out = None  # 出口攝影機
        self.running = False
        self.thread = None
        self.last_plate = None
        self.last_plate_time = 0
        self.cooldown = 5  # 同車牌冷卻時間（秒）

    def set_camera(self, source=0, mode='in'):
        """
        設定攝像頭
        source: int = USB webcam 編號
                str = RTSP URL, 例如 'rtsp://admin:password@192.168.1.100:554/stream1'
        mode: 'in' = 進口, 'out' = 出口
        """
        camera = cv2.VideoCapture(source)
        
        if not camera.isOpened():
            logger.error(f'無法開啟攝影機 {source}')
            return False
        
        if mode == 'in':
            self.camera_in = camera
            logger.info(f'進口攝影機已連接: {source}')
        else:
            self.camera_out = camera
            logger.info(f'出口攝影機已連接: {source}')
        
        return True
    
    def get_camera(self, mode='in'):
        """取得指定模式的攝影機"""
        return self.camera_in if mode == 'in' else self.camera_out

    def preprocess(self, frame):
        """影像前處理"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.bilateralFilter(gray, 11, 17, 17)
        edged = cv2.Canny(blur, 30, 200)
        return edged

    def find_plate_contour(self, edged):
        """找可能的车牌区域"""
        contours, _ = cv2.findContours(
            edged.copy(), cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]

        plate_contour = None
        for c in contours:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.018 * peri, True)
            if len(approx) == 4:
                plate_contour = approx
                break
        return plate_contour

    def _apply_roi(self, frame, mode='in'):
        """根據 ROI 設定裁切畫面（支援多區域，取第一區）"""
        import json
        key = f'camera_{mode}_roi'
        roi_str = db.get_setting(key, '')
        if not roi_str:
            return frame
        try:
            data = json.loads(roi_str)
            # 支援新格式（zones 列表）和舊格式（直接 roi 物件）
            if isinstance(data, dict) and 'zones' in data:
                zones = data['zones']
            elif isinstance(data, list):
                zones = data
            else:
                zones = [data]

            if not zones:
                return frame
            roi = zones[0]
            x = int(roi.get('x', 0))
            y = int(roi.get('y', 0))
            w = int(roi.get('w', 100))
            h = int(roi.get('h', 100))
            if w >= 100 and h >= 100 and x == 0 and y == 0:
                return frame
            h_img, w_img = frame.shape[:2]
            x1 = int(w_img * x / 100)
            y1 = int(h_img * y / 100)
            x2 = int(w_img * (x + w) / 100)
            y2 = int(h_img * (y + h) / 100)
            x1, x2 = max(0, x1), min(w_img, x2)
            y1, y2 = max(0, y1), min(h_img, y2)
            return frame[y1:y2, x1:x2]
        except:
            return frame

    def process_frame(self, frame):
        """處理單一幀，嘗試辨識車牌"""
        edged = self.preprocess(frame)
        plate_contour = self.find_plate_contour(edged)

        if plate_contour is not None:
            mask = np.zeros_like(edged)
            cv2.drawContours(mask, [plate_contour], 0, 255, -1)
            cv2.drawContours(frame, [plate_contour], -1, (0, 255, 0), 3)

        return frame, plate_contour

    def save_capture(self, frame, plate_text):
        """儲存捕捉的畫面"""
        filename = f"{plate_text}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        filepath = os.path.join('captures', filename)
        os.makedirs('captures', exist_ok=True)
        cv2.imwrite(filepath, frame)
        return filepath

    def capture_and_recognize(self, mode='in'):
        """擷取並辨識一次"""
        camera = self.get_camera(mode)
        if not camera or not camera.isOpened():
            return None

        ret, frame = camera.read()
        if not ret:
            return None

        frame = self._apply_roi(frame, mode)
        return frame

    def start_continuous(self, mode='in', callback=None):
        """連續偵測執行緒"""
        def run():
            camera = self.get_camera(mode)
            reconnect_failures = 0
            while self.running:
                if not camera or not camera.isOpened():
                    camera = self.get_camera(mode)
                    reconnect_failures += 1
                    if reconnect_failures > 50:
                        logger.warning(f'攝影機 {mode} 連線失敗，已停止嘗試')
                        break
                    time.sleep(0.5)
                    continue
                ret, frame = camera.read()
                if not ret:
                    reconnect_failures += 1
                    if reconnect_failures > 10:
                        logger.warning(f'攝影機 {mode} 讀取失敗，嘗試重連...')
                        camera = self.get_camera(mode)
                        reconnect_failures = 0
                    time.sleep(0.2)
                    continue
                reconnect_failures = 0
                frame = self._apply_roi(frame, mode)

                # 車牌辨識流程
                processed, plate = self.process_frame(frame)

                if plate is not None:
                    now = time.time()
                    if now - self.last_plate_time > self.cooldown:
                        self.last_plate_time = now
                        if callback:
                            callback(frame, plate)

                time.sleep(0.05)

        self.running = True
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.camera_in:
            self.camera_in.release()
        if self.camera_out:
            self.camera_out.release()

# ============ 初始化 ============

# 資料庫
db.init_db()
logger.info('資料庫初始化完成')

# 車牌辨識器
lpr = PlateRecognizer()

# 從資料庫讀取攝影機設定
camera_in_url = db.get_setting('camera_in_url', '')
camera_out_url = db.get_setting('camera_out_url', '')

# 嘗試開啟進口攝影機
if camera_in_url:
    if not lpr.set_camera(camera_in_url, 'in'):
        logger.warning('無法開啟進口攝影機')
else:
    logger.info('未設定進口攝影機')

# 嘗試開啟出口攝影機
if camera_out_url:
    if not lpr.set_camera(camera_out_url, 'out'):
        logger.warning('無法開啟出口攝影機')
else:
    logger.info('未設定出口攝影機')

# 如果都沒有設定，使用預設 webcam 0
if not camera_in_url and not camera_out_url:
    lpr.set_camera(0, 'in')
    logger.info('使用預設 Webcam 0 作為進口攝影機')

# 嘗試連接繼電器
relay_type = os.environ.get('RELAY_TYPE', '')
relay_port = os.environ.get('RELAY_PORT', None)
simulate_relay = os.environ.get('SIMULATE_RELAY', '').lower() in ('1', 'true', 'yes')

if simulate_relay:
    from relay import RelayController
    relay = RelayController(simulate=True)
    logger.warning('⚠️  繼電器：模擬模式（無硬體）— 正式環境請設 SIMULATE_RELAY=0')
elif relay_type == 'modbus_tcp':
    relay_modbus_ip = db.get_setting('relay_modbus_ip', '')
    relay_modbus_port = int(db.get_setting('relay_modbus_port', 502))
    relay_modbus_coil = int(db.get_setting('relay_modbus_coil', 0))
    if relay_modbus_ip:
        from relay import ModbusTCPController
        relay = ModbusTCPController(ip=relay_modbus_ip, port=relay_modbus_port, coil=relay_modbus_coil)
        logger.info(f'繼電器：Modbus TCP {relay_modbus_ip}:{relay_modbus_port} coil={relay_modbus_coil}')
    else:
        from relay import RelayController
        relay = RelayController(simulate=True)
        logger.info('繼電器：模擬模式（無 IP）')
elif relay_port:
    from relay import RelayController
    relay = RelayController(port=relay_port)
    if relay.connect():
        logger.info(f'繼電器已連接: {relay_port}')
    else:
        relay = None
        logger.warning('繼電器連接失敗，開門功能將無法使用')
else:
    from relay import RelayController
    relay = RelayController(simulate=True)
    logger.warning('⚠️  繼電器：模擬模式（無硬體）— 請設定 RELAY_PORT 或 RELAY_TYPE')

# ============ 攝影機串流 / 截圖 ============

@app.route('/video_feed/<mode>')
def video_feed(mode='in'):
    """回傳即時影像（每次請求回傳一幀）"""
    if 'user_id' not in session:
        return '', 401
    
    camera = lpr.get_camera(mode)
    if camera and camera.isOpened():
        ret, frame = camera.read()
        if ret:
            # 儲存到記憶體
            _, buffer = cv2.imencode('.jpg', frame)
            return buffer.tobytes(), 200, {'Content-Type': 'image/jpeg'}
    
    # 如果沒有攝影機，回傳預設圖片
    return '', 404

@app.route('/video_feed.jpg/<mode>')
def video_feed_jpg(mode='in'):
    """回傳即時影像 JPG（可用於 img src）"""
    camera = lpr.get_camera(mode)
    if camera and camera.isOpened():
        ret, frame = camera.read()
        if ret and frame is not None:
            _, buffer = cv2.imencode('.jpg', frame)
            return buffer.tobytes(), 200, {'Content-Type': 'image/jpeg'}
    return '', 404

@app.route('/captures/<path:filename>')
def serve_capture(filename):
    """提供 captures 資料夾中的圖片"""
    if 'user_id' not in session:
        return '', 401
    from flask import send_from_directory
    return send_from_directory('captures', filename)

# ============ ROI 視覺化編輯器 ============

@app.route('/roi-editor/<mode>')
def roi_editor(mode='in'):
    """ROI 區域設定視覺化編輯器"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if mode not in ('in', 'out'):
        mode = 'in'
    
    camera_url = db.get_setting(f'camera_{mode}_url', '')
    snapshot_url = None
    has_stream = False
    
    if camera_url:
        try:
            cap = cv2.VideoCapture(camera_url)
            if cap.isOpened():
                has_stream = True
                ret, frame = cap.read()
                if ret:
                    # 縮小以免太大
                    scale = min(1.0, 900 / frame.shape[1])
                    if scale < 1:
                        frame = cv2.resize(frame, (0, 0), fx=scale, fy=scale)
                    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    import base64
                    snapshot_url = 'data:image/jpeg;base64,' + base64.b64encode(buf).decode('utf-8')
                cap.release()
        except Exception as e:
            logger.error(f'ROI editor snapshot failed: {e}')
    
    return render_template('roi_editor.html',
        mode=mode,
        snapshot_url=snapshot_url,
        has_stream=has_stream,
        timestamp=datetime.now().strftime('%H%M%S')
    )

@app.route('/api/roi/<mode>', methods=['POST'])
def api_save_roi(mode='in'):
    """儲存 ROI 設定（支援多區域）"""
    if 'user_id' not in session:
        return jsonify({'success': False}), 401
    import json
    data = request.get_json() if request.is_json else {}
    zones = data.get('zones', [])
    if not zones:
        # 相容舊版（表單格式）
        roi = {
            'x': float(request.form.get('roi_x', 0)),
            'y': float(request.form.get('roi_y', 0)),
            'w': float(request.form.get('roi_w', 100)),
            'h': float(request.form.get('roi_h', 100))
        }
        zones = [roi]
    db.set_setting(f'camera_{mode}_roi', json.dumps({'zones': zones}))
    return jsonify({'success': True})

@app.route('/api/roi/<mode>', methods=['GET'])
def api_get_roi(mode='in'):
    """取得 ROI 設定"""
    if 'user_id' not in session:
        return jsonify({'success': False}), 401
    import json
    val = db.get_setting(f'camera_{mode}_roi', '')
    if val:
        try:
            data = json.loads(val)
            return jsonify(data)
        except:
            pass
    return jsonify({'zones': []})


@app.route('/api/camera_status/<mode>')
def api_camera_status(mode='in'):
    """檢查攝影機狀態"""
    camera = lpr.get_camera(mode)
    available = camera is not None and camera.isOpened()
    # 嘗試讀取一幀確認
    if available:
        ret, frame = camera.read()
        available = ret and frame is not None
    return jsonify({'available': available, 'mode': mode})

# ============ Web 路由 ============

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    records = db.get_records(limit=10)
    owners_count = len(db.get_owners())
    today_count = db.get_record_count(date_filter=datetime.now().strftime('%Y-%m-%d'))

    return render_template('index.html',
        records=records,
        owners_count=owners_count,
        today_count=today_count,
        username=session.get('username')
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    # 檢查 rate limit
    client_ip = request.remote_addr
    rate_check = check_login_rate_limit(client_ip)
    if not rate_check:
        remaining = rate_check[1] if isinstance(rate_check, tuple) else 0
        flash(f'登入嘗試過多，請等待 {remaining} 秒後再試', 'error')
        return render_template('login.html')
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user = db.verify_user(username, password)
        if user:
            clear_login_attempts(client_ip)  # 成功登入，清除記錄
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        record_failed_login(client_ip)  # 失敗記錄
        flash('帳號或密碼錯誤', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('engineer_mode', None)
    session.clear()
    return redirect(url_for('login'))

# --- 車主管理 ---

@app.route('/owners')
def owners():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    owners_list = db.get_owners()
    now_date = datetime.now().strftime('%Y-%m-%d')
    return render_template('owners.html', owners=owners_list, now_date=now_date)

@app.route('/owners/add', methods=['POST'])
def owners_add():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    plate = request.form.get('plate', '').strip().upper()
    card_id = request.form.get('card_id', '').strip()
    car_type = request.form.get('car_type', '轎車').strip()
    owner_type = request.form.get('owner_type', 'resident').strip()
    slot_number = request.form.get('slot_number', '').strip()
    note = request.form.get('note', '').strip()
    member_id = request.form.get('member_id', '').strip()
    rental_expiry_date = request.form.get('rental_expiry_date', '').strip()
    if not name or not plate:
        flash('姓名和車牌必填', 'error')
    else:
        owner_id = request.form.get('id', '').strip()
        owner_id = int(owner_id) if owner_id else None
        rental_start_date = request.form.get('rental_start_date', '').strip()
        ok, msg = db.add_owner(name, phone, plate, car_type, slot_number, note, owner_id, member_id, owner_type, card_id, rental_start_date or None, rental_expiry_date or None)
        if not ok:
            flash(msg, 'error')
    return redirect(url_for('owners'))

@app.route('/owners/edit/<int:owner_id>', methods=['POST'])
def owners_edit(owner_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    plate = request.form.get('plate', '').strip().upper()
    card_id = request.form.get('card_id', '').strip()
    car_type = request.form.get('car_type', '轎車').strip()
    owner_type = request.form.get('owner_type', 'resident').strip()
    slot_number = request.form.get('slot_number', '').strip()
    note = request.form.get('note', '').strip()
    member_id = request.form.get('member_id', '').strip()
    is_blacklist = 1 if request.form.get('is_blacklist') else 0
    rental_start_date = request.form.get('rental_start_date', '').strip()
    rental_expiry_date = request.form.get('rental_expiry_date', '').strip()
    ok, msg = db.update_owner(owner_id, name, phone, plate, car_type, slot_number, note, is_blacklist, member_id, owner_type, card_id, rental_start_date or None, rental_expiry_date or None)
    if not ok:
        flash(msg, 'error')
    return redirect(url_for('owners'))

@app.route('/owners/delete/<int:owner_id>', methods=['POST'])
def owners_delete(owner_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    db.delete_owner(owner_id)
    flash('已刪除', 'success')
    return redirect(url_for('owners'))

@app.route('/api/owners/<int:owner_id>/remove-blacklist', methods=['POST'])
def api_owners_remove_blacklist(owner_id):
    """移除車主的黑名單狀態"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    owner = db.get_owner_by_id(owner_id)
    if not owner:
        return jsonify({'success': False, 'error': '找不到車主'})
    
    # 更新為非黑名單
    db.update_owner(
        owner_id=owner_id,
        name=owner['name'],
        phone=owner['phone'],
        plate=owner['plate'],
        car_type=owner.get('car_type', '轎車'),
        slot_number=owner.get('slot_number'),
        note=owner.get('note', ''),
        is_blacklist=0,
        member_id=owner.get('member_id')
    )
    
    return jsonify({'success': True, 'message': '已移除黑名單'})

# --- 開門紀錄 ---

@app.route('/records')
def records():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    plate_filter = request.args.get('plate', '')
    date_filter = request.args.get('date', '')
    records_list = db.get_records(limit=per_page, offset=offset,
                                  plate_filter=plate_filter, date_filter=date_filter)
    total = db.get_record_count(plate_filter=plate_filter, date_filter=date_filter)
    total_pages = (total + per_page - 1) // per_page
    return render_template('records.html',
        records=records_list,
        page=page,
        total_pages=total_pages,
        plate_filter=plate_filter,
        date_filter=date_filter,
        per_page=per_page
    )

# --- 記錄管理 API ---

@app.route('/api/records/<int:record_id>', methods=['DELETE'])
def api_delete_record(record_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    conn = db.get_db()
    conn.execute('DELETE FROM records WHERE id = ?', (record_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/records/<int:record_id>', methods=['PUT'])
def api_update_record(record_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    data = request.json
    note = data.get('note', '')
    conn = db.get_db()
    conn.execute('UPDATE records SET note = ? WHERE id = ?', (note, record_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/records/recent')
def api_records_recent():
    """取得最近的記錄（供首頁自動刷新用）"""
    limit = int(request.args.get('limit', 10))
    records = db.get_records(limit=limit)
    return jsonify({'records': records})

@app.route('/api/records/export')
def api_records_export():
    """匯出記錄為 CSV"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    records = db.get_records(limit=10000)
    
    # 生成 CSV
    csv_lines = ['ID,時間,車牌,車主,結果,備註']
    for r in records:
        csv_lines.append(f'{r["id"]},{r["created_at"]},{r["plate"]},{r["owner_name"] or ""},{r["result"]},{r["note"] or ""}')
    
    # 生成 CSV（加入 BOM 確保 Excel 正確顯示中文）
    csv_content = '\ufeff' + '\n'.join(csv_lines)
    
    response = make_response(csv_content)
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename=records_{datetime.now().strftime("%Y%m%d")}.csv'
    return response

# ============ 訪客通行證 ============

@app.route('/visitor-passes')
def visitor_passes():
    """訪客通行證管理頁面"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('visitor_passes.html')

@app.route('/api/visitor-passes')
def api_visitor_passes():
    """取得訪客通行證列表"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    passes = db.get_visitor_passes(active_only=False)
    return jsonify({'passes': passes})

@app.route('/api/visitor-passes/active')
def api_visitor_passes_active():
    """取得有效的訪客通行證"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    passes = db.get_visitor_passes(active_only=True)
    return jsonify({'passes': passes})

@app.route('/api/visitor-passes', methods=['POST'])
def api_visitor_passes_add():
    """新增訪客通行證"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    plate = data.get('plate', '').upper()
    visitor_name = data.get('visitor_name', '')
    visitor_phone = data.get('visitor_phone', '')
    valid_hours = int(data.get('valid_hours', 24))
    note = data.get('note', '')
    
    if not plate:
        return jsonify({'success': False, 'error': '請填寫車牌'})
    
    pass_id = db.create_visitor_pass(
        plate=plate,
        visitor_name=visitor_name,
        visitor_phone=visitor_phone,
        valid_hours=valid_hours,
        note=note,
        created_by=session.get('user_id')
    )
    
    return jsonify({'success': True, 'pass_id': pass_id, 'message': f'已建立通行證，有效期 {valid_hours} 小時'})

@app.route('/api/visitor-passes/<int:pass_id>/cancel', methods=['POST'])
def api_visitor_passes_cancel(pass_id):
    """取消訪客通行證"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    db.cancel_visitor_pass(pass_id)
    return jsonify({'success': True, 'message': '已取消通行證'})

@app.route('/api/billing/export')
def api_billing_export():
    """匯出帳單為 CSV"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    billings = db.get_billing_list(limit=10000)
    
    # 生成 CSV
    csv_lines = ['ID,車牌,車主,金額,停車分鐘,進場時間,離場時間,付款方式,付款狀態,備註']
    for b in billings:
        csv_lines.append(f'{b["id"]},{b["plate"]},{b["owner_name"] or ""},{b["amount"]},{b["duration_minutes"]},{b["entry_time"]},{b["exit_time"]},{b["payment_method"]},{b["payment_status"]},{b["note"] or ""}')
    
    csv_content = '\ufeff' + '\n'.join(csv_lines)
    
    response = make_response(csv_content)
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename=billings_{datetime.now().strftime("%Y%m%d")}.csv'
    return response

@app.route('/api/owners')
def api_owners():
    """取得所有車主資料（含車位）"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    owners = db.get_owners()
    # 加入車位資訊（從 parking_slots 查找）
    result = []
    for o in owners:
        o = dict(o)
        slot = db.get_slot_by_owner_id(o['id'])
        o['slot_number'] = slot['slot_number'] if slot else None
        result.append(o)
    return jsonify(result)

@app.route('/api/owners/export')
def api_owners_export():
    """匯出車主為 CSV"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    owners = db.get_owners()
    
    # 生成 CSV
    csv_lines = ['ID,車牌,姓名,電話,車型,車位,備註']
    for o in owners:
        csv_lines.append(f'{o["id"]},{o["plate"]},{o["name"]},{o["phone"] or ""},{o["car_type"]},{o["slot_number"] or ""},{o["note"] or ""}')
    
    csv_content = '\ufeff' + '\n'.join(csv_lines)
    
    response = make_response(csv_content)
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename=owners_{datetime.now().strftime("%Y%m%d")}.csv'
    return response

@app.route('/api/owners/import', methods=['POST'])
def api_owners_import():
    """匯入車主 CSV"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '沒有上傳檔案'})
    
    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({'success': False, 'message': '只接受 CSV 檔案'})
    
    try:
        import csv
        import io
        stream = io.TextIOWrapper(file.stream, encoding='utf-8-sig')
        reader = csv.reader(stream)
        next(reader)  # 跳過標題列
        
        added = 0
        updated = 0
        errors = []
        
        for i, row in enumerate(reader):
            if len(row) < 4:
                errors.append(f'第 {i+2} 行：資料不足')
                continue
            
            try:
                # ID,車牌,姓名,電話,車型,車位,備註,車主類型,卡號
                owner_id = row[0].strip() if row[0] else None
                plate = row[1].strip().upper().replace('·', '')
                name = row[2].strip()
                phone = row[3].strip() if len(row) > 3 else ''
                car_type = row[4].strip() if len(row) > 4 else '轎車'
                slot_number = row[5].strip() if len(row) > 5 else ''
                note = row[6].strip() if len(row) > 6 else ''
                owner_type = row[7].strip() if len(row) > 7 else 'resident'
                card_id = row[8].strip() if len(row) > 8 else ''
                
                if not plate or not name:
                    errors.append(f'第 {i+2} 行：車牌或姓名為空')
                    continue
                
                # 檢查是否已存在
                existing = db.get_owner_by_plate(plate)
                if existing:
                    # 更新（保留原有 owner_type 和 card_id 若未提供）
                    db.update_owner(
                        existing['id'], name, phone, plate, car_type, slot_number, note,
                        existing.get('is_blacklist', 0),
                        owner_type=(owner_type or existing.get('owner_type', 'resident')),
                        card_id=(card_id or existing.get('card_id', ''))
                    )
                    updated += 1
                else:
                    # 新增
                    db.add_owner(
                        name, phone, plate, car_type, slot_number, note,
                        owner_type=owner_type or 'resident',
                        card_id=card_id
                    )
                    added += 1
            except Exception as e:
                errors.append(f'第 {i+2} 行：{str(e)}')
        
        message = f'匯入完成：新增 {added} 筆，更新 {updated} 筆'
        if errors:
            message += f'，錯誤 {len(errors)} 筆'
        
        return jsonify({'success': True, 'message': message, 'errors': errors[:10]})
    except Exception as e:
        return jsonify({'success': False, 'message': f'匯入失敗：{str(e)}'})

# --- 手動開門 ---

@app.route('/open', methods=['POST'])
def manual_open():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    if relay:
        note = request.form.get('note', '')
        db.add_record('手動開門', session.get('username'), '手動開門', note=note)
        ok = relay.open_gate()
        return jsonify({'success': ok})
    return jsonify({'success': False, 'message': '繼電器未連接'})

# --- 車牌比對 API ---

@app.route('/api/check_plate', methods=['POST'])
def check_plate():
    """車牌辨識系統回調：發現車牌後比對"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'}), 401
    plate = request.json.get('plate', '').upper()
    image_path = request.json.get('image_path')

    owner = db.get_owner_by_plate(plate)
    if owner:
        result = f'✅ 允許進場'
        if relay:
            relay.open_gate()
        if image_path:
            db.add_record(plate, owner['name'], result, image_path)
        else:
            db.add_record(plate, owner['name'], result)
        return jsonify({'allowed': True, 'owner': owner['name']})
    else:
        result = f'❌ {plate} 不在白名單'
        db.add_record(plate, None, result, image_path)
        return jsonify({'allowed': False, 'plate': plate})

# --- 認列拍照 ---

@app.route('/capture/<mode>')
def capture(mode='in'):
    """手動拍照"""
    if 'user_id' not in session:
        return jsonify({'error': '未登入'})
    camera = lpr.get_camera(mode)
    if camera and camera.isOpened():
        ret, frame = camera.read()
        if ret:
            filename = f"manual_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join('captures', filename)
            os.makedirs('captures', exist_ok=True)
            cv2.imwrite(filepath, frame)
            return jsonify({'success': True, 'image': filename})
    return jsonify({'success': False})

# --- 儀表板數據 ---

@app.route('/api/dashboard')
def dashboard():
    """儀表板摘要"""
    today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'owners_count': len(db.get_owners()),
        'today_count': db.get_record_count(date_filter=today),
        'recent_records': db.get_records(limit=5)
    })

# ============ 設定頁面 ============

@app.route('/settings')
def settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    # 檢查是否需要工程商密碼
    engineer_hash = db.get_setting('engineer_password_hash', '')
    if engineer_hash and not session.get('engineer_mode'):
        return render_template('engineer_verify.html', next='settings')
    
    import json
    # 解析 ROI 設定
    def parse_roi(key):
        val = db.get_setting(key, '')
        if val:
            try:
                return json.loads(val)
            except:
                pass
        return {'x': 0, 'y': 0, 'w': 100, 'h': 100}

    return render_template('settings.html',
        camera_in_url=db.get_setting('camera_in_url', ''),
        camera_out_url=db.get_setting('camera_out_url', ''),
        camera_in_roi=parse_roi('camera_in_roi'),
        camera_out_roi=parse_roi('camera_out_roi'),
        relay_type=db.get_setting('relay_type', 'usb'),
        relay_port=db.get_setting('relay_port', ''),
        open_duration=float(db.get_setting('open_duration', '1.5')),
        relay_modbus_ip=db.get_setting('relay_modbus_ip', ''),
        relay_modbus_port=int(db.get_setting('relay_modbus_port', '502')),
        relay_modbus_coil=int(db.get_setting('relay_modbus_coil', '0')),
        ar725e_ip=db.get_setting('ar725e_ip', ''),
        ar725e_port=int(db.get_setting('ar725e_port', '502')),
        ar725e_coil=int(db.get_setting('ar725e_coil', '0')),
        ar725e_mode=db.get_setting('ar725e_mode', 'modbus_tcp'),
        owners_count=len(db.get_owners()),
        today_count=db.get_record_count(date_filter=datetime.now().strftime('%Y-%m-%d')),
        yolo_conf=float(db.get_setting('yolo_conf', '0.5')),
        ocr_conf=float(db.get_setting('ocr_conf', '0.3')),
        image_zoom=float(db.get_setting('image_zoom', '2')),
        cooldown=int(db.get_setting('cooldown', '5')),
        ocr_engine=db.get_setting('ocr_engine', 'easyocr'),
        ollama_url=db.get_setting('ollama_url', 'http://localhost:11434'),
        ollama_model=db.get_setting('ollama_model', 'llava'),
        project_name=db.get_setting('project_name', '車牌辨識開門系統'),
        # 新功能設定
        auto_parking_session=db.get_setting('auto_parking_session', 'false'),
        auto_assign_slot=db.get_setting('auto_assign_slot', 'false'),
        record_direction=db.get_setting('record_direction', 'true')
    )

# ============ 資料庫備份 ============

@app.route('/api/backup')
@engineer_required
def api_backup():
    """手動備份資料庫"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    if backup_database():
        return jsonify({'success': True, 'message': '備份成功'})
    return jsonify({'success': False, 'message': '備份失敗'})

@app.route('/api/backup/list')
def api_backup_list():
    """取得備份列表"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    backup_dir = 'backups'
    if not os.path.exists(backup_dir):
        return jsonify({'success': True, 'backups': []})
    
    backups = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if f.endswith('.db'):
            path = os.path.join(backup_dir, f)
            backups.append({
                'name': f,
                'size': os.path.getsize(path),
                'time': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')
            })
    
    return jsonify({'success': True, 'backups': backups})

@app.route('/api/backup/restore/<backup_name>')
@engineer_required
def api_backup_restore(backup_name):
    """從備份還原"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    backup_file = os.path.join('backups', backup_name)
    if not os.path.exists(backup_file):
        return jsonify({'success': False, 'message': '找不到備份檔案'})
    
    if restore_database(backup_file):
        return jsonify({'success': True, 'message': '還原成功，請重啟系統'})
    return jsonify({'success': False, 'message': '還原失敗'})

@app.route('/api/backup/download/<backup_name>')
def api_backup_download(backup_name):
    """下載備份檔案"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    backup_file = os.path.join('backups', backup_name)
    if not os.path.exists(backup_file):
        return jsonify({'success': False, 'message': '找不到備份檔案'})
    
    return send_file(backup_file, as_attachment=True)

@app.route('/settings/save', methods=['POST'])
def settings_save():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    # 工程商密碼保護
    engineer_hash = db.get_setting('engineer_password_hash', '')
    if engineer_hash and not session.get('engineer_mode'):
        return redirect(url_for('login'))
    section = request.form.get('section')
    # 工程商密碼保護（密碼變更除外：用戶只能改自己的密碼）
    if section != 'password':
        engineer_hash = db.get_setting('engineer_password_hash', '')
        if engineer_hash and not session.get('engineer_mode'):
            return redirect(url_for('login'))
    if section == 'camera':
        import json
        db.set_setting('camera_in_url', request.form.get('camera_in_url', ''))
        db.set_setting('camera_out_url', request.form.get('camera_out_url', ''))
        # ROI 設定（存成 JSON）
        roi_in = {
            'x': int(request.form.get('roi_in_x', 0)),
            'y': int(request.form.get('roi_in_y', 0)),
            'w': int(request.form.get('roi_in_w', 100)),
            'h': int(request.form.get('roi_in_h', 100))
        }
        roi_out = {
            'x': int(request.form.get('roi_out_x', 0)),
            'y': int(request.form.get('roi_out_y', 0)),
            'w': int(request.form.get('roi_out_w', 100)),
            'h': int(request.form.get('roi_out_h', 100))
        }
        db.set_setting('camera_in_roi', json.dumps(roi_in))
        db.set_setting('camera_out_roi', json.dumps(roi_out))
        flash('攝影機設定已儲存', 'success')
    elif section == 'relay':
        relay_type = request.form.get('relay_type', 'usb')
        db.set_setting('relay_type', relay_type)
        db.set_setting('relay_port', request.form.get('relay_port', ''))
        db.set_setting('open_duration', request.form.get('open_duration', '1.5'))
        db.set_setting('relay_modbus_ip', request.form.get('relay_modbus_ip', ''))
        db.set_setting('relay_modbus_port', request.form.get('relay_modbus_port', '502'))
        db.set_setting('relay_modbus_coil', request.form.get('relay_modbus_coil', '0'))
        # 更新 relay
        global relay
        if relay:
            relay.close()
        if relay_type == 'modbus_tcp':
            from relay import ModbusTCPController
            ip = request.form.get('relay_modbus_ip', '')
            port = int(request.form.get('relay_modbus_port', 502))
            coil = int(request.form.get('relay_modbus_coil', 0))
            if ip:
                relay = ModbusTCPController(ip=ip, port=port, coil=coil)
                logger.info(f'Modbus TCP 繼電器已設定: {ip}:{port} coil={coil}')
        else:
            port = request.form.get('relay_port', '')
            if port:
                from relay import RelayController
                relay = RelayController(port=port)
                if relay.connect():
                    logger.info(f'USB 繼電器已更新: {port}')
        flash('繼電器設定已儲存', 'success')
    elif section == 'card_reader':
        db.set_setting('ar725e_ip', request.form.get('ar725e_ip', ''))
        db.set_setting('ar725e_port', request.form.get('ar725e_port', '502'))
        db.set_setting('ar725e_coil', request.form.get('ar725e_coil', '0'))
        db.set_setting('ar725e_mode', request.form.get('ar725e_mode', 'modbus_tcp'))
        flash('AR-725E 讀卡機設定已儲存', 'success')
    elif section == 'password':
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass and new_pass == confirm:
            db.change_password(session['user_id'], new_pass)
            flash('密碼已修改', 'success')
        elif new_pass and new_pass != confirm:
            flash('兩次密碼不同', 'error')
    elif section == 'lpr_tuning':
        db.set_setting('yolo_conf', request.form.get('yolo_conf', '0.5'))
        db.set_setting('ocr_conf', request.form.get('ocr_conf', '0.3'))
        db.set_setting('image_zoom', request.form.get('image_zoom', '2'))
        db.set_setting('cooldown', request.form.get('cooldown', '5'))
        db.set_setting('ocr_engine', request.form.get('ocr_engine', 'easyocr'))
        db.set_setting('ulrixon_bbox_model', request.form.get('ulrixon_bbox_model', 'models/ulrixon_bbox.pt'))
        db.set_setting('ulrixon_ocr_model', request.form.get('ulrixon_ocr_model', 'models/ulrixon_ocr.pt'))
        db.set_setting('ollama_url', request.form.get('ollama_url', 'http://localhost:11434'))
        db.set_setting('ollama_model', request.form.get('ollama_model', 'llava'))
        flash('車牌辨識微調設定已儲存', 'success')
    elif section == 'features':
        # 新功能設定
        auto_session = 'true' if request.form.get('auto_parking_session') else 'false'
        auto_slot = 'true' if request.form.get('auto_assign_slot') else 'false'
        record_dir = 'true' if request.form.get('record_direction') else 'false'
        db.set_setting('auto_parking_session', auto_session)
        db.set_setting('auto_assign_slot', auto_slot)
        db.set_setting('record_direction', record_dir)
        flash('功能設定已儲存', 'success')
    elif section == 'project':
        db.set_setting('project_name', request.form.get('project_name', '車牌辨識開門系統'))
        flash('系統資訊已儲存', 'success')
    return redirect(url_for('settings'))

# ============ 測試 API ============

@app.route('/api/test_camera')
@engineer_required
def test_camera():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    # 測試進口攝影機
    camera_in = lpr.get_camera('in')
    camera_out = lpr.get_camera('out')
    
    result = {'in': False, 'out': False, 'message': ''}
    
    if camera_in and camera_in.isOpened():
        ret, frame = camera_in.read()
        if ret:
            filename = f"test_in_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join('captures', filename)
            os.makedirs('captures', exist_ok=True)
            cv2.imwrite(filepath, frame)
            result['in'] = True
    
    if camera_out and camera_out.isOpened():
        ret, frame = camera_out.read()
        if ret:
            filename = f"test_out_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join('captures', filename)
            os.makedirs('captures', exist_ok=True)
            cv2.imwrite(filepath, frame)
            result['out'] = True
    
    if result['in'] or result['out']:
        return jsonify({
            'success': True, 
            'message': f"進口: {'✅' if result['in'] else '❌'}, 出口: {'✅' if result['out'] else '❌'}",
            'in': result['in'],
            'out': result['out']
        })
    
    return jsonify({'success': False, 'message': '無法連接任何攝影機，請檢查 URL 設定'})

@app.route('/api/test_relay', methods=['POST'])
@engineer_required
def test_relay():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    if relay:
        ok = relay.open_gate()
        return jsonify({'success': ok, 'message': '' if ok else '連接失敗'})
    return jsonify({'success': False, 'message': '繼電器未連接'})


def ocr_crop_with_tesseract(crop_img):
    """Use Tesseract OCR for plate crop (fallback engine)"""
    try:
        import pytesseract
        import cv2
        import numpy as np

        h, w = crop_img.shape[:2]
        if h < 20 or w < 60:
            scale = max(60 / w, 20 / h)
            crop_img = cv2.resize(crop_img, (int(w * scale), int(h * scale)))

        gray = cv2.cvtColor(crop_img, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        config = '--psm 7 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-'
        text = pytesseract.image_to_string(binary, lang='eng', config=config).strip()

        if text:
            logger.info(f'Tesseract fallback found: {text}')
            return [{'text': text, 'confidence': 0.7}]
        return []
    except Exception as e:
        logger.error(f'Tesseract OCR failed: {e}')
        return []


def ocr_with_ulrixon(image_path):
    """
    使用 Ulrixon 兩階段車牌辨識模型（台灣車牌專用）
    Stage 1: best_bbox.pt 偵測車牌位置
    Stage 2: best_s_ocr.pt 辨識車牌字元
    回傳: [{'text': 'ABC-1234', 'confidence': 0.95}]
    """
    try:
        import cv2
        import numpy as np

        # 讀取圖片
        img = cv2.imread(image_path)
        if img is None:
            logger.error(f'Ulrixon OCR: 無法讀取圖片 {image_path}')
            return []

        # Stage 1: 車牌偵測
        bbox_model = get_ulrixon_bbox_model()
        results = bbox_model(img, conf=0.1, verbose=False)
        
        if not results or not results[0].boxes:
            return []

        texts = []
        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue
            
            for box in result.boxes:
                xyxy = box.xyxy[0].cpu().numpy()
                x1, y1, x2, y2 = map(int, xyxy)
                
                # 擴大一點範圍避免切到字
                margin = 2
                x1 = max(0, x1 - margin)
                y1 = max(0, y1 - margin)
                x2 = min(img.shape[1], x2 + margin)
                y2 = min(img.shape[0], y2 + margin)
                
                # 裁出車牌區域
                crop = img[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                
                # 車牌直式（高度>寬度）-> 旋轉 90 度
                h, w = crop.shape[:2]
                if h > w:
                    crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
                
                # Stage 2: OCR
                ocr_model = get_ulrixon_ocr_model()
                ocr_results = ocr_model(crop, verbose=False)
                
                if not ocr_results or not ocr_results[0].boxes:
                    continue
                
                # 取得每個字元的預測 + 位置
                ocr_result = ocr_results[0]
                if ocr_result.boxes is None:
                    continue
                
                boxes = ocr_result.boxes.xyxy.cpu().numpy()
                classes = ocr_result.boxes.cls.cpu().numpy()
                confs = ocr_result.boxes.conf.cpu().numpy()
                
                # 按 x 座標排序（由左到右）
                order = boxes[:, 0].argsort()
                
                # 對應字元
                char_classes = [int(c) for c in classes[order]]
                char_conf = [float(c) for c in confs[order]]
                
                # 組成車牌文字
                plate_chars = []
                for cls_id in char_classes:
                    if cls_id < len(OCR_CLASSES):
                        plate_chars.append(OCR_CLASSES[cls_id])
                
                plate_text = ''.join(plate_chars)
                
                # 格式化：一般車牌 XXX-XXXX
                if len(plate_text) >= 7:
                    plate_text = plate_text[:3] + '-' + plate_text[3:]
                
                avg_conf = np.mean(char_conf) if char_conf else 0
                
                if plate_text and len(plate_text) >= 4:
                    texts.append({
                        'text': plate_text.upper(),
                        'confidence': round(float(avg_conf), 2)
                    })
                    logger.info(f'Ulrixon OCR: {plate_text.upper()} (conf={avg_conf:.2f})')
        
        return texts
    except Exception as e:
        logger.error(f'Ulrixon OCR 失敗: {e}')
        return []

# 初始化 YOLOv8 和 PaddleOCR (在背景延遲載入)
_yolo_model = None
_yolo_ocr_model = None
_paddleocr = None
_ulrixon_bbox_model = None
_ulrixon_ocr_model = None

def get_ulrixon_bbox_model():
    global _ulrixon_bbox_model
    if _ulrixon_bbox_model is None:
        from ultralytics import YOLO
        model_path = db.get_setting('ulrixon_bbox_model', 'models/ulrixon_bbox.pt')
        _ulrixon_bbox_model = YOLO(model_path)
        logger.info(f'Ulrixon BBox 模型已載入: {model_path}')
    return _ulrixon_bbox_model

def get_ulrixon_ocr_model():
    global _ulrixon_ocr_model
    if _ulrixon_ocr_model is None:
        from ultralytics import YOLO
        model_path = db.get_setting('ulrixon_ocr_model', 'models/ulrixon_ocr.pt')
        _ulrixon_ocr_model = YOLO(model_path)
        logger.info(f'Ulrixon OCR 模型已載入: {model_path}')
    return _ulrixon_ocr_model

def get_yolo_model():
    """取得 YOLOv8 車牌檢測模型"""
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO
        # 使用專門的車牌檢測模型（預設用 huggingface 預訓練模型）
        model_path = 'models/best.pt'
        if os.path.exists(model_path):
            _yolo_model = YOLO(model_path)
            logger.info('YOLOv8 車牌檢測模型初始化完成 (best.pt)')
        else:
            _yolo_model = YOLO('yolov8s.pt')
            logger.info('YOLOv8s 初始化完成')
    return _yolo_model

def get_paddleocr():
    """取得 PaddleOCR Reader"""
    global _paddleocr
    if _paddleocr is None:
        import os
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
        from paddleocr import PaddleOCR
        _paddleocr = PaddleOCR(lang='en', use_angle_cls=True)
        logger.info('PaddleOCR 初始化完成')
    return _paddleocr

def ocr_crop_with_paddle(crop_img):
    """使用 PaddleOCR 辨識車牌截圖"""
    try:
        ocr = get_paddleocr()
        import numpy as np
        result = ocr.ocr(np.array(crop_img), cls=True)
        
        if not result or not result[0]:
            return []
        
        texts = []
        for line in result[0]:
            if line:
                text = line[1][0] if len(line) > 1 else ''
                conf = line[1][1] if len(line) > 1 else 0
                texts.append((text, conf))
        
        logger.info(f'PaddleOCR 結果: {texts}')
        return texts
    except Exception as e:
        logger.error(f'PaddleOCR 錯誤: {e}')
        return []

# EasyOCR 全域實例
_easyocr = None

def get_easyocr():
    """取得 EasyOCR Reader"""
    global _easyocr
    if _easyocr is None:
        import easyocr
        _easyocr = easyocr.Reader(['en'], gpu=False, verbose=False)
        logger.info('EasyOCR 初始化完成')
    return _easyocr

def detect_plate_with_yolo(image_path):
    """
    使用 YOLOv8 專門車牌檢測模型偵測車牌位置
    回傳: list of {'bbox': tuple, 'crop': numpy array}
    """
    try:
        model = get_yolo_model()
        img = cv2.imread(image_path)
        if img is None:
            return []
        
        # 讀取 YOLO 偵測信心度設定
        yolo_conf = float(db.get_setting('yolo_conf', '0.5'))
        
        # YOLOv8 車牌偵測
        results = model(img, verbose=False, conf=yolo_conf)
        
        plate_crops = []
        h, w = img.shape[:2]
        
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                
                # 擴大區域以包含完整車牌（車牌上下左右都要留白）
                pad_x = int((x2 - x1) * 0.15) + 10
                pad_y = int((y2 - y1) * 0.2) + 10
                x1 = max(0, x1 - pad_x)
                y1 = max(0, y1 - pad_y)
                x2 = min(w, x2 + pad_x)
                y2 = min(h, y2 + pad_y)
                
                if int(x2) > int(x1) and int(y2) > int(y1):
                    crop = img[int(y1):int(y2), int(x1):int(x2)]
                    plate_crops.append({
                        'bbox': (int(x1), int(y1), int(x2), int(y2)),
                        'crop': crop,
                        'vehicle_type': 'license_plate',
                        'vehicle_conf': round(conf, 2)
                    })
                    logger.info(f'YOLOv8 偵測到車牌，信心度: {conf:.2f}, 裁切尺寸: {crop.shape}')
        
        return plate_crops
        
    except Exception as e:
        logger.error(f'YOLOv8 車牌偵測失敗: {e}')
        return []

def apply_perspective_transform(crop_img):
    """
    對車牌區域進行簡單的影像增強，準備 OCR
    簡化版：不做複雜的透視變換，直接增強對比度和銳利化
    """
    try:
        h, w = crop_img.shape[:2]
        
        # 確保足夠大的尺寸用於 OCR
        min_height = 60
        if h < min_height:
            scale = min_height / h
            crop_img = cv2.resize(crop_img, (int(w * scale), min_height))
        
        # 轉灰階
        gray = cv2.cvtColor(crop_img, cv2.COLOR_BGR2GRAY)
        
        # 將灰階圖放大兩倍，OCR 需要足夠像素
        zoomed = cv2.resize(gray, (gray.shape[1] * 2, gray.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
        
        # 增強對比
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(4,4))
        enhanced = clahe.apply(zoomed)
        
        # 輕度銳利化
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        sharpened = cv2.filter2D(enhanced, -1, kernel)
        
        # 轉回 BGR（EasyOCR 接受 BGR 或灰階）
        result = cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)
        
        return result
        
    except Exception as e:
        logger.error(f'影像增強失敗: {e}')
        return crop_img

def preprocess_for_ocr(img):
    """
    專業的車牌圖片前處理，專為 OCR 優化（加強版）
    """
    try:
        # 轉灰階
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
        
        # 放大到標準高度（200像素高度最適合 OCR）
        h, w = gray.shape
        target_height = 200
        scale = target_height / h
        new_width = int(w * scale)
        resized = cv2.resize(gray, (new_width, target_height), interpolation=cv2.INTER_CUBIC)
        
        # 高斯模糊去噪
        blurred = cv2.GaussianBlur(resized, (2, 2), 0)
        
        # 自適應二值化（對車牌效果更好）
        binary = cv2.adaptiveThreshold(
            blurred, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            11, 2
        )
        
        # 形態學操作：去除小噪點、填補孔洞
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        
        # 銳利化
        sharpen_kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        sharpened = cv2.filter2D(cleaned, -1, sharpen_kernel)
        
        # 對比增強
        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(6,6))
        enhanced = clahe.apply(sharpened)
        
        # 最後再做一次銳利化
        final = cv2.filter2D(enhanced, -1, sharpen_kernel)
        
        return final
        
    except Exception as e:
        logger.error(f'OCR 前處理失敗: {e}')
        return img

def ocr_with_easyocr(image_path):
    """使用 EasyOCR + Tesseract 雙重辨識（加強版）"""
    try:
        import cv2
        import pytesseract
        import tempfile
        import os
        ocr = get_easyocr()
        
        # 讀取圖片
        img = cv2.imread(image_path)
        if img is None:
            return []
        
        texts = []
        
        # 方法1: Tesseract OCR（增強配置）
        processed = preprocess_for_ocr(img)
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            cv2.imwrite(tmp.name, processed)
            
            # 嘗試多種 PSM 模式
            for psm in [7, 8, 13]:  # 7=單行, 8=單字, 13= raw line
                try:
                    tess_text = pytesseract.image_to_string(
                        tmp.name, 
                        lang='eng', 
                        config=f'--psm {psm} --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-'
                    ).strip()
                    if tess_text and len(tess_text) >= 4:
                        texts.append({'text': tess_text, 'confidence': 0.85})
                        logger.info(f'Tesseract PSM{psm} 找到了: {tess_text}')
                        break
                except:
                    continue
            
            os.unlink(tmp.name)
        
        # 方法2: EasyOCR（放大圖片）
        img_large = cv2.resize(img, (img.shape[1] * 3, img.shape[0] * 3), interpolation=cv2.INTER_CUBIC)
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            cv2.imwrite(tmp.name, img_large)
            result = ocr.readtext(tmp.name, paragraph=False, detail=1)
            os.unlink(tmp.name)
        
        # 處理 EasyOCR 結果
        if result:
            for line in result:
                bbox, text, conf = line
                if conf > 0.3 and len(text.strip()) >= 4:
                    texts.append({'text': text.strip(), 'confidence': round(conf, 2)})
        
        logger.info(f'OCR 找到了 {len(texts)} 個候選: {[t["text"] for t in texts]}')
        return texts
    except Exception as e:
        logger.error(f'OCR failed: {e}')
        return []

def ocr_crop_with_easyocr(crop_img):
    """對裁剪後的車牌區域使用 OCR 辨識（加強版）"""
    try:
        import tempfile
        import os
        import cv2
        import pytesseract
        ocr = get_easyocr()
        
        # 確保裁切圖片足夠大
        h, w = crop_img.shape[:2]
        if h < 20:
            scale = 20 / h
            crop_img = cv2.resize(crop_img, (int(w * scale), 20))
        
        texts = []
        
        # 方法1: Tesseract OCR（增強配置）
        processed = preprocess_for_ocr(crop_img)
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            cv2.imwrite(tmp.name, processed)
            
            # 嘗試多種 PSM 模式和字元白名單
            for psm in [7, 8, 13]:
                try:
                    tess_text = pytesseract.image_to_string(
                        tmp.name,
                        lang='eng',
                        config=f'--psm {psm} --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-'
                    ).strip()
                    if tess_text and len(tess_text) >= 4:
                        texts.append({'text': tess_text, 'confidence': 0.85})
                        logger.info(f'Tesseract crop PSM{psm} 找到了: {tess_text}')
                        break
                except:
                    continue
            os.unlink(tmp.name)
        
        # 方法2: EasyOCR（4倍放大）
        img_large = cv2.resize(crop_img, (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            cv2.imwrite(tmp.name, img_large)
            result = ocr.readtext(tmp.name, paragraph=False, detail=1)
            os.unlink(tmp.name)
        
        # 處理 EasyOCR 結果
        if result:
            for line in result:
                bbox, text, conf = line
                if conf > 0.25 and len(text.strip()) >= 4:
                    texts.append({'text': text.strip(), 'confidence': round(conf, 2)})
        
        logger.info(f'OCR crop 找到了 {len(texts)} 個候選: {[t["text"] for t in texts]}')
        return texts
    except Exception as e:
        logger.error(f'OCR crop failed: {e}')
        return []

def ocr_with_tesseract(image_path):
    """使用 Tesseract OCR 辨識圖片中的文字"""
    try:
        import cv2
        import pytesseract
        import tempfile
        import os
        
        # 讀取圖片
        img = cv2.imread(image_path)
        if img is None:
            return []
        
        # 放大圖片讓 OCR 更容易辨識
        h, w = img.shape[:2]
        img_large = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        
        # 轉灰階
        gray = cv2.cvtColor(img_large, cv2.COLOR_BGR2GRAY)
        
        # 二值化處理（黑白分明）
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # 儲存處理後的圖片用於 OCR
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            cv2.imwrite(tmp.name, binary)
            # 使用 Tesseract OCR，設定英語（車牌主要是數字和字母）
            text = pytesseract.image_to_string(tmp.name, lang='eng', config='--psm 7')
            os.unlink(tmp.name)
        
        # 解析 Tesseract 輸出
        texts = []
        for line in text.split('\n'):
            line = line.strip()
            if line:
                texts.append({
                    'text': line,
                    'confidence': 0.7  # Tesseract 不提供信心度，用預設值
                })
        
        logger.info(f'Tesseract OCR 找到了 {len(texts)} 個文字: {[t["text"] for t in texts]}')
        return texts
    except Exception as e:
        logger.error(f'Tesseract OCR failed: {e}')
        return []

def ocr_with_ollama(image_path):
    """使用 Ollama VLM 辨識車牌"""
    try:
        import requests
        import base64
        import os
        
        ollama_url = db.get_setting('ollama_url', 'http://localhost:11434')
        ollama_model = db.get_setting('ollama_model', 'llava')
        
        # 讀取圖片並轉 base64
        with open(image_path, 'rb') as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        
        # 呼叫 Ollama
        payload = {
            "model": ollama_model,
            "prompt": "請辨識圖片中的車牌號碼，只回傳車牌號碼，格式如 ABC-1234，不需要其他文字。",
            "images": [img_b64],
            "stream": False
        }
        
        resp = requests.post(f'{ollama_url}/api/generate', json=payload, timeout=60)
        if resp.status_code != 200:
            logger.error(f'Ollama OCR 失敗: HTTP {resp.status_code}')
            return []
        
        result_text = resp.json().get('response', '').strip()
        logger.info(f'Ollama OCR 結果: {result_text}')
        
        if result_text:
            return [{'text': result_text, 'confidence': 0.8}]
        return []
        
    except Exception as e:
        logger.error(f'Ollama OCR 失敗: {e}')
        return []

def ocr_crop_with_ollama(crop_img):
    """對裁剪後的車牌區域使用 Ollama VLM 辨識"""
    try:
        import requests
        import base64
        import cv2
        import tempfile
        import os
        
        ollama_url = db.get_setting('ollama_url', 'http://localhost:11434')
        ollama_model = db.get_setting('ollama_model', 'llava')
        
        # 儲存裁切圖片
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            cv2.imwrite(tmp.name, crop_img)
            with open(tmp.name, 'rb') as f:
                img_bytes = f.read()
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            os.unlink(tmp.name)
        
        payload = {
            "model": ollama_model,
            "prompt": "請辨識圖片中的車牌號碼，只回傳車牌號碼，格式如 ABC-1234，不需要其他文字。",
            "images": [img_b64],
            "stream": False
        }
        
        resp = requests.post(f'{ollama_url}/api/generate', json=payload, timeout=60)
        if resp.status_code != 200:
            logger.error(f'Ollama crop OCR 失敗: HTTP {resp.status_code}')
            return []
        
        result_text = resp.json().get('response', '').strip()
        logger.info(f'Ollama crop OCR 結果: {result_text}')
        
        if result_text:
            return [{'text': result_text, 'confidence': 0.85}]
        return []
        
    except Exception as e:
        logger.error(f'Ollama crop OCR 失敗: {e}')
        return []

def filter_plate_text(ocr_texts):
    """過濾並格式化車牌文字"""
    import re
    
    # 讀取 OCR 信心度設定
    ocr_conf_threshold = float(db.get_setting('ocr_conf', '0.3'))
    
    # 台灣車牌格式
    plate_patterns = [
        # 標準格式
        r'[A-Z]{2,3}-[0-9]{3,4}',   # ABC-1234, AB-1234, ABC-123
        r'[0-9]{2}-[A-Z]{2,3}',       # 12-ABC, 123-ABC
        r'[0-9]{4}-[A-Z]{2,3}',      # 5799-KE, 1234-ABC (4位數-2/3字母)
        r'[A-Z]{2,3}[0-9]{4}',        # ABC1234, AB1234
        r'[0-9][A-Z0-9]{5}',          # 1ABC23, 12ABC3
        r'[0-9]{4}[A-Z]{2,3}',        # 5799KE, 1234AB (4位數2字母，無dash)

        # 新式車牌（2012年後）
        r'[A-Z]{2}-[0-9]{4}',         # AB-1234 (新式2碼-4碼)
        r'[A-Z]{3}-[0-9]{3}',         # ABC-123 (新式3碼-3碼)

        # 大型車
        r'[0-9]{3}-[A-Z]{3}',         # 123-ABC (大型車)

        # 特殊車牌（含中間點 · 或 .）
        r'[0-9]{4}[·\.][A-Z]{2,3}',   # 0831·KM, 0831.KM (台灣舊式)
        r'[0-9]{3}[·\.][A-Z]{3}',     # 123·ABC (大型車·格式)

        # 特殊車牌
        r'[0-9]{3}[A-Z]{3}',         # 123ABC (無dash大型車)
        r'[A-Z][0-9]{4}[A-Z]',       # A1234B (混合)
    ]
    
    # 排除包含中文的文字
    chinese_pattern = re.compile(r'[\u4e00-\u9fff]')
    
    plates = []
    for t in ocr_texts:
        text = t['text'].strip()
        conf = t.get('confidence', 1.0)
        
        # 跳過包含中文的文字（如「台灣省」）
        if chinese_pattern.search(text):
            continue
        # 跳過太短或太長的文字
        if len(text) < 4 or len(text) > 10:
            continue
        # 必須包含數字和字母
        if not re.search(r'[0-9]', text) or not re.search(r'[A-Z]', text):
            continue
        # 信心度太低就跳過
        if conf < ocr_conf_threshold:
            continue
        
        text_upper = text.upper().replace('·', '-').replace('.', '-')
        for pattern in plate_patterns:
            matches = re.findall(pattern, text_upper)
            for match in matches:
                if '-' not in match and len(match) == 7:
                    plates.append(match[:3] + '-' + match[3:])
                else:
                    plates.append(match)
    
    return list(dict.fromkeys(plates))

def extract_plate_number(ocr_texts):
    """從 OCR 文字中提取引擎號碼格式的內容"""
    return filter_plate_text(ocr_texts)

@app.route('/api/detect_plate', methods=['POST'])
def api_detect_plate():
    """辨識上傳圖片中的車牌（YOLOv8 + PaddleOCR 架構）"""
    from datetime import datetime as dt  # 避免 scope 問題
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    if 'image' not in request.files:
        return jsonify({'success': False, 'message': '沒有上傳檔案'})
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'message': '沒有選擇檔案'})
    
    # 儲存圖片
    filename = f"detect_{dt.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    filepath = os.path.join('captures', filename)
    os.makedirs('captures', exist_ok=True)
    file.save(filepath)
    
    # === YOLOv8 + PaddleOCR 架構 ===
    
    # Step 1: YOLOv8 偵測車牌區域
    logger.info(f'開始處理圖片: {filepath}')
    plate_crops = detect_plate_with_yolo(filepath)
    logger.info(f'YOLOv8 偵測到 {len(plate_crops)} 個車牌區域')
    
    # Step 2: 對每個車牌區域進行 OCR 辨識（根據設定選擇引擎）
    all_ocr_texts = []
    plate_results = []
    ocr_engine = db.get_setting('ocr_engine', 'easyocr')
    
    for pc in plate_crops:
        # 應用透視變換
        transformed = apply_perspective_transform(pc['crop'])
        
        # 根據設定選擇 OCR 引擎
        if ocr_engine == 'ollama':
            ocr_texts = ocr_crop_with_ollama(transformed)
        elif ocr_engine == 'easyocr':
            ocr_texts = ocr_crop_with_easyocr(transformed)
        elif ocr_engine == 'tesseract':
            ocr_texts = ocr_crop_with_tesseract(transformed)
        elif ocr_engine == 'ulrixon':
            # Ulrixon 兩階段：自己偵測車牌（忽略已crop的圖，直接用原圖）
            # 但如果 YOLO 找到了 crops，仍用 crops 做 OCR
            ulrixon_results = ocr_with_ulrixon(image_path)
            ocr_texts.extend(ulrixon_results)
            if not ocr_texts:
                easy_results = ocr_crop_with_easyocr(transformed)
                ocr_texts.extend(easy_results)
        elif ocr_engine == 'paddle':
            ocr_texts = ocr_crop_with_paddle(transformed)
        else:  # 'both' or 'hybrid'
            # hybrid = EasyOCR 為主，Ollama 為輔
            ocr_texts = ocr_crop_with_easyocr(transformed)
            if not ocr_texts:
                ollama_texts = ocr_crop_with_ollama(transformed)
                ocr_texts.extend(ollama_texts)
        plates = filter_plate_text(ocr_texts)
        plate_results.append({
            'vehicle_type': pc['vehicle_type'],
            'vehicle_conf': pc['vehicle_conf'],
            'bbox': pc['bbox'],
            'ocr_texts': ocr_texts,
            'possible_plates': plates
        })
        all_ocr_texts.extend(ocr_texts)
        logger.info(f'  {pc["vehicle_type"]} 區域 OCR: {plates}')
    
    # Step 2.5: 如果 YOLO 找到 0 個車牌但選了 ulrixon，直接用 ulrixon 全圖偵測
    if not plate_crops and ocr_engine == 'ulrixon':
        logger.info('YOLO 未偵測到車牌，改用 Ulrixon 兩階段全圖偵測')
        ulrixon_results = ocr_with_ulrixon(filepath)
        if ulrixon_results:
            all_ocr_texts.extend(ulrixon_results)
            plate_results.append({
                'vehicle_type': 'Ulrixon',
                'vehicle_conf': 1.0,
                'bbox': None,
                'ocr_texts': ulrixon_results,
                'possible_plates': filter_plate_text(ulrixon_results)
            })
    
    # Step 3: 對全圖做 OCR（根據設定選擇引擎）
    if ocr_engine == 'ollama':
        full_ocr_texts = ocr_with_ollama(filepath)
    elif ocr_engine == 'easyocr':
        full_ocr_texts = ocr_with_easyocr(filepath)
        if not full_ocr_texts:
            full_ocr_texts = ocr_with_tesseract(filepath)
    elif ocr_engine == 'tesseract':
        full_ocr_texts = ocr_with_tesseract(filepath)
    elif ocr_engine == 'ulrixon':
        full_ocr_texts = ocr_with_ulrixon(filepath)
    elif ocr_engine == 'paddle':
        full_ocr_texts = ocr_crop_with_paddle(cv2.imread(filepath))
    else:  # 'both' or 'hybrid'
        # hybrid: 先用 EasyOCR，再用 Ollama/Tesseract 備援
        full_ocr_texts = ocr_with_easyocr(filepath)
        if not full_ocr_texts:
            try:
                ollama_full = ocr_with_ollama(filepath)
                full_ocr_texts.extend(ollama_full)
            except:
                pass
        if not full_ocr_texts:
            full_ocr_texts = ocr_with_tesseract(filepath)
    all_ocr_texts.extend(full_ocr_texts)
    full_plates = filter_plate_text(full_ocr_texts)
    
    # Step 4: 合併所有偵測到的車牌
    all_possible_plates = filter_plate_text(all_ocr_texts)
    
    # 合併車牌區域和全圖的結果
    combined_plates = all_possible_plates + full_plates
    
    # Step 4.5: 如果 EasyOCR 沒找到，嘗試 Tesseract OCR
    if not combined_plates:
        logger.info('EasyOCR 未找到車牌，嘗試 Tesseract OCR...')
        tess_texts = ocr_with_tesseract(filepath)
        tess_plates = filter_plate_text(tess_texts)
        if tess_plates:
            combined_plates = tess_plates
            logger.info(f'Tesseract OCR 找到了: {tess_plates}')
    
    # Step 5: 繪製 YOLOv8 偵測框（現在有車牌號碼了）
    if plate_crops:
        img = cv2.imread(filepath)
        for i, pc in enumerate(plate_crops):
            x1, y1, x2, y2 = pc['bbox']
            # 繪製綠色框 (B, G, R)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 3)
            # 找到這個區域對應的車牌號碼
            plate_label = plate_results[i]['possible_plates'][0] if plate_results[i]['possible_plates'] else f'#{i+1}'
            # 在框上方寫上車牌號碼
            cv2.putText(img, plate_label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
        # 儲存有框的圖片
        annotated_path = filepath.replace('.jpg', '_annotated.jpg').replace('.png', '_annotated.png')
        cv2.imwrite(annotated_path, img)
        logger.info(f'已繪製 YOLOv8 偵測框: {annotated_path}')
        # 回傳相對路徑，讓前端可以存取（需保留 captures/ 前綴）
        annotated_path = 'captures/' + os.path.basename(annotated_path)
    else:
        annotated_path = None
    
    # Step 6: 比對白名單
    matched_owner = None
    matched_plate = None
    is_blacklisted = False
    visitor_pass = None
    
    for plate in combined_plates:
        owner = db.get_owner_by_plate(plate)
        if owner:
            # 檢查是否為黑名單
            if owner.get('is_blacklist'):
                is_blacklisted = True
                add_alert('danger', f'🚫 黑名單車輛出現：{plate}（{owner.get("name", "未知")}）')
                db.add_record(plate, owner.get('name'), '⚠️ 黑名單車輛', filepath, direction='in')
                logger.warning(f'黑名單車輛 {plate} 被偵測到！')
                # 不開門
                continue
            
            # 檢查租賃是否過期
            expiry = owner.get('rental_expiry_date')
            owner_type = owner.get('owner_type', '')
            is_expired = False
            if expiry and owner_type in ('resident', 'owner', 'tenant'):
                try:
                    from datetime import datetime
                    expiry_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                    if datetime.now().date() > expiry_date:
                        is_expired = True
                except:
                    pass
            
            if is_expired:
                add_alert('warning', f'⚠️ 租賃已過期：{plate}（{owner.get("name", "未知")}，到期日：{expiry}）')
                # 允許進場但記錄為已過期
                if relay:
                    relay.open_gate()
                db.add_record(plate, owner.get('name'), f'⚠️ 已過期({expiry})', filepath, direction='in')
                logger.warning(f'車牌 {plate} 租賃已過期，到期日：{expiry}，已開門')
                continue
            
            matched_owner = owner
            matched_plate = plate
            # 找到匹配的車牌，開門
            if relay:
                relay.open_gate()
            db.add_record(plate, owner['name'], '✅ 允許進場', filepath, direction='in')
            logger.info(f'車牌 {plate} 比對成功，{owner["name"]} 已開門')

            # 自動建立停車記錄（如果功能開啟）
            if db.get_setting('auto_parking_session', 'false') == 'true':
                db.create_parking_session(plate, owner_id=owner.get('id'), direction='in')
                logger.info(f'已自動建立停車記錄：{plate}')

            break

    # 如果沒有匹配到車主，檢查是否有有效的訪客通行證
    if not matched_plate:
        for plate in combined_plates:
            visitor_pass = db.check_visitor_pass(plate)
            if visitor_pass:
                matched_plate = plate
                if relay:
                    relay.open_gate()
                db.add_record(plate, visitor_pass.get('visitor_name', '訪客'), '✅ 訪客通行', filepath, direction='in')
                db.use_visitor_pass(visitor_pass['id'])  # 標記為已使用
                logger.info(f'車牌 {plate} 持有有效訪客通行證，已開門')
                add_alert('info', f'👋 訪客通行：{plate}（{visitor_pass.get("visitor_name", "未知")}）')
                break
    
    # 如果都沒有匹配，記錄為不在白名單
    if not matched_plate and combined_plates:
        best_plate = combined_plates[0]
        db.add_record(best_plate, None, f'❌ {best_plate} 不在白名單', filepath, direction='in')
    
    # 計算平均 OCR 信心度
    avg_conf = 0
    if all_ocr_texts and isinstance(all_ocr_texts[0], dict):
        avg_conf = sum([t['confidence'] for t in all_ocr_texts]) / len(all_ocr_texts) * 100
    
    # 安全處理 ocr_texts
    if all_ocr_texts and isinstance(all_ocr_texts[0], dict):
        safe_ocr_texts = all_ocr_texts
    elif all_ocr_texts:
        safe_ocr_texts = [{'text': t, 'confidence': 0.5} for t in all_ocr_texts]
    else:
        safe_ocr_texts = []
    
    result = {
        'success': True,
        'filename': filename,
        'filepath': filepath,
        'yolo_detected': len(plate_crops),
        'annotated_image': annotated_path,
        'plate_results': [{
            'vehicle_type': pr['vehicle_type'],
            'vehicle_conf': pr['vehicle_conf'],
            'bbox': pr['bbox'],
            'plates': pr['possible_plates']
        } for pr in plate_results],
        'full_image_plates': full_plates,
        'all_plates': all_possible_plates,
        'best_plate': matched_plate or (combined_plates[0] if combined_plates else None),
        'ocr_confidence': round(avg_conf),
        'ocr_texts': safe_ocr_texts,
        'matched_plate': matched_owner['plate'] if matched_owner else None,
        'matched_owner': matched_owner['name'] if matched_owner else None,
        'allowed': matched_owner is not None,
        'message': (f'✅ {matched_owner["name"]} 驗證成功，已開門！' if matched_owner 
                    else f'YOLOv8 偵測到 {len(plate_crops)} 個車牌區域，{ocr_engine} 找到 {len(combined_plates)} 個可能車牌，無匹配白名單')
    }
    
    return jsonify(result)

# ============ 圖片上傳測試 ============

@app.route('/upload-test')
def upload_test():
    """圖片上傳測試頁面"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>車牌測試上傳</title>
        <meta charset="UTF-8">
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
            .upload-area { border: 3px dashed #ccc; padding: 40px; text-align: center; border-radius: 10px; }
            .preview { margin-top: 20px; max-width: 100%; }
            .result { margin-top: 20px; padding: 20px; border-radius: 10px; }
            .result.success { background: #d4edda; color: #155724; }
            .result.error { background: #f8d7da; color: #721c24; }
            .result.info { background: #d1ecf1; color: #0c5460; }
            input[type="file"] { margin: 20px 0; }
            button { background: #007bff; color: white; padding: 15px 30px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
            button:hover { background: #0056b3; }
            .plate-input { margin-top: 20px; }
            input[type="text"] { padding: 10px; font-size: 16px; width: 200px; }
        </style>
    </head>
    <body>
        <h1>🚗 車牌測試上傳</h1>
        <p>上傳一張包含車牌的圖片來測試系統</p>
        
        <div class="upload-area">
            <form method="POST" action="/api/upload_test" enctype="multipart/form-data">
                <input type="file" name="image" accept="image/*" required onchange="previewImage(this)">
                <br>
                <img id="preview" class="preview" style="display:none;">
                <br><br>
                <div class="plate-input">
                    <label>手動輸入車牌（用於比對）：</label><br><br>
                    <input type="text" name="manual_plate" placeholder="例如：ABC-1234">
                </div>
                <br><br>
                <button type="submit">上傳並測試</button>
            </form>
        </div>
        
        <div style="margin-top:30px;">
            <h3>📋 快速測試</h3>
            <p>上傳後，系統會：</p>
            <ol>
                <li>儲存圖片到 captures 資料夾</li>
                <li>如果選擇了「手動車牌」，系統會直接比對白名單</li>
                <li>開門（模擬模式）如果車牌在白名單中</li>
            </ol>
        </div>
        
        <script>
        function previewImage(input) {
            if (input.files && input.files[0]) {
                var reader = new FileReader();
                reader.onload = function(e) {
                    document.getElementById('preview').src = e.target.result;
                    document.getElementById('preview').style.display = 'block';
                }
                reader.readAsDataURL(input.files[0]);
            }
        }
        </script>
    </body>
    </html>
    '''

@app.route('/api/upload_test', methods=['POST'])
def api_upload_test():
    """處理上傳的圖片"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    
    if 'image' not in request.files:
        return jsonify({'success': False, 'message': '沒有上傳檔案'})
    
    file = request.files['image']
    manual_plate = request.form.get('manual_plate', '').strip().upper()
    
    if file.filename == '':
        return jsonify({'success': False, 'message': '沒有選擇檔案'})
    
    # 儲存圖片
    filename = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    filepath = os.path.join('captures', filename)
    os.makedirs('captures', exist_ok=True)
    file.save(filepath)
    
    result = {
        'success': True,
        'filename': filename,
        'filepath': filepath,
        'message': '圖片已儲存'
    }
    
    # 如果有填寫手動車牌，直接比對
    if manual_plate:
        owner = db.get_owner_by_plate(manual_plate)
        if owner:
            result['plate'] = manual_plate
            result['owner'] = owner['name']
            result['allowed'] = True
            result['message'] = f'車牌 {manual_plate} 比對成功！{owner["name"]} - 已開門'
            # 開門
            if relay:
                relay.open_gate()
            # 記錄
            db.add_record(manual_plate, owner['name'], '測試開門', filepath)
        else:
            result['plate'] = manual_plate
            result['allowed'] = False
            result['message'] = f'車牌 {manual_plate} 不在白名單中'
            db.add_record(manual_plate, None, '測試-無授權', filepath)
    
    return jsonify(result)

# ============ 計費管理 ============

@app.route('/billing')
def billing():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('billing.html',
        owners_count=len(db.get_owners()),
        today_count=db.get_record_count(date_filter=datetime.now().strftime('%Y-%m-%d'))
    )

@app.route('/parking-slots')
def parking_slots():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('parking_slots.html',
        owners_count=len(db.get_owners()),
        today_count=db.get_record_count(date_filter=datetime.now().strftime('%Y-%m-%d'))
    )

# --- 計費 API ---

@app.route('/api/billing/stats')
@app.route('/api/alerts')
def api_alerts():
    """取得系統通知"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # 只回傳未讀的，或最近 20 條
    unread = [a for a in alerts if not a['read']]
    return jsonify({
        'alerts': alerts[:20],
        'unread_count': len(unread)
    })

@app.route('/api/alerts/mark-read', methods=['POST'])
def api_alerts_mark_read():
    """標記通知為已讀"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    for a in alerts:
        a['read'] = True
    return jsonify({'success': True})

@app.route('/api/billing/unpaid')
def api_billing_unpaid():
    """取得拖欠帳單"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    days = int(request.args.get('days', 7))  # 預設超過7天
    unpaid = db.get_unpaid_bills(days_threshold=days)
    total = sum(b['amount'] for b in unpaid)
    
    return jsonify({
        'bills': unpaid,
        'total_unpaid': total,
        'count': len(unpaid)
    })

@app.route('/api/billing/stats')
def api_billing_stats():
    """取得帳單統計"""
    today = datetime.now().strftime('%Y-%m-%d')
    month_start = datetime.now().strftime('%Y-%m-01')
    
    today_summary = db.get_billing_summary(start_date=today, end_date=today)
    month_summary = db.get_billing_summary(start_date=month_start)
    total_summary = db.get_billing_summary()
    
    return jsonify({
        'today_income': today_summary['total_paid'] or 0,
        'unpaid_count': today_summary['unpaid_count'] or 0,
        'month_income': month_summary['total_paid'] or 0,
        'total_count': total_summary['total_count'] or 0
    })

@app.route('/api/billing/daily')
def api_billing_daily():
    """取得過去30天每日收入統計"""
    days = int(request.args.get('days', 30))
    
    daily_stats = []
    for i in range(days - 1, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        summary = db.get_billing_summary(start_date=date, end_date=date)
        daily_stats.append({
            'date': date,
            'income': summary['total_paid'] or 0,
            'count': summary['total_count'] or 0
        })
    
    return jsonify({'daily': daily_stats})

@app.route('/api/billing/rules')
def api_billing_rules():
    return jsonify(db.get_billing_rules())

@app.route('/api/billing/rules/<int:rule_id>')
def api_billing_rule(rule_id):
    rules = db.get_billing_rules()
    for r in rules:
        if r['id'] == rule_id:
            return jsonify(r)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/billing/rules', methods=['POST'])
def api_billing_rules_add():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    db.add_billing_rule(
        name=data['name'],
        car_type=data.get('car_type', 'all'),
        base_minutes=data.get('base_minutes', 0),
        base_fee=data.get('base_fee', 0),
        hourly_fee=data.get('hourly_fee', 30),
        daily_max=data.get('daily_max')
    )
    return jsonify({'success': True})

@app.route('/api/billing/rules/<int:rule_id>', methods=['PUT'])
def api_billing_rules_update(rule_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    db.update_billing_rule(
        rule_id=rule_id,
        name=data['name'],
        car_type=data.get('car_type', 'all'),
        base_minutes=data.get('base_minutes', 0),
        base_fee=data.get('base_fee', 0),
        hourly_fee=data.get('hourly_fee', 30),
        daily_max=data.get('daily_max'),
        is_active=data.get('is_active', 1)
    )
    return jsonify({'success': True})

@app.route('/api/billing/rules/<int:rule_id>', methods=['DELETE'])
def api_billing_rules_delete(rule_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db.delete_billing_rule(rule_id)
    return jsonify({'success': True})

@app.route('/api/billing/list')
def api_billing_list():
    page = int(request.args.get('page', 1))
    per_page = 20
    offset = (page - 1) * per_page
    plate = request.args.get('plate', '')
    status = request.args.get('status', '')
    date = request.args.get('date', '')
    
    items = db.get_billing_list(limit=per_page, offset=offset,
                                plate_filter=plate, status_filter=status, date_filter=date)
    total = db.get_billing_count(plate_filter=plate, status_filter=status, date_filter=date)
    
    return jsonify({
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page
    })

@app.route('/api/billing/<int:billing_id>/paid', methods=['POST'])
def api_billing_mark_paid(billing_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db.mark_billing_paid(billing_id)
    return jsonify({'success': True})

@app.route('/api/engineer/verify', methods=['POST'])
def api_engineer_verify():
    """工程商密碼驗證"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': '未登入'})
    
    import hashlib
    data = request.json
    password = data.get('password', '')
    
    # 取得工程商密碼（hash 比對）
    stored_hash = db.get_setting('engineer_password_hash', '')
    if not stored_hash:
        # 如果沒有設定工程商密碼，就不允許進入
        return jsonify({'success': False, 'error': '工程商密碼未設定'})
    
    verified = False
    
    # 支援新舊兩種格式
    if stored_hash.startswith('pbkdf2:') or stored_hash.startswith('scrypt:'):
        # 新格式：werkzeug scrypt 或 pbkdf2
        verified = check_password_hash(stored_hash, password)
    else:
        # 舊格式：SHA256（純 hex，64字元）→ 遷移到新格式
        import re
        if re.fullmatch(r'[a-f0-9]{64}', stored_hash):
            input_hash = hashlib.sha256(password.encode()).hexdigest()
            if input_hash == stored_hash:
                # 密碼正確，自動升級為 werkzeug 新格式
                new_hash = generate_password_hash(password)
                db.set_setting('engineer_password_hash', new_hash)
                logger.info('工程商密碼已自動升級為安全的 hash 格式')
                verified = True
    
    if verified:
        session['engineer_mode'] = True
        return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': '密碼錯誤'})

@app.route('/api/ollama/models')
def api_ollama_models():
    """取得 Ollama 可用的模型列表"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    import requests
    url = request.args.get('url', 'http://localhost:11434')
    
    try:
        response = requests.get(f'{url}/api/tags', timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = data.get('models', [])
            return jsonify({'models': models})
        else:
            return jsonify({'error': f'HTTP {response.status_code}'}), 400
    except requests.exceptions.ConnectionError:
        return jsonify({'error': '無法連線到 Ollama，請確認 Ollama 已啟動'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400
@app.route('/api/settings/set-engineer-password', methods=['POST'])
def api_set_engineer_password():
    """設定工程商密碼"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': '未登入'})
    
    data = request.json
    password = data.get('password', '')
    
    if not password:
        return jsonify({'success': False, 'error': '密碼不能為空'})
    
    # 使用 werkzeug 安全 hash（pbkdf2 + salt，比 SHA256 安全）
    password_hash = generate_password_hash(password)
    db.set_setting('engineer_password_hash', password_hash)
    
    return jsonify({'success': True, 'message': '工程商密碼已設定'})

# --- 車位 API ---

@app.route('/api/parking/slots/stats')
def api_parking_slots_stats():
    slots = db.get_parking_slots()
    available = len([s for s in slots if s['status'] == 'available'])
    occupied = len([s for s in slots if s['status'] == 'occupied'])
    reserved = len([s for s in slots if s['status'] == 'reserved'])
    return jsonify({
        'total': len(slots),
        'available': available,
        'occupied': occupied,
        'reserved': reserved
    })

@app.route('/api/parking/slots')
def api_parking_slots():
    return jsonify(db.get_parking_slots())

@app.route('/api/parking/slots', methods=['POST'])
def api_parking_slots_add():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    ok, msg = db.add_parking_slot(data['slot_number'])
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/parking/slots/<int:slot_id>', methods=['DELETE'])
def api_parking_slots_delete(slot_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    db.delete_parking_slot(slot_id)
    return jsonify({'success': True})

@app.route('/api/parking/slots/<int:slot_id>/cancel-reservation', methods=['POST'])
def api_parking_slots_cancel_reservation(slot_id):
    """取消車位預訂"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    slot = db.get_parking_slot(slot_id)
    if not slot:
        return jsonify({'success': False, 'error': '找不到車位'})
    
    if slot['status'] != 'reserved':
        return jsonify({'success': False, 'error': '此車位不是預訂狀態'})
    
    # 將車位改為可用狀態，清空車牌和owner_id
    db.update_parking_slot(slot_id, 'available', None, None)
    
    return jsonify({'success': True, 'message': '已取消預訂'})

@app.route('/api/parking/sessions/active')
def api_parking_sessions_active():
    sessions = db.get_active_sessions()
    now = datetime.now()
    result = []
    for s in sessions:
        entry = s['entry_time']
        if isinstance(entry, str):
            entry_str = entry.split('.')[0]
            entry = datetime.strptime(entry_str, '%Y-%m-%d %H:%M:%S')
        duration = int((now - entry).total_seconds() / 60)
        fee = db.calculate_fee(duration)
        result.append({
            **s,
            'duration': f'{duration} 分',
            'fee': fee
        })
    return jsonify(result)

@app.route('/api/parking/entry', methods=['POST'])
def api_parking_entry():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    plate = data.get('plate', '').upper()
    slot_number = data.get('slot_number')
    
    owner = db.get_owner_by_plate(plate)
    owner_id = owner['id'] if owner else None
    
    session_id, msg = db.create_parking_session(plate, slot_number, owner_id)
    
    if session_id:
        if slot_number:
            db.assign_slot_to_plate(slot_number, plate, owner_id)
        # 開門
        if relay:
            relay.open_gate()
        # 記錄
        db.add_record(plate, owner['name'] if owner else None, '進場')
        return jsonify({'success': True, 'message': '進場成功', 'session_id': session_id})
    else:
        return jsonify({'success': False, 'error': msg})

@app.route('/api/parking/exit', methods=['POST'])
def api_parking_exit():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    
    plate = data.get('plate', '').upper()
    slot_number = data.get('slot_number')
    reason = data.get('reason', 'normal')  # normal, free, discount, error, other
    override_fee = data.get('fee')  # 如果有提供，代表要覆寫費用
    note = data.get('note', '')
    
    if slot_number:
        # 透過車位找車牌
        slot = db.get_slot_by_number(slot_number)
        if slot and slot['plate']:
            plate = slot['plate']
    
    owner = db.get_owner_by_plate(plate) if plate else None
    
    session_id, result = db.end_parking_session(plate, owner_type=owner.get('owner_type', 'visitor') if owner else 'visitor')
    
    # 釋放車位
    if slot_number:
        db.free_slot(slot_number)
    
    if session_id is None:
        # 車位已釋放，但沒有實際的停車記錄（可能手動標記占用的）
        return jsonify({'success': True, 'fee': 0, 'duration': 0, 'message': f'車位已釋放（{plate}）'})
    
    # 計算費用
    fee = result
    if override_fee is not None:
        fee = override_fee
    elif reason in ('free', 'error'):
        fee = 0
    else:
        # 嘗試自動釋放
        active = db.get_parking_session_by_plate(plate)
        if active and active['slot_number']:
            db.free_slot(active['slot_number'])
    
    # 開門
    if relay:
        relay.open_gate()
    
    # 記錄（根據原因調整描述）
    reason_text = {
        'normal': '正常繳費離場',
        'free': '免費放行',
        'discount': '優惠折扣離場',
        'error': '作業失誤離場',
        'other': '其他原因離場'
    }.get(reason, '離場')
    
    note_suffix = f' ({note})' if note else ''
    db.add_record(plate, owner['name'] if owner else None, reason_text + note_suffix, direction='out')
    
    # 建立帳單
    session_data = db.get_parking_session_by_plate(plate) if plate else None
    if session_data:
        entry = session_data['entry_time']
        exit_t = datetime.now()
        if isinstance(entry, str):
            entry_str = entry.split('.')[0]
            entry = datetime.strptime(entry_str, '%Y-%m-%d %H:%M:%S')
        duration = int((exit_t - entry).total_seconds() / 60)
        db.create_billing(
            session_id=session_id,
            plate=plate,
            owner_name=owner['name'] if owner else None,
            amount=fee,
            duration_minutes=duration,
            entry_time=entry,
            exit_time=exit_t,
            note=note
        )
    
    return jsonify({
        'success': True,
        'fee': fee,
        'duration': duration if session_data else 0
    })

@app.route('/api/parking/block', methods=['POST'])
def api_parking_block():
    """禁止車輛離場（將車牌加入黑名單並釋放車位）"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    
    plate = data.get('plate', '').upper()
    reason = data.get('reason', '')
    
    if not plate:
        return jsonify({'success': False, 'error': '請提供車牌'})
    
    # 取得車輛資訊
    session_data = db.get_parking_session_by_plate(plate)
    if not session_data:
        return jsonify({'success': False, 'error': '找不到該車牌的停車記錄'})
    
    # 釋放車位
    if session_data and session_data.get('slot_number'):
        db.free_slot(session_data['slot_number'])
    
    # 取得車主資訊（用於計費）
    owner = db.get_owner_by_plate(plate)
    owner_type = owner.get('owner_type', 'visitor') if owner else 'visitor'
    
    # 刪除停車 session
    db.end_parking_session(plate, owner_type=owner_type)
    
    # 加入黑名單
    if owner:
        db.update_owner(
            owner_id=owner['id'],
            name=owner['name'],
            phone=owner['phone'],
            plate=owner['plate'],
            car_type=owner.get('car_type', '轎車'),
            slot_number=None,
            note=owner.get('note', ''),
            is_blacklist=1  # 設為黑名單
        )
    else:
        # 如果沒有車主記錄，至少記錄到黑名單
        db.add_owner(
            name='黑名單-' + plate,
            phone='',
            plate=plate,
            car_type='轎車',
            note=f'禁止離場：{reason}' if reason else '禁止離場'
        )
        # 將其設為黑名單
        owners = db.get_owners()
        for o in owners:
            if o['plate'] == plate:
                db.update_owner(
                    owner_id=o['id'],
                    name=o['name'],
                    phone=o['phone'],
                    plate=o['plate'],
                    car_type=o.get('car_type', '轎車'),
                    slot_number=None,
                    note=o.get('note', ''),
                    is_blacklist=1
                )
                break
    
    # 記錄
    db.add_record(plate, None, f'禁止離場：{reason}' if reason else '禁止離場', direction='out')
    
    return jsonify({
        'success': True,
        'message': f'{plate} 已禁止離場'
    })

@app.route('/api/owners/<int:owner_id>/assign-slot', methods=['POST'])
def api_owners_assign_slot(owner_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    slot_number = data.get('slot_number')
    owner = db.get_owner_by_id(owner_id)
    
    if not owner:
        return jsonify({'success': False, 'message': '找不到車主'})
    
    # 更新車主的車位
    db.update_owner(owner_id, owner['name'], owner['phone'], owner['plate'], 
                    owner.get('car_type', '轎車'), slot_number, owner.get('note', ''), 
                    owner.get('is_blacklist', 0))
    
    # 更新車位狀態
    if slot_number:
        db.assign_slot_to_plate(slot_number, owner['plate'], owner_id)
    
    return jsonify({'success': True, 'message': '車位分配成功'})

# ============ 啟動 ============


# ============ 月租到期提醒 API ============
@app.route('/api/owners/expiring')
def api_owners_expiring():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    days = int(request.args.get('days', 30))
    expiring = db.get_owners_expiring_soon(days)
    return jsonify({'owners': expiring})

@app.route('/api/owners/<int:owner_id>/expiry', methods=['POST'])
def api_owners_update_expiry(owner_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    expiry_date = data.get('expiry_date', '')
    try:
        db.update_owner_expiry(owner_id, expiry_date or None)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def check_rental_expiry_alerts():
    try:
        expiring = db.get_owners_expiring_soon(7)
        for owner in expiring:
            plate = owner.get('plate', '')
            expiry = owner.get('rental_expiry_date', '')
            if not expiry:
                continue
            days_left = (datetime.strptime(expiry, '%Y-%m-%d').date() - datetime.now().date()).days
            if days_left < 0:
                msg = f'[已過期] {owner["name"]} ({plate}) 月租已過期'
            elif days_left == 0:
                msg = f'[今日到期] {owner["name"]} ({plate}) 月租今天到期'
            else:
                msg = f'[到期提醒] {owner["name"]} ({plate}) 月租將於 {days_left} 天後到期（{expiry}）'
            already = any(a.get('message','') == msg and (datetime.now() - a.get('time', datetime.min)).days == 0 for a in alerts)
            if not already:
                add_alert('warning', msg)
    except Exception as e:
        logger.error(f'月租到期檢查失敗: {e}')

# ============ 啟動 ============

    print('=' * 50)
    print('  車牌辨識開門系統')
    print('  網頁管理: http://localhost:5000')
    print('  預設帳號: admin')
    print('  預設密碼: admin123')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=(os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')))
