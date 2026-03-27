import sqlite3
import os
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE = os.path.join(os.path.dirname(__file__), 'lpr.db')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # 車主資料表
    c.execute('''
        CREATE TABLE IF NOT EXISTS owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT,
            name TEXT NOT NULL,
            phone TEXT,
            plate TEXT UNIQUE NOT NULL,
            car_type TEXT DEFAULT '轎車',
            owner_type TEXT DEFAULT 'resident',  -- resident (月租戶) or visitor (臨停)
            slot_number TEXT,
            note TEXT,
            is_blacklist INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 訪客通行證表（臨時通行）
    c.execute('''
        CREATE TABLE IF NOT EXISTS visitor_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            visitor_name TEXT,
            visitor_phone TEXT,
            valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            valid_until TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',  -- active, used, expired, cancelled
            note TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 開門紀錄表
    c.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            owner_name TEXT,
            result TEXT NOT NULL,
            image_path TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 管理員帳號表
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 系統設定表
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # ============ 新增：車位資料表 ============
    c.execute('''
        CREATE TABLE IF NOT EXISTS parking_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_number TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'available',  -- available, occupied, reserved, disabled
            plate TEXT,
            owner_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES owners(id)
        )
    ''')

    # ============ 新增：停車 session 表（追蹤目前停車中）============
    c.execute('''
        CREATE TABLE IF NOT EXISTS parking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            owner_id INTEGER,
            slot_number TEXT,
            entry_time TIMESTAMP NOT NULL,
            exit_time TIMESTAMP,
            status TEXT DEFAULT 'parking',  -- parking, exited
            fee INTEGER DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES owners(id)
        )
    ''')

    # ============ 新增：帳單/收費記錄表 ============
    c.execute('''
        CREATE TABLE IF NOT EXISTS billing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            plate TEXT NOT NULL,
            owner_name TEXT,
            amount INTEGER NOT NULL,  -- 金額（分）
            duration_minutes INTEGER,  -- 停車分鐘數
            entry_time TIMESTAMP,
            exit_time TIMESTAMP,
            payment_method TEXT DEFAULT 'cash',  -- cash, card, transfer
            payment_status TEXT DEFAULT 'unpaid',  -- unpaid, paid
            paid_at TIMESTAMP,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES parking_sessions(id)
        )
    ''')

    # ============ 新增：收費規則表 ============
    c.execute('''
        CREATE TABLE IF NOT EXISTS billing_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            car_type TEXT DEFAULT 'all',  -- all, 轎車, 休旅車, 機車
            billing_type TEXT DEFAULT 'hourly',  -- hourly (臨停) or monthly (月租)
            base_minutes INTEGER DEFAULT 0,  -- 免費分鐘數
            base_fee INTEGER DEFAULT 0,  --  base_time 內的費用（分）
            hourly_fee INTEGER DEFAULT 100,  -- 每小時費用（分）
            daily_max INTEGER,  -- 每日上限（分）
            monthly_fee INTEGER,  -- 月租費（月租戶用）
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 預設管理員帳號 admin / admin123
    c.execute('SELECT * FROM users WHERE username = ?', ('admin',))
    if not c.fetchone():
        c.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            ('admin', generate_password_hash('admin123'), 'admin')
        )
    
    # Migration: 新增 billing_rules 欄位（如果還沒有）
    try:
        c.execute("ALTER TABLE billing_rules ADD COLUMN billing_type TEXT DEFAULT 'hourly'")
    except:
        pass
    try:
        c.execute("ALTER TABLE billing_rules ADD COLUMN monthly_fee INTEGER")
    except:
        pass
    
    # Migration: 新增 owners 欄位（如果還沒有）
    try:
        c.execute("ALTER TABLE owners ADD COLUMN owner_type TEXT DEFAULT 'resident'")
    except:
        pass

    # 訪客通行證表（如果還沒有）
    try:
        c.execute('''
            CREATE TABLE IF NOT EXISTS visitor_passes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate TEXT NOT NULL,
                visitor_name TEXT,
                visitor_phone TEXT,
                valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                valid_until TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'active',
                note TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    except:
        pass

    # 預設收費規則
    c.execute('SELECT * FROM billing_rules WHERE name = ?', ('default',))
    if not c.fetchone():
        c.execute(
            'INSERT INTO billing_rules (name, car_type, billing_type, base_minutes, base_fee, hourly_fee, daily_max, monthly_fee) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            ('一般臨停', 'all', 'hourly', 15, 0, 30, 500, 0)
        )
        c.execute(
            'INSERT INTO billing_rules (name, car_type, billing_type, monthly_fee) VALUES (?, ?, ?, ?)',
            ('月租戶', 'all', 'monthly', 3000)
        )

    conn.commit()
    conn.close()

# ============ 車主管理 ============

def get_owners():
    conn = get_db()
    rows = conn.execute('SELECT * FROM owners ORDER BY created_at DESC').fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_owner_by_plate(plate):
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM owners WHERE plate = ? AND is_blacklist = 0',
        (plate,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_owner_by_id(owner_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM owners WHERE id = ?', (owner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_owner(name, phone, plate, car_type='轎車', slot_number=None, note='', owner_id=None, member_id=None, owner_type='resident'):
    conn = get_db()
    try:
        if owner_id:
            # 手動指定 ID
            conn.execute(
                'INSERT INTO owners (id, member_id, name, phone, plate, car_type, owner_type, slot_number, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (owner_id, member_id, name, phone, plate, car_type, owner_type, slot_number if slot_number else None, note)
            )
        else:
            conn.execute(
                'INSERT INTO owners (member_id, name, phone, plate, car_type, owner_type, slot_number, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (member_id, name, phone, plate, car_type, owner_type, slot_number if slot_number else None, note)
            )
        conn.commit()
        conn.close()
        return True, '新增成功'
    except sqlite3.IntegrityError as e:
        conn.close()
        if 'UNIQUE constraint failed: owners.id' in str(e):
            return False, 'ID 已經存在'
        return False, '車牌已存在'

def update_owner(owner_id, name, phone, plate, car_type, slot_number, note, is_blacklist, member_id=None, owner_type='resident'):
    conn = get_db()
    try:
        conn.execute(
            '''UPDATE owners SET member_id=?, name=?, phone=?, plate=?, car_type=?, owner_type=?, slot_number=?, note=?, is_blacklist=? WHERE id=?''',
            (member_id, name, phone, plate, car_type, owner_type, slot_number, note, is_blacklist, owner_id)
        )
        conn.commit()
        conn.close()
        return True, '更新成功'
    except sqlite3.IntegrityError:
        conn.close()
        return False, '車牌已存在'

def delete_owner(owner_id):
    conn = get_db()
    conn.execute('DELETE FROM owners WHERE id = ?', (owner_id,))
    conn.commit()
    conn.close()

def delete_record(record_id):
    conn = get_db()
    conn.execute('DELETE FROM records WHERE id = ?', (record_id,))
    conn.commit()
    conn.close()

def update_record_note(record_id, note):
    conn = get_db()
    conn.execute('UPDATE records SET note = ? WHERE id = ?', (note, record_id))
    conn.commit()
    conn.close()

# ============ 開門紀錄 ============

def add_record(plate, owner_name, result, image_path=None, note=''):
    conn = get_db()
    conn.execute(
        'INSERT INTO records (plate, owner_name, result, image_path, note) VALUES (?, ?, ?, ?, ?)',
        (plate, owner_name, result, image_path, note)
    )
    conn.commit()
    conn.close()

def get_records(limit=100, offset=0, plate_filter=None, date_filter=None):
    conn = get_db()
    query = 'SELECT * FROM records WHERE 1=1'
    params = []
    if plate_filter:
        query += ' AND plate LIKE ?'
        params.append(f'%{plate_filter}%')
    if date_filter:
        query += ' AND DATE(created_at) = ?'
        params.append(date_filter)
    query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_record_count(plate_filter=None, date_filter=None):
    conn = get_db()
    query = 'SELECT COUNT(*) FROM records WHERE 1=1'
    params = []
    if plate_filter:
        query += ' AND plate LIKE ?'
        params.append(f'%{plate_filter}%')
    if date_filter:
        query += ' AND DATE(created_at) = ?'
        params.append(date_filter)
    count = conn.execute(query, params).fetchone()[0]
    conn.close()
    return count

# ============ 認證 ============

def verify_user(username, password):
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM users WHERE username = ?', (username,)
    ).fetchone()
    conn.close()
    if row and check_password_hash(row['password_hash'], password):
        return dict(row)
    return None

def change_password(user_id, new_password):
    conn = get_db()
    conn.execute(
        'UPDATE users SET password_hash = ? WHERE id = ?',
        (generate_password_hash(new_password), user_id)
    )
    conn.commit()
    conn.close()

# ============ 設定 ============

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, value)
    )
    conn.commit()
    conn.close()

# ============ 車位管理 ============

def get_parking_slots():
    conn = get_db()
    rows = conn.execute('''
        SELECT ps.*, o.name as owner_name, o.phone 
        FROM parking_slots ps 
        LEFT JOIN owners o ON ps.owner_id = o.id 
        ORDER BY ps.slot_number
    ''').fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_parking_slot(slot_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM parking_slots WHERE id = ?', (slot_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_slot_by_number(slot_number):
    conn = get_db()
    row = conn.execute('SELECT * FROM parking_slots WHERE slot_number = ?', (slot_number,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_parking_slot(slot_number, status='available'):
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO parking_slots (slot_number, status) VALUES (?, ?)',
            (slot_number, status)
        )
        conn.commit()
        conn.close()
        return True, '車位新增成功'
    except sqlite3.IntegrityError:
        conn.close()
        return False, '車位已存在'

def update_parking_slot(slot_id, status, plate=None, owner_id=None):
    conn = get_db()
    conn.execute(
        'UPDATE parking_slots SET status=?, plate=?, owner_id=? WHERE id=?',
        (status, plate, owner_id, slot_id)
    )
    conn.commit()
    conn.close()

def delete_parking_slot(slot_id):
    conn = get_db()
    conn.execute('DELETE FROM parking_slots WHERE id = ?', (slot_id,))
    conn.commit()
    conn.close()

def get_available_slots():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM parking_slots WHERE status = 'available' ORDER BY slot_number"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def assign_slot_to_plate(slot_number, plate, owner_id=None):
    conn = get_db()
    # 檢查車位是否已被占用
    slot = conn.execute('SELECT * FROM parking_slots WHERE slot_number = ?', (slot_number,)).fetchone()
    if slot and slot['status'] == 'occupied' and slot['plate']:
        conn.close()
        return False, '車位已被占用'
    
    conn.execute(
        "UPDATE parking_slots SET status='occupied', plate=?, owner_id=? WHERE slot_number=?",
        (plate, owner_id, slot_number)
    )
    conn.commit()
    conn.close()
    return True, '車位分配成功'

def free_slot(slot_number):
    conn = get_db()
    conn.execute(
        "UPDATE parking_slots SET status='available', plate=NULL, owner_id=NULL WHERE slot_number=?",
        (slot_number,)
    )
    conn.commit()
    conn.close()

# ============ 停車 Session 管理 ============

def create_parking_session(plate, slot_number=None, owner_id=None):
    conn = get_db()
    # 檢查是否已有進行中的 session
    existing = conn.execute(
        "SELECT * FROM parking_sessions WHERE plate=? AND status='parking'",
        (plate,)
    ).fetchone()
    
    if existing:
        conn.close()
        return None, '此車牌已有進行中的停車記錄'
    
    conn.execute(
        'INSERT INTO parking_sessions (plate, slot_number, owner_id, entry_time, status) VALUES (?, ?, ?, ?, ?)',
        (plate, slot_number, owner_id, datetime.now(), 'parking')
    )
    conn.commit()
    session_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()
    return session_id, '進場成功'

def end_parking_session(plate, note=''):
    conn = get_db()
    session = conn.execute(
        "SELECT * FROM parking_sessions WHERE plate=? AND status='parking' ORDER BY entry_time DESC LIMIT 1",
        (plate,)
    ).fetchone()
    
    if not session:
        conn.close()
        return None, '找不到進行中的停車記錄'
    
    exit_time = datetime.now()
    entry_time = datetime.strptime(session['entry_time'], '%Y-%m-%d %H:%M:%S') if isinstance(session['entry_time'], str) else session['entry_time']
    duration_minutes = int((exit_time - entry_time).total_seconds() / 60)
    
    # 計算費用
    fee = calculate_fee(duration_minutes, session['slot_number'])
    
    conn.execute(
        'UPDATE parking_sessions SET exit_time=?, status=?, fee=? WHERE id=?',
        (exit_time, 'exited', fee, session['id'])
    )
    conn.commit()
    conn.close()
    
    return session['id'], fee

def get_active_sessions():
    conn = get_db()
    rows = conn.execute('''
        SELECT ps.*, o.name as owner_name, o.phone 
        FROM parking_sessions ps 
        LEFT JOIN owners o ON ps.owner_id = o.id 
        WHERE ps.status = 'parking' 
        ORDER BY ps.entry_time DESC
    ''').fetchall()
    conn.close()
    
    result = []
    now = datetime.now()
    for row in rows:
        row = dict(row)
        # 計算停車時長
        if isinstance(row['entry_time'], str):
            entry = datetime.strptime(row['entry_time'], '%Y-%m-%d %H:%M:%S')
        else:
            entry = row['entry_time']
        duration = int((now - entry).total_seconds() / 60)
        row['duration'] = f'{duration} 分鐘'
        row['duration_minutes'] = duration
        result.append(row)
    
    return result

def get_parking_session_by_plate(plate):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM parking_sessions WHERE plate=? AND status='parking' ORDER BY entry_time DESC LIMIT 1",
        (plate,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

# ============ 費用計算 ============

def calculate_fee(duration_minutes, slot_number=None, car_type='all', owner_type='visitor'):
    """計算停車費用
    
    Args:
        duration_minutes: 停車分鐘數
        slot_number: 車位號碼
        car_type: 車型
        owner_type: 'resident' (月租戶) or 'visitor' (臨停)
    """
    conn = get_db()
    
    # 如果是月租戶，直接用月租費（這裡簡化處理）
    if owner_type == 'resident':
        # 月租戶：根據停車時長計算（一天為上限）
        rule = conn.execute(
            "SELECT * FROM billing_rules WHERE is_active=1 AND billing_type='monthly' LIMIT 1"
        ).fetchone()
        if rule:
            monthly_fee = rule['monthly_fee'] or 0
            # 簡化：不足一天按比例計算，最高為月租費
            daily_equivalent = monthly_fee / 30
            fee = min(int(daily_equivalent * (duration_minutes / 1440)), monthly_fee)
            conn.close()
            return fee
    
    # 臨停：用一般計時規則
    rule = conn.execute(
        'SELECT * FROM billing_rules WHERE is_active=1 AND billing_type="hourly" AND (car_type=? OR car_type="all") ORDER BY car_type DESC LIMIT 1',
        (car_type,)
    ).fetchone()
    
    if not rule:
        # 找不到規則，使用預設值
        rule = {'base_minutes': 15, 'base_fee': 0, 'hourly_fee': 30, 'daily_max': 500}
    
    conn.close()
    
    base_minutes = rule['base_minutes']
    base_fee = rule['base_fee']
    hourly_fee = rule['hourly_fee']
    daily_max = rule['daily_max']
    
    if duration_minutes <= base_minutes:
        return base_fee
    
    chargeable_minutes = duration_minutes - base_minutes
    hours = (chargeable_minutes + 59) // 60  # 向上取整到小時
    fee = base_fee + (hours * hourly_fee)
    
    # 檢查每日上限
    if daily_max and fee > daily_max:
        fee = daily_max
    
    return fee

# ============ 訪客通行證 ============

def get_visitor_passes(active_only=True):
    """取得訪客通行證列表"""
    conn = get_db()
    if active_only:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows = conn.execute(
            "SELECT * FROM visitor_passes WHERE status='active' AND valid_until >= ? ORDER BY valid_until ASC",
            (now,)
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM visitor_passes ORDER BY created_at DESC').fetchall()
    conn.close()
    return [dict(row) for row in rows]

def create_visitor_pass(plate, visitor_name, visitor_phone, valid_hours, note='', created_by=None):
    """建立訪客通行證"""
    conn = get_db()
    from datetime import timedelta
    valid_from = datetime.now()
    valid_until = valid_from + timedelta(hours=valid_hours)
    
    cursor = conn.execute(
        '''INSERT INTO visitor_passes (plate, visitor_name, visitor_phone, valid_from, valid_until, note, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (plate, visitor_name, visitor_phone, valid_from, valid_until, note, created_by)
    )
    conn.commit()
    conn.close()
    return cursor.lastrowid

def check_visitor_pass(plate):
    """檢查車牌是否有有效的訪客通行證"""
    conn = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    row = conn.execute(
        "SELECT * FROM visitor_passes WHERE plate=? AND status='active' AND valid_until >= ? ORDER BY valid_until DESC LIMIT 1",
        (plate, now)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def use_visitor_pass(pass_id):
    """使用通行證（標記為已使用）"""
    conn = get_db()
    conn.execute("UPDATE visitor_passes SET status='used' WHERE id=?", (pass_id,))
    conn.commit()
    conn.close()

def cancel_visitor_pass(pass_id):
    """取消通行證"""
    conn = get_db()
    conn.execute("UPDATE visitor_passes SET status='cancelled' WHERE id=?", (pass_id,))
    conn.commit()
    conn.close()

# ============ 帳單管理 ============

def create_billing(session_id, plate, owner_name, amount, duration_minutes, entry_time, exit_time, payment_method='cash', note=''):
    conn = get_db()
    conn.execute(
        '''INSERT INTO billing (session_id, plate, owner_name, amount, duration_minutes, entry_time, exit_time, payment_method, note) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (session_id, plate, owner_name, amount, duration_minutes, entry_time, exit_time, payment_method, note)
    )
    conn.commit()
    conn.close()

def get_unpaid_bills(days_threshold=7):
    """取得拖欠超過指定天數的帳單"""
    conn = get_db()
    from datetime import timedelta
    threshold_date = (datetime.now() - timedelta(days=days_threshold)).strftime('%Y-%m-%d')
    
    rows = conn.execute(
        '''SELECT b.*, o.name as owner_name, o.phone 
           FROM billing b
           LEFT JOIN owners o ON b.plate = o.plate
           WHERE b.payment_status = 'unpaid' 
           AND b.exit_time < ?
           ORDER BY b.exit_time ASC''',
        (threshold_date,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_billing_list(limit=100, offset=0, plate_filter=None, date_filter=None, status_filter=None):
    conn = get_db()
    query = 'SELECT * FROM billing WHERE 1=1'
    params = []
    if plate_filter:
        query += ' AND plate LIKE ?'
        params.append(f'%{plate_filter}%')
    if date_filter:
        query += ' AND DATE(created_at) = ?'
        params.append(date_filter)
    if status_filter:
        query += ' AND payment_status = ?'
        params.append(status_filter)
    query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_billing_count(plate_filter=None, date_filter=None, status_filter=None):
    conn = get_db()
    query = 'SELECT COUNT(*) FROM billing WHERE 1=1'
    params = []
    if plate_filter:
        query += ' AND plate LIKE ?'
        params.append(f'%{plate_filter}%')
    if date_filter:
        query += ' AND DATE(created_at) = ?'
        params.append(date_filter)
    if status_filter:
        query += ' AND payment_status = ?'
        params.append(status_filter)
    count = conn.execute(query, params).fetchone()[0]
    conn.close()
    return count

def mark_billing_paid(billing_id, payment_method='cash'):
    conn = get_db()
    conn.execute(
        'UPDATE billing SET payment_status=?, paid_at=?, payment_method=? WHERE id=?',
        ('paid', datetime.now(), payment_method, billing_id)
    )
    conn.commit()
    conn.close()

def get_billing_summary(start_date=None, end_date=None):
    """取得帳單統計"""
    conn = get_db()
    query = '''
        SELECT 
            COUNT(*) as total_count,
            SUM(CASE WHEN payment_status='paid' THEN 1 ELSE 0 END) as paid_count,
            SUM(CASE WHEN payment_status='unpaid' THEN 1 ELSE 0 END) as unpaid_count,
            SUM(CASE WHEN payment_status='paid' THEN amount ELSE 0 END) as total_paid,
            SUM(CASE WHEN payment_status='unpaid' THEN amount ELSE 0 END) as total_unpaid
        FROM billing WHERE 1=1
    '''
    params = []
    if start_date:
        query += ' AND DATE(created_at) >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND DATE(created_at) <= ?'
        params.append(end_date)
    
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None

# ============ 收費規則管理 ============

def get_billing_rules():
    conn = get_db()
    rows = conn.execute('SELECT * FROM billing_rules ORDER BY is_active DESC, id ASC').fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_active_billing_rule():
    conn = get_db()
    row = conn.execute('SELECT * FROM billing_rules WHERE is_active=1 LIMIT 1').fetchone()
    conn.close()
    return dict(row) if row else None

def add_billing_rule(name, car_type, base_minutes, base_fee, hourly_fee, daily_max=None):
    conn = get_db()
    conn.execute(
        '''INSERT INTO billing_rules (name, car_type, base_minutes, base_fee, hourly_fee, daily_max) 
           VALUES (?, ?, ?, ?, ?, ?)''',
        (name, car_type, base_minutes, base_fee, hourly_fee, daily_max)
    )
    conn.commit()
    conn.close()

def update_billing_rule(rule_id, name, car_type, base_minutes, base_fee, hourly_fee, daily_max, is_active):
    conn = get_db()
    conn.execute(
        '''UPDATE billing_rules SET name=?, car_type=?, base_minutes=?, base_fee=?, hourly_fee=?, daily_max=?, is_active=? WHERE id=?''',
        (name, car_type, base_minutes, base_fee, hourly_fee, daily_max, is_active, rule_id)
    )
    conn.commit()
    conn.close()

def delete_billing_rule(rule_id):
    conn = get_db()
    conn.execute('DELETE FROM billing_rules WHERE id = ?', (rule_id,))
    conn.commit()
    conn.close()
