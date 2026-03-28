with open('/home/bot/.openclaw/workspace/lpr-system/main.py', 'r') as f:
    content = f.read()

insert_after = """def api_owners_remove_blacklist(owner_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401"""

new_apis = """def api_owners_remove_blacklist(owner_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

# --- Monthly Rental Expiry API ---
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
        from datetime import datetime, timedelta
        for owner in expiring:
            plate = owner.get('plate', '')
            expiry = owner.get('rental_expiry_date', '')
            days_left = (datetime.strptime(expiry, '%Y-%m-%d').date() - datetime.now().date()).days
            if days_left < 0:
                msg = f'[EXPIRED] {owner["name"]} ({plate}) monthly rental expired'
            elif days_left == 0:
                msg = f'[EXPIRES TODAY] {owner["name"]} ({plate}) monthly rental expires today'
            else:
                msg = f'[REMINDER] {owner["name"]} ({plate}) monthly rental expires in {days_left} days ({expiry})'
            already = any(a.get('message','') == msg and (datetime.now() - a.get('time', datetime.min)).days == 0
                         for a in alerts)
            if not already:
                add_alert('warning', msg)
    except Exception as e:
        logger.error(f'Rental expiry check failed: {e}')"""

content = content.replace(insert_after, new_apis, 1)

old_startup = "# ============ Startup ============"
new_startup = """# ============ Startup: check rental expiry ============
try:
    check_rental_expiry_alerts()
except:
    pass

# ============ Startup ============"""

content = content.replace(old_startup, new_startup, 1)

with open('/home/bot/.openclaw/workspace/lpr-system/main.py', 'w') as f:
    f.write(content)
print("Done")
