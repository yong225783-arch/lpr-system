import re

with open('/home/bot/.openclaw/workspace/lpr-system/templates/owners.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Add "到期日" column header (between 類型 and 狀態)
old_th = "                <th>類型</th>\n                <th>狀態</th>"
new_th = "                <th>類型</th>\n                <th>到期日</th>\n                <th>狀態</th>"
html = html.replace(old_th, new_th, 1)

# 2. Add expiry date cell in each row (between owner_type badge and status badge)
# Find the pattern: end of owner_type cell -> start of status cell
old_cell = """                <td>
                    {% if o.owner_type == 'resident' %}
                    <span style="background:#007bff; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">👤 月租戶</span>"""
new_cell = """                <td>
                    {% if o.owner_type == 'resident' %}
                    <span style="background:#007bff; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">👤 月租戶</span>
                    {% elif o.owner_type == 'owner' %}
                    <span style="background:#28a745; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">🏠 住戶</span>
                    {% elif o.owner_type == 'tenant' %}
                    <span style="background:#17a2b8; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">🔑 承租戶</span>
                    {% elif o.owner_type == 'visitor' %}
                    <span style="background:#6c757d; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">🚶 訪客</span>
                    {% elif o.owner_type == 'service' %}
                    <span style="background:#fd7e14; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">🔧 服務</span>
                    {% elif o.owner_type == 'delivery' %}
                    <span style="background:#e83e8c; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">📦 快遞</span>
                    {% else %}
                    <span style="background:#ccc; color:#333; padding:2px 8px; border-radius:10px; font-size:12px;">{{ o.owner_type }}</span>
                    {% endif %}
                </td>
                <td>
                    {% if o.owner_type in ('resident','owner','tenant') and o.rental_expiry_date %}
                        {% set expiry = o.rental_expiry_date %}
                        {% set today = "2026-03-28" %} {# placeholder, computed in JS #}
                        <span class="expiry-badge" data-expiry="{{ expiry }}" style="cursor:pointer; padding:2px 8px; border-radius:10px; font-size:12px; background:#6c757d; color:white;"></span>
                    {% else %}
                    <span style="color:#aaa; font-size:12px;">-</span>
                    {% endif %}
                </td>
                <td>
                    {% if o.owner_type == 'resident' %}
                    <span style="background:#007bff; color:white; padding:2px 8px; border-radius:10px; font-size:12px;">👤 月租戶</span>"""

# Hmm this approach is complex due to Jinja2. Let me try a different approach.
# Just add the expiry cell right before the status cell, using a simpler marker.

# Actually, let me just add the expiry column data cell using a JavaScript approach
# by adding data-expiry attribute to the row and rendering in JS.

# Let me take a simpler approach: add data-expiry to each row tr and render in JS
# Add data-expiry to each row
old_tr = '<tr class="{% if o.is_blacklist %}row-danger{% endif %}" data-blacklist="{{ o.is_blacklist }}" data-owner-type="{{ o.owner_type }}">'
new_tr = '<tr class="{% if o.is_blacklist %}row-danger{% endif %}" data-blacklist="{{ o.is_blacklist }}" data-owner-type="{{ o.owner_type }}" data-expiry="{{ o.rental_expiry_date or "" }}" data-name="{{ o.name }}" data-plate="{{ o.plate }}">'
html = html.replace(old_tr, new_tr, 1)

# Now add the expiry date <td> after the owner_type </td>
# Find owner_type badge end and status badge start
# The owner_type badge is in a <td> and ends before <td> for status
# Let's add it after the closing </td> of owner_type but before status badge

# Add expiry badge <td> - insert before status <td>
old_before_status = """                </td>
                <td>
                    <span class="badge badge-{% if o.is_blacklist %}danger{% else %}success{% endif %}">
                        {% if o.is_blacklist %}🚫 黑名單{% else %}✅ 正常{% endif %}
                    </span>
                </td>
                <td>
                    <button class="btn-sm btn-edit\""""

new_before_status = """                </td>
                <td>
                    {% if o.owner_type in ('resident','owner','tenant') %}
                    <span class="expiry-cell" data-expiry="{{ o.rental_expiry_date or '' }}" style="cursor:pointer; padding:2px 8px; border-radius:10px; font-size:12px; background:#6c757d; color:white;"></span>
                    {% else %}
                    <span style="color:#bbb; font-size:12px;">-</span>
                    {% endif %}
                </td>
                <td>
                    <span class="badge badge-{% if o.is_blacklist %}danger{% else %}success{% endif %}">
                        {% if o.is_blacklist %}🚫 黑名单{% else %}✅ 正常{% endif %}
                    </span>
                </td>
                <td>
                    <button class="btn-sm btn-edit\""""

html = html.replace(old_before_status, new_before_status, 1)

# 3. Add expiry date input in edit form (after slot_number input)
old_form_slot = '<input type="text" name="slot_number" id="ownerSlotNumber" placeholder="如：A001">'
new_form_slot = '<input type="text" name="slot_number" id="ownerSlotNumber" placeholder="如：A001">\n                <label>月租到期日（YYYY-MM-DD）</label>\n                <input type="date" name="rental_expiry_date" id="ownerExpiryDate">'
html = html.replace(old_form_slot, new_form_slot, 1)

# 4. Update JS populate to fill expiry date
old_js = "    document.getElementById('ownerNote').value = o.note || '';"
new_js = "    document.getElementById('ownerNote').value = o.note || '';\n    document.getElementById('ownerExpiryDate').value = o.rental_expiry_date || '';"
html = html.replace(old_js, new_js, 1)

# 5. Add JavaScript to compute and render expiry dates (color + countdown)
# Add before </body>
old_body_end = "    // Fill blacklist/owner_type when editing\n    function fillOwnerType(o) {"
new_body_end = """    // Render expiry badge with color coding
    function renderExpiryBadges() {
        const today = new Date();
        document.querySelectorAll('.expiry-cell').forEach(function(cell) {
            const expiryStr = cell.getAttribute('data-expiry');
            if (!expiryStr) { cell.textContent = '-'; return; }
            const expiry = new Date(expiryStr);
            const diff = Math.ceil((expiry - today) / (1000*60*60*24));
            if (diff < 0) {
                cell.textContent = '已過期 ' + Math.abs(diff) + '天';
                cell.style.background = '#dc3545';
            } else if (diff === 0) {
                cell.textContent = '今日到期!';
                cell.style.background = '#fd7e14';
            } else if (diff <= 7) {
                cell.textContent = '剩 ' + diff + ' 天';
                cell.style.background = '#fd7e14';
            } else if (diff <= 30) {
                cell.textContent = '剩 ' + diff + ' 天';
                cell.style.background = '#ffc107';
                cell.style.color = '#333';
            } else {
                cell.textContent = expiryStr;
                cell.style.background = '#28a745';
            }
            // Click to edit
            cell.onclick = function() {
                const id = cell.closest('tr').querySelector('.btn-edit').getAttribute('onclick').match(/\\d+/)[0];
                editOwner({id: parseInt(id), name: cell.closest('tr').getAttribute('data-name'), plate: cell.closest('tr').getAttribute('data-plate'), rental_expiry_date: expiryStr});
            };
        });
    }
    document.addEventListener('DOMContentLoaded', renderExpiryBadges);
    // Also run after AJAX reload
    function refreshTable() {
        renderExpiryBadges();
    }

    // Fill blacklist/owner_type when editing
    function fillOwnerType(o) {"""

html = html.replace(old_body_end, new_body_end, 1)

with open('/home/bot/.openclaw/workspace/lpr-system/templates/owners.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("✅ owners.html updated")
