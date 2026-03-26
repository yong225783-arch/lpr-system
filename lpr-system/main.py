"""
車牌辨識開門系統 - 主程式
Flask + OpenCV + EasyOCR + USB Relay
"""

import os
import sys
import io
import time
import logging
import threading
import cv2
import numpy as np
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, send_file, flash
)
from werkzeug.security import generate_password_hash
import database as db

# ============ Flask App ============

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'lpr-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============ LPR Module ============

class PlateRecognizer:
    def __init__(self):
        self.camera = None
        self.running = False
        self.thread = None
        self.last_plate = None
        self.last_plate_time = 0
        self.cooldown = 5  # 同車牌冷卻時間（秒）

    def set_camera(self, source=0):
        """
        設定攝像頭
        source: int = USB webcam 編號
                str = RTSP URL, 例如 'rtsp://admin:password@192.168.1.100:554/stream1'
        """
        if isinstance(source, str):
            # RTSP URL 或其他串流 URL
            self.camera = cv2.VideoCapture(source)
            logger.info(f'已連接 IP Cam: {source}')
        else:
            self.camera = cv2.VideoCapture(source)
            logger.info(f'已連接 Webcam: {source}')

        if not self.camera.isOpened():
            logger.error(f'無法開啟攝影機 {source}')
            return False
        logger.info(f'攝影機已開啟')
        return True

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

    def process_frame(self, frame):
        """處理單一幀，嘗試辨識車牌"""
        edged = self.preprocess(frame)
        plate_contour = self.find_plate_contour(edged)

        if plate_contour is not None:
            mask = np.zeros_like(gray)
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

    def capture_and_recognize(self):
        """擷取並辨識一次"""
        if not self.camera or not self.camera.isOpened():
            return None

        ret, frame = self.camera.read()
        if not ret:
            return None

        # 這裡需要 EasyOCR，見下面章節說明
        # 示意：使用 OCR 辨識
        return frame

    def start_continuous(self, callback):
        """連續偵測執行緒"""
        def run():
            while self.running:
                ret, frame = self.camera.read()
                if not ret:
                    time.sleep(0.1)
                    continue

                # 車牌辨識流程
                processed, plate = self.process_frame(frame)

                if plate is not None:
                    now = time.time()
                    if now - self.last_plate_time > self.cooldown:
                        self.last_plate_time = now
                        callback(frame, self.last_plate)

                time.sleep(0.05)

        self.running = True
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.camera:
            self.camera.release()

# ============ 初始化 ============

# 資料庫
db.init_db()
logger.info('資料庫初始化完成')

# 車牌辨識器
lpr = PlateRecognizer()

# 嘗試開啟攝像頭（支援 webcam 或 RTSP URL）
# 設定環境變量 CAMERA_SOURCE，例如：
#   CAMERA_SOURCE=0              (使用第0個 webcam)
#   CAMERA_SOURCE=rtsp://...     (使用 IP Cam RTSP 串流)
camera_source = os.environ.get('CAMERA_SOURCE', '0')
if camera_source.isdigit():
    camera_source = int(camera_source)

if not lpr.set_camera(camera_source):
    logger.warning('無法開啟攝影機，網頁模式仍可使用')

# 嘗試連接繼電器
relay_port = os.environ.get('RELAY_PORT', None)
simulate_relay = os.environ.get('SIMULATE_RELAY', '').lower() in ('1', 'true', 'yes')

if simulate_relay:
    relay = RelayController(simulate=True)
    logger.info('繼電器：模擬模式（無硬體）')
elif relay_port:
    from relay import RelayController
    relay = RelayController(port=relay_port)
    if relay.connect():
        logger.info(f'繼電器已連接: {relay_port}')
    else:
        relay = None
        logger.warning('繼電器連接失敗，開門功能將無法使用')
else:
    relay = RelayController(simulate=True)
    logger.info('繼電器：模擬模式（無硬體）')

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
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user = db.verify_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        flash('帳號或密碼錯誤', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- 車主管理 ---

@app.route('/owners')
def owners():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    owners_list = db.get_owners()
    return render_template('owners.html', owners=owners_list)

@app.route('/owners/add', methods=['POST'])
def owners_add():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    plate = request.form.get('plate', '').strip().upper()
    note = request.form.get('note', '').strip()
    if not name or not plate:
        flash('姓名和車牌必填', 'error')
    else:
        ok, msg = db.add_owner(name, phone, plate, note)
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
    note = request.form.get('note', '').strip()
    is_blacklist = 1 if request.form.get('is_blacklist') else 0
    ok, msg = db.update_owner(owner_id, name, phone, plate, note, is_blacklist)
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
        date_filter=date_filter
    )

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
    plate = request.json.get('plate', '').upper()
    image_path = request.json.get('image_path')

    owner = db.get_owner_by_plate(plate)
    if owner:
        result = '已授權'
        if relay:
            relay.open_gate()
        if image_path:
            db.add_record(plate, owner['name'], result, image_path)
        else:
            db.add_record(plate, owner['name'], result)
        return jsonify({'allowed': True, 'owner': owner['name']})
    else:
        result = '未授權'
        db.add_record(plate, None, result, image_path)
        return jsonify({'allowed': False, 'plate': plate})

# --- 認列拍照 ---

@app.route('/capture')
def capture():
    """手動拍照"""
    if 'user_id' not in session:
        return jsonify({'error': '未登入'})
    if lpr.camera and lpr.camera.isOpened():
        ret, frame = lpr.camera.read()
        if ret:
            filename = f"manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
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
    return render_template('settings.html',
        camera_type=db.get_setting('camera_type', 'webcam'),
        camera_index=db.get_setting('camera_index', '0'),
        rtsp_url=db.get_setting('rtsp_url', ''),
        relay_port=db.get_setting('relay_port', ''),
        open_duration=float(db.get_setting('open_duration', '1.5')),
        owners_count=len(db.get_owners()),
        today_count=db.get_record_count(date_filter=datetime.now().strftime('%Y-%m-%d'))
    )

@app.route('/settings/save', methods=['POST'])
def settings_save():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    section = request.form.get('section')
    if section == 'camera':
        db.set_setting('camera_type', request.form.get('camera_type', 'webcam'))
        db.set_setting('camera_index', request.form.get('camera_index', '0'))
        db.set_setting('rtsp_url', request.form.get('rtsp_url', ''))
        flash('攝影機設定已儲存', 'success')
    elif section == 'relay':
        db.set_setting('relay_port', request.form.get('relay_port', ''))
        db.set_setting('open_duration', request.form.get('open_duration', '1.5'))
        # 更新 relay port
        global relay
        if relay:
            relay.close()
        port = request.form.get('relay_port', '')
        if port:
            from relay import RelayController
            relay = RelayController(port=port)
            if relay.connect():
                logger.info(f'繼電器已更新: {port}')
        flash('繼電器設定已儲存', 'success')
    elif section == 'password':
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass and new_pass == confirm:
            db.change_password(session['user_id'], new_pass)
            flash('密碼已修改', 'success')
        elif new_pass and new_pass != confirm:
            flash('兩次密碼不同', 'error')
    return redirect(url_for('settings'))

# ============ 測試 API ============

@app.route('/api/test_camera')
def test_camera():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    # 嘗試讀取一幀
    if lpr.camera and lpr.camera.isOpened():
        ret, frame = lpr.camera.read()
        if ret:
            filename = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join('captures', filename)
            os.makedirs('captures', exist_ok=True)
            cv2.imwrite(filepath, frame)
            return jsonify({'success': True, 'message': '截圖已儲存'})
    return jsonify({'success': False, 'message': '無法連接攝影機'})

@app.route('/api/test_relay', methods=['POST'])
def test_relay():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '未登入'})
    if relay:
        ok = relay.open_gate()
        return jsonify({'success': ok, 'message': '' if ok else '連接失敗'})
    return jsonify({'success': False, 'message': '繼電器未連接'})

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

@app.route('/api/parking/sessions/active')
def api_parking_sessions_active():
    sessions = db.get_active_sessions()
    now = datetime.now()
    result = []
    for s in sessions:
        entry = s['entry_time']
        if isinstance(entry, str):
            entry = datetime.strptime(entry, '%Y-%m-%d %H:%M:%S')
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
    
    if slot_number:
        # 透過車位找車牌
        slot = db.get_slot_by_number(slot_number)
        if slot and slot['plate']:
            plate = slot['plate']
    
    owner = db.get_owner_by_plate(plate) if plate else None
    
    session_id, result = db.end_parking_session(plate)
    
    if session_id is None:
        return jsonify({'error': result})
    
    fee = result
    
    # 釋放車位
    if slot_number:
        db.free_slot(slot_number)
    else:
        # 嘗試自動釋放
        active = db.get_parking_session_by_plate(plate)
        if active and active['slot_number']:
            db.free_slot(active['slot_number'])
    
    # 開門
    if relay:
        relay.open_gate()
    
    # 記錄
    db.add_record(plate, owner['name'] if owner else None, '離場')
    
    # 建立帳單
    session_data = db.get_parking_session_by_plate(plate) if plate else None
    if session_data:
        entry = session_data['entry_time']
        exit_t = datetime.now()
        if isinstance(entry, str):
            entry = datetime.strptime(entry, '%Y-%m-%d %H:%M:%S')
        duration = int((exit_t - entry).total_seconds() / 60)
        db.create_billing(
            session_id=session_id,
            plate=plate,
            owner_name=owner['name'] if owner else None,
            amount=fee,
            duration_minutes=duration,
            entry_time=entry,
            exit_time=exit_t
        )
    
    return jsonify({
        'success': True,
        'fee': fee,
        'duration': duration if session_data else 0
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

if __name__ == '__main__':
    print('=' * 50)
    print('  車牌辨識開門系統')
    print('  網頁管理: http://localhost:5000')
    print('  預設帳號: admin')
    print('  預設密碼: admin123')
    print('=' * 50)
    app.run(host='0.0.0.0', port=5000, debug=True)
