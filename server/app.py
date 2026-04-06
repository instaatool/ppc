#!/usr/bin/env python3
"""PPC Toolroom Planning — Flask + SQLite backend"""

import sqlite3
import os
from flask import Flask, jsonify, request, send_from_directory, g

app = Flask(__name__, static_folder=None)

_server_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir   = os.path.dirname(_server_dir)
DB_PATH     = os.path.join(_server_dir, 'ppc.db')

# Find client dir — check several possible locations
def _find_client_dir():
    candidates = [
        os.path.join(_root_dir, 'client'),   # client/index.html
        _root_dir,                             # index.html at repo root
        _server_dir,                           # index.html next to app.py
    ]
    for d in candidates:
        if os.path.isfile(os.path.join(d, 'index.html')):
            return d
    return _root_dir  # fallback

CLIENT_DIR = _find_client_dir()

# ── Database helpers ────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
    return db

@app.teardown_appcontext
def close_db(exc=None):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                oc_no     TEXT NOT NULL,
                customer  TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS parts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id         INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                parent_id        INTEGER REFERENCES parts(id) ON DELETE CASCADE,
                s_no             INTEGER,
                part_description TEXT NOT NULL,
                qty              INTEGER DEFAULT 1,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS process_tracking (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id      INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
                process_name TEXT NOT NULL,
                required     INTEGER DEFAULT 0,
                planned_date TEXT,
                start_date   TEXT,
                actual_date  TEXT,
                UNIQUE(part_id, process_name)
            );
        """)

        # Migration: add parent_id column if it doesn't exist yet
        cols = [r[1] for r in db.execute("PRAGMA table_info(parts)").fetchall()]
        if 'parent_id' not in cols:
            db.execute("ALTER TABLE parts ADD COLUMN parent_id INTEGER REFERENCES parts(id) ON DELETE CASCADE")
            db.commit()

        # Seed demo data if empty
        count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if count == 0:
            cur = db.execute("INSERT INTO orders (oc_no, customer) VALUES ('202', 'SPIROTECH')")
            oc202 = cur.lastrowid
            cur = db.execute("INSERT INTO orders (oc_no, customer) VALUES ('184', 'FRIGOGLASS')")
            oc184 = cur.lastrowid

            spirotech_parts = [
                ('B STATION EXTRUSION TOOL UP FORMING', 1),
                ('BIG NUT', 1), ('SMALL NUT', 1),
                ('M5X25 ALLEN BOLT', 7), ('SHANK', 1),
                ('GUIDE BODY PACKING', 1), ('SPRING', 1),
                ('EJECTOR', 1), ('GUIDE BODY', 1),
                ('DIE BASE', 1), ('DIE STRIPPER', 1),
                ('SPRING', 6), ('SLEEVE', 3),
                ('M5X20 ALLEN BOLT', 3), ('WASHER', 3),
                ('SHEET FOR TRIAL', 1), ('DRILL SLEEVE', 1),
            ]
            for i, (desc, qty) in enumerate(spirotech_parts, 1):
                cur = db.execute(
                    "INSERT INTO parts (order_id, s_no, part_description, qty) VALUES (?,?,?,?)",
                    (oc202, i, desc, qty))
                pid = cur.lastrowid
                db.execute(
                    "INSERT INTO process_tracking (part_id, process_name, required, planned_date) VALUES (?,?,?,?)",
                    (pid, 'DESIGN', 1, '2026-04-02'))
                db.execute(
                    "INSERT INTO process_tracking (part_id, process_name, required, planned_date) VALUES (?,?,?,?)",
                    (pid, 'FINAL INSPECTION', 1, '2026-04-11'))

            frigoglass_parts = [
                ('B STATION PUNCH SHAPE', 28),
                ('B STATION PIN', 28),
                ('B STATION PUNCH ROUND', 9),
            ]
            for i, (desc, qty) in enumerate(frigoglass_parts, len(spirotech_parts) + 1):
                cur = db.execute(
                    "INSERT INTO parts (order_id, s_no, part_description, qty) VALUES (?,?,?,?)",
                    (oc184, i, desc, qty))
                pid = cur.lastrowid
                db.execute(
                    "INSERT INTO process_tracking (part_id, process_name, required, planned_date) VALUES (?,?,?,?)",
                    (pid, 'INSPECTION B. HT', 1, '2026-04-02'))
                db.execute(
                    "INSERT INTO process_tracking (part_id, process_name, required, planned_date) VALUES (?,?,?,?)",
                    (pid, 'FINAL INSPECTION', 1, '2026-04-03'))
            db.commit()


# ── Orders API ───────────────────────────────────────────────────────────────

def build_part_tree(parts_flat):
    """Convert a flat list of part dicts (with parent_id) into a nested tree."""
    by_id = {p['id']: {**p, 'children': []} for p in parts_flat}
    roots = []
    for p in parts_flat:
        if p['parent_id'] is None:
            roots.append(by_id[p['id']])
        else:
            parent = by_id.get(p['parent_id'])
            if parent:
                parent['children'].append(by_id[p['id']])
    return roots

@app.route('/api/orders', methods=['GET'])
def get_orders():
    db = get_db()
    orders = db.execute("SELECT * FROM orders ORDER BY id").fetchall()
    result = []
    for o in orders:
        parts_rows = db.execute(
            "SELECT * FROM parts WHERE order_id=? ORDER BY parent_id NULLS FIRST, s_no, id",
            (o['id'],)
        ).fetchall()
        parts_flat = []
        for p in parts_rows:
            procs = db.execute(
                "SELECT * FROM process_tracking WHERE part_id=?", (p['id'],)
            ).fetchall()
            parts_flat.append({**dict(p), 'processes': [dict(pr) for pr in procs]})
        result.append({**dict(o), 'parts': build_part_tree(parts_flat)})
    return jsonify(result)

@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.json
    if not data.get('oc_no') or not data.get('customer'):
        return jsonify({'error': 'oc_no and customer required'}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO orders (oc_no, customer) VALUES (?,?)",
        (data['oc_no'], data['customer']))
    db.commit()
    return jsonify({'id': cur.lastrowid, **data}), 201

@app.route('/api/orders/<int:oid>', methods=['PUT'])
def update_order(oid):
    data = request.json
    db = get_db()
    db.execute("UPDATE orders SET oc_no=?, customer=? WHERE id=?",
               (data['oc_no'], data['customer'], oid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/orders/<int:oid>', methods=['DELETE'])
def delete_order(oid):
    db = get_db()
    db.execute("DELETE FROM orders WHERE id=?", (oid,))
    db.commit()
    return jsonify({'success': True})


# ── Parts API ─────────────────────────────────────────────────────────────────

@app.route('/api/parts', methods=['POST'])
def create_part():
    data = request.json
    if not data.get('order_id') or not data.get('part_description'):
        return jsonify({'error': 'order_id and part_description required'}), 400
    db = get_db()
    max_sno = db.execute("SELECT MAX(s_no) FROM parts").fetchone()[0] or 0
    parent_id = data.get('parent_id') or None
    cur = db.execute(
        "INSERT INTO parts (order_id, parent_id, s_no, part_description, qty) VALUES (?,?,?,?,?)",
        (data['order_id'], parent_id, max_sno + 1, data['part_description'], data.get('qty', 1)))
    db.commit()
    return jsonify({
        'id': cur.lastrowid, 'order_id': data['order_id'], 'parent_id': parent_id,
        's_no': max_sno + 1, 'part_description': data['part_description'],
        'qty': data.get('qty', 1), 'processes': [], 'children': []
    }), 201

@app.route('/api/parts/<int:pid>', methods=['PUT'])
def update_part(pid):
    data = request.json
    db = get_db()
    db.execute("UPDATE parts SET part_description=?, qty=? WHERE id=?",
               (data['part_description'], data['qty'], pid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/parts/<int:pid>', methods=['DELETE'])
def delete_part(pid):
    db = get_db()
    db.execute("DELETE FROM parts WHERE id=?", (pid,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/parts/<int:pid>/process', methods=['PUT'])
def upsert_process(pid):
    data = request.json
    if not data.get('process_name'):
        return jsonify({'error': 'process_name required'}), 400
    db = get_db()
    db.execute("""
        INSERT INTO process_tracking (part_id, process_name, required, planned_date, start_date, actual_date)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(part_id, process_name) DO UPDATE SET
            required=excluded.required,
            planned_date=excluded.planned_date,
            start_date=excluded.start_date,
            actual_date=excluded.actual_date
    """, (pid,
          data['process_name'],
          1 if data.get('required') else 0,
          data.get('planned_date') or None,
          data.get('start_date') or None,
          data.get('actual_date') or None))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/parts/dashboard/upcoming', methods=['GET'])
def dashboard_upcoming():
    db = get_db()
    rows = db.execute("""
        SELECT p.id as part_id, p.part_description, p.qty,
               o.oc_no, o.customer,
               pt.process_name, pt.planned_date, pt.actual_date, pt.required
        FROM process_tracking pt
        JOIN parts p ON pt.part_id = p.id
        JOIN orders o ON p.order_id = o.id
        WHERE pt.planned_date IS NOT NULL
          AND pt.actual_date IS NULL
          AND pt.required = 1
        ORDER BY pt.planned_date ASC
    """).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Init DB on startup (works with both gunicorn and direct python) ──────────
init_db()

# ── Serve frontend ───────────────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    file_path = os.path.join(CLIENT_DIR, path) if path else os.path.join(CLIENT_DIR, 'index.html')
    if path and os.path.isfile(file_path):
        return send_from_directory(CLIENT_DIR, path)
    return send_from_directory(CLIENT_DIR, 'index.html')


if __name__ == '__main__':
    init_db()
    import socket
    hostname = socket.gethostname()
    try:
        ip = socket.gethostbyname(hostname)
    except Exception:
        ip = 'localhost'
    print(f"\n🏭  PPC Toolroom running at http://localhost:3001")
    print(f"    Team access: http://{ip}:3001\n")
    app.run(host='0.0.0.0', port=3001, debug=False)
