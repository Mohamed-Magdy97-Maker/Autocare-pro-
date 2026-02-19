from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sqlite3
import json
import os
from datetime import datetime, timedelta
import random
import math

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

DATABASE = 'autocare.db'
UPLOAD_FOLDER = '/tmp/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'odometer'), exist_ok=True)

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/api/makes')
def get_makes():
    conn = get_db()
    makes = conn.execute('SELECT * FROM vehicle_makes ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(make) for make in makes])

@app.route('/api/models')
def get_models():
    make = request.args.get('make')
    year = request.args.get('year', type=int)
    conn = get_db()
    sql = '''SELECT vm.*, vmk.name as make_name FROM vehicle_models vm 
             JOIN vehicle_makes vmk ON vm.make_id = vmk.id WHERE 1=1'''
    params = []
    if make:
        sql += ' AND vmk.name = ?'
        params.append(make)
    if year:
        sql += ' AND vm.year_start <= ? AND vm.year_end >= ?'
        params.extend([year, year])
    sql += ' ORDER BY vmk.name, vm.name'
    models = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(model) for model in models])

@app.route('/api/years')
def get_years():
    make = request.args.get('make')
    model = request.args.get('model')
    conn = get_db()
    if make and model:
        result = conn.execute('''SELECT year_start, year_end FROM vehicle_models vm
            JOIN vehicle_makes vmk ON vm.make_id = vmk.id
            WHERE vmk.name = ? AND vm.name = ?''', (make, model)).fetchone()
        conn.close()
        if result:
            return jsonify(list(range(result['year_start'], result['year_end'] + 1)))
    conn.close()
    return jsonify(list(range(1970, 2027)))

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    conn = get_db()
    try:
        cur = conn.execute('''INSERT INTO users (email, name, phone, latitude, longitude)
            VALUES (?, ?, ?, ?, ?)''', (data.get('email'), data.get('name'), 
            data.get('phone'), data.get('latitude'), data.get('longitude')))
        conn.commit()
        uid = cur.lastrowid
        conn.close()
        return jsonify({"id": uid, "message": "User created"}), 201
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Email exists"}), 400

@app.route('/api/users/<int:user_id>')
def get_user(user_id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return jsonify(dict(user)) if user else (jsonify({"error": "Not found"}), 404)

@app.route('/api/vehicles', methods=['POST'])
def add_vehicle():
    data = request.json
    conn = get_db()
    cur = conn.execute('''INSERT INTO user_vehicles 
        (user_id, make, model, year, vin, current_km, engine_type, transmission)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (data.get('user_id'), data.get('make'), data.get('model'), data.get('year'),
         data.get('vin'), data.get('current_km', 0), data.get('engine_type'), data.get('transmission')))
    vid = cur.lastrowid
    conn.commit()
    schedule = generate_maintenance_schedule(conn, vid, data.get('make'), 
                                              data.get('model'), data.get('year'), data.get('current_km', 0))
    conn.close()
    return jsonify({"id": vid, "maintenance_schedule": schedule}), 201

def generate_maintenance_schedule(conn, vehicle_id, make, model, year, current_km):
    schedules = conn.execute('''SELECT * FROM maintenance_schedules 
        WHERE (make IS NULL OR make = ?) AND (model IS NULL OR model = ?)
        AND year_start <= ? AND year_end >= ? ORDER BY is_critical DESC''',
        (make, model, year, year)).fetchall()
    upcoming = []
    for s in schedules:
        upcoming.append({
            "service_type": s['service_type'],
            "due_km": current_km + s['interval_km'],
            "due_date": (datetime.now() + timedelta(days=30*s['interval_months'])).isoformat(),
            "interval_km": s['interval_km'],
            "description": s['description'],
            "estimated_cost": {"min": s['estimated_cost_min'], "max": s['estimated_cost_max']},
            "difficulty": s['difficulty']
        })
    return upcoming

def get_upcoming_maintenance(conn, vehicle_id):
    vehicle = conn.execute('SELECT * FROM user_vehicles WHERE id = ?', (vehicle_id,)).fetchone()
    if not vehicle:
        return []
    current_km = vehicle['current_km'] or 0
    make, model, year = vehicle['make'], vehicle['model'], vehicle['year']
    last_services = {}
    logs = conn.execute('''SELECT service_type, MAX(date_performed) as last_date, MAX(km_reading) as last_km
        FROM maintenance_logs WHERE vehicle_id = ? GROUP BY service_type''', (vehicle_id,)).fetchall()
    for log in logs:
        last_services[log['service_type']] = {'date': log['last_date'], 'km': log['last_km']}
    schedules = conn.execute('''SELECT * FROM maintenance_schedules 
        WHERE (make IS NULL OR make = ?) AND (model IS NULL OR model = ?)
        AND year_start <= ? AND year_end >= ?''', (make, model, year, year)).fetchall()
    upcoming = []
    for s in schedules:
        last = last_services.get(s['service_type'], {})
        km_remaining = s['interval_km'] - (current_km - last.get('km', 0))
        months_elapsed = 0
        if last.get('date'):
            last_dt = datetime.strptime(last['date'], '%Y-%m-%d')
            months_elapsed = (datetime.now() - last_dt).days / 30
        months_remaining = s['interval_months'] - months_elapsed
        urgency = "overdue" if km_remaining <= 0 or months_remaining <= 0 else \
                  "critical" if km_remaining < 1000 or months_remaining < 1 else \
                  "soon" if km_remaining < 3000 or months_remaining < 3 else "upcoming"
        upcoming.append({
            "service_type": s['service_type'],
            "km_remaining": int(km_remaining),
            "months_remaining": round(months_remaining, 1),
            "urgency": urgency,
            "description": s['description'],
            "estimated_cost": {"min": s['estimated_cost_min'], "max": s['estimated_cost_max']},
            "difficulty": s['difficulty']
        })
    upcoming.sort(key=lambda x: {"overdue":0, "critical":1, "soon":2, "upcoming":3}.get(x['urgency'], 4))
    return upcoming[:10]

@app.route('/api/vehicles/user/<int:user_id>')
def get_user_vehicles(user_id):
    conn = get_db()
    vehicles = conn.execute('SELECT * FROM user_vehicles WHERE user_id = ?', (user_id,)).fetchall()
    result = []
    for v in vehicles:
        vd = dict(v)
        vd['upcoming_maintenance'] = get_upcoming_maintenance(conn, v['id'])
        result.append(vd)
    conn.close()
    return jsonify(result)

@app.route('/api/maintenance/log', methods=['POST'])
def log_maintenance():
    data = request.json
    conn = get_db()
    conn.execute('''INSERT INTO maintenance_logs 
        (vehicle_id, service_type, km_reading, date_performed, cost, workshop_name, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (data.get('vehicle_id'), data.get('service_type'), data.get('km_reading'),
         data.get('date_performed'), data.get('cost'), data.get('workshop_name'), data.get('notes')))
    conn.execute('UPDATE user_vehicles SET current_km = MAX(current_km, ?) WHERE id = ?',
                  (data.get('km_reading'), data.get('vehicle_id')))
    conn.commit()
    conn.close()
    return jsonify({"message": "Logged"})

@app.route('/api/odometer/submit', methods=['POST'])
def submit_odometer():
    vehicle_id = request.form.get('vehicle_id')
    km_reading = int(request.form.get('km_reading'))
    photo = request.files.get('photo')
    photo_path = None
    if photo:
        filename = f"{vehicle_id}_{datetime.now().timestamp()}.jpg"
        photo_path = os.path.join(UPLOAD_FOLDER, 'odometer', filename)
        photo.save(photo_path)
    conn = get_db()
    conn.execute('INSERT INTO odometer_readings (vehicle_id, km_reading, photo_path) VALUES (?,?,?)',
                  (vehicle_id, km_reading, photo_path))
    conn.execute('UPDATE user_vehicles SET current_km = ?, last_km_update = CURRENT_TIMESTAMP WHERE id = ?',
                  (km_reading, vehicle_id))
    conn.commit()
    upcoming = get_upcoming_maintenance(conn, vehicle_id)
    conn.close()
    return jsonify({"km_reading": km_reading, "upcoming_maintenance": upcoming})

@app.route('/api/diagnostics/analyze', methods=['POST'])
def analyze_diagnostic():
    data = request.json
    vehicle_id = data.get('vehicle_id')
    symptoms = data.get('symptoms', [])
    description = data.get('description', '')
    conn = get_db()
    vehicle = conn.execute('SELECT * FROM user_vehicles WHERE id = ?', (vehicle_id,)).fetchone()
    if not vehicle:
        conn.close()
        return jsonify({"error": "Vehicle not found"}), 404
    result = perform_ai_analysis(dict(vehicle), symptoms, description)
    conn.execute('''INSERT INTO diagnostic_reports 
        (vehicle_id, input_type, symptoms, diagnosis, confidence, severity, recommended_actions)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (vehicle_id, 'text', json.dumps(symptoms), json.dumps(result['diagnosis']),
         result['confidence'], result['severity'], json.dumps(result['recommended_actions'])))
    conn.commit()
    conn.close()
    return jsonify(result)

def perform_ai_analysis(vehicle, symptoms, description):
    full_text = ' '.join(symptoms + [description]).lower()
    issues = []
    kb = {
        "engine": {
            "overheating": ["Coolant leak", "Thermostat", "Water pump", "Head gasket"],
            "knocking": ["Rod bearing", "Detonation", "Carbon buildup"],
            "rough_idle": ["Vacuum leak", "Ignition coils", "Fuel injectors"],
            "loss_of_power": ["Clogged filter", "Fuel pump", "Catalytic converter"]
        },
        "brakes": {
            "squealing": ["Worn pads", "Glazed pads"],
            "grinding": ["Metal on metal", "Rotors"],
            "spongy": ["Air in lines", "Master cylinder"]
        },
        "transmission": {
            "slipping": ["Low fluid", "Worn clutches"],
            "hard_shifting": ["Solenoids", "Fluid condition"]
        }
    }
    for system, data in kb.items():
        for symptom, causes in data.items():
            if symptom in full_text:
                for cause in causes:
                    issues.append({"system": system, "symptom": symptom, "cause": cause, "confidence": random.uniform(0.7, 0.95)})
    if not issues:
        issues = [{"system": "general", "symptom": "unspecified", "cause": "Needs inspection", "confidence": 0.5}]
    issues.sort(key=lambda x: x['confidence'], reverse=True)
    primary = issues[0]
    severity = "high" if primary['system'] in ['engine', 'brakes'] and any(w in full_text for w in ['overheating', 'knocking', 'grinding']) else "medium" if primary['system'] in ['engine', 'brakes', 'transmission'] else "low"
    return {
        "vehicle_info": {"make": vehicle['make'], "model": vehicle['model'], "year": vehicle['year']},
        "diagnosis": {"primary_issue": primary, "all_issues": issues[:5]},
        "confidence": round(sum(i['confidence'] for i in issues)/len(issues), 2),
        "severity": severity,
        "recommended_actions": generate_repair_guide(primary)
    }

def generate_repair_guide(issue):
    guides = {
        "Worn pads": {
            "steps": ["Remove wheel", "Inspect caliper", "Replace pads", "Bed in brakes"],
            "difficulty": "easy", "time": "1-2 hours", "cost": {"min": 50, "max": 200}
        },
        "Coolant leak": {
            "steps": ["Pressure test", "Inspect hoses", "Check radiator", "Repair leak"],
            "difficulty": "medium", "time": "2-4 hours", "cost": {"min": 100, "max": 500}
        }
    }
    return guides.get(issue['cause'], {
        "steps": ["Inspect component", "Consult manual", "Consider professional service"],
        "difficulty": "unknown", "time": "Unknown", "cost": {"min": 100, "max": 1000}
    })

@app.route('/api/nearby/workshops')
def get_nearby_workshops():
    lat = float(request.args.get('lat'))
    lng = float(request.args.get('lng'))
    radius = float(request.args.get('radius', 10))
    conn = get_db()
    workshops = conn.execute('SELECT * FROM workshops').fetchall()
    conn.close()
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        return R * 2 * math.asin(math.sqrt(a))
    results = []
    for shop in workshops:
        d = haversine(lat, lng, shop['latitude'], shop['longitude'])
        if d <= radius:
            sd = dict(shop)
            sd['distance_km'] = round(d, 2)
            sd['specialties'] = json.loads(shop['specialties'])
            results.append(sd)
    results.sort(key=lambda x: x['distance_km'])
    return jsonify(results)

@app.route('/api/nearby/parts')
def get_nearby_parts():
    lat = float(request.args.get('lat'))
    lng = float(request.args.get('lng'))
    vehicle_id = request.args.get('vehicle_id')
    query = request.args.get('q', '')
    conn = get_db()
    vehicle = conn.execute('SELECT make, model, year FROM user_vehicles WHERE id = ?', (vehicle_id,)).fetchone() if vehicle_id else None
    sql = 'SELECT * FROM spare_parts WHERE 1=1'
    params = []
    if vehicle:
        sql += ' AND (compatible_makes LIKE ? OR compatible_makes LIKE ?)'
        params.extend([f'%{vehicle["make"]}%', '%"All"%'])
    if query:
        sql += ' AND name LIKE ?'
        params.append(f'%{query}%')
    parts = conn.execute(sql, params).fetchall()
    conn.close()
    stores = [
        {"name": "AutoZone", "lat": lat + 0.01, "lng": lng + 0.01, "dist": 1.2, "stock": True},
        {"name": "O'Reilly", "lat": lat - 0.005, "lng": lng + 0.008, "dist": 0.8, "stock": True},
        {"name": "Advance Auto", "lat": lat + 0.008, "lng": lng - 0.003, "dist": 1.5, "stock": random.choice([True, False])},
        {"name": "NAPA", "lat": lat - 0.012, "lng": lng - 0.007, "dist": 2.1, "stock": True}
    ]
    results = []
    for part in parts:
        pd = dict(part)
        pd['compatible_makes'] = json.loads(part['compatible_makes'])
        pd['nearby_stores'] = [{**s, "price": part['average_price'] * random.uniform(0.9, 1.3)} for s in stores if s['stock']]
        results.append(pd)
    return jsonify(results)

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
