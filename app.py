from flask import Flask, request, jsonify, send_file
import json
import os

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    import pg8000.native

    def parse_db_url(url):
        # Remove query string (?sslmode=require etc)
        if '?' in url:
            url = url[:url.index('?')]
        # Remove scheme (postgresql:// or postgres://)
        url = url.split('://', 1)[1]
        # Split userinfo@hostinfo
        userinfo, hostinfo = url.rsplit('@', 1)
        # Parse user:password
        if ':' in userinfo:
            user, password = userinfo.split(':', 1)
        else:
            user, password = userinfo, ''
        # Parse host:port/database
        if '/' in hostinfo:
            hostport, database = hostinfo.split('/', 1)
        else:
            hostport, database = hostinfo, 'postgres'
        if ':' in hostport:
            host, port_str = hostport.split(':', 1)
            port = int(port_str)
        else:
            host, port = hostport, 5432
        return host, port, database, user, password

    def get_db():
        host, port, database, user, password = parse_db_url(DATABASE_URL)
        return pg8000.native.Connection(
            host=host, port=port,
            database=database, user=user,
            password=password, ssl_context=True
        )

    def init_db():
        conn = get_db()
        conn.run('CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.close()

    def db_get(key, default=None):
        conn = get_db()
        rows = conn.run('SELECT value FROM store WHERE key = :key', key=key)
        conn.close()
        if rows:
            try: return json.loads(rows[0][0])
            except: return rows[0][0]
        return default

    def db_set(key, value):
        conn = get_db()
        conn.run('INSERT INTO store (key, value) VALUES (:key, :value) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                 key=key, value=json.dumps(value, ensure_ascii=False))
        conn.close()

else:
    import sqlite3
    DB = 'clientes.db'

    def get_db():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        conn = get_db()
        conn.execute('CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.commit(); conn.close()

    def db_get(key, default=None):
        conn = get_db()
        row = conn.execute('SELECT value FROM store WHERE key = ?', (key,)).fetchone()
        conn.close()
        if row:
            try: return json.loads(row['value'])
            except: return row['value']
        return default

    def db_set(key, value):
        conn = get_db()
        conn.execute('INSERT OR REPLACE INTO store (key, value) VALUES (?, ?)',
                     (key, json.dumps(value, ensure_ascii=False)))
        conn.commit(); conn.close()


def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/')
def index():
    return send_file('index.html')

@app.route('/api/efectivo')
def efectivo():
    val = db_get('efectivo_actual', 0)
    r = jsonify({'efectivo': val})
    return _cors(r)

@app.route('/api/load')
def load():
    data = db_get('ap_all_v2')
    passwd = db_get('ap_pass')
    return jsonify({'data': data, 'pass': passwd})

@app.route('/api/save', methods=['POST'])
def save():
    body = request.json
    if not body: return jsonify({'ok': False}), 400
    if 'data' in body:
        db_set('ap_all_v2', body['data'])
    if 'pass' in body:
        db_set('ap_pass', body['pass'])
    if 'efectivo_actual' in body:
        db_set('efectivo_actual', body['efectivo_actual'])
    return jsonify({'ok': True})


try:
    init_db()
except Exception as e:
    print(f'DB init warning: {e}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
