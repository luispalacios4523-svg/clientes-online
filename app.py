from flask import Flask, request, jsonify, send_file
import json
import os
import time
import hmac
import base64
import hashlib
import secrets

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


# ─────────────────────────────────────────────────────────────
#  AUTENTICACION
#  Las contrasenas se guardan cifradas (PBKDF2), nunca en texto
#  plano, y /api/load y /api/save exigen un token valido.
# ─────────────────────────────────────────────────────────────

def _secreto():
    s = db_get('auth_secret')
    if not s:
        s = secrets.token_hex(32)
        db_set('auth_secret', s)
    return s.encode()


def _cifrar(pw, sal=None):
    if sal is None:
        sal = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), sal.encode(), 120000).hex()
    return {'sal': sal, 'hash': h}


def _coincide(pw, reg):
    if not reg or 'sal' not in reg or 'hash' not in reg:
        return False
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), reg['sal'].encode(), 120000).hex()
    return hmac.compare_digest(h, reg['hash'])


def _usuarios():
    u = db_get('ap_users')
    if not u:
        # Migracion: toma la contrasena que hubiera en texto plano y la cifra
        viejo = db_get('ap_pass') or {}
        u = {
            'admin': _cifrar(viejo.get('admin') or 'admin123'),
            'cobrador': _cifrar(viejo.get('cobrador') or 'cobrador123'),
        }
        db_set('ap_users', u)
        db_set('ap_pass', None)   # se borra el texto plano
    return u


def _token(rol, dias=30):
    exp = int(time.time()) + dias * 86400
    msg = '%s:%d' % (rol, exp)
    firma = hmac.new(_secreto(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(('%s:%s' % (msg, firma)).encode()).decode()


def _verificar(token):
    try:
        crudo = base64.urlsafe_b64decode(token.encode()).decode()
        rol, exp, firma = crudo.split(':')
        msg = '%s:%s' % (rol, exp)
        esperada = hmac.new(_secreto(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(firma, esperada):
            return None
        if int(exp) < time.time():
            return None
        return rol
    except Exception:
        return None


def _sesion():
    return _verificar(request.headers.get('X-Auth', ''))


@app.route('/api/login', methods=['POST'])
def login():
    body = request.json or {}
    rol = body.get('rol')
    pw = body.get('pass') or ''
    if rol not in ('admin', 'cobrador'):
        return jsonify({'ok': False}), 400
    time.sleep(0.4)   # frena los intentos por fuerza bruta
    if not _coincide(pw, _usuarios().get(rol)):
        return jsonify({'ok': False, 'motivo': 'credenciales'}), 401
    return jsonify({'ok': True, 'token': _token(rol), 'rol': rol})


@app.route('/api/password', methods=['POST'])
def cambiar_password():
    rol_sesion = _sesion()
    if rol_sesion != 'admin':
        return jsonify({'ok': False, 'motivo': 'no_autorizado'}), 403
    body = request.json or {}
    objetivo = body.get('rol')
    nueva = body.get('nueva') or ''
    if objetivo not in ('admin', 'cobrador') or len(nueva) < 4:
        return jsonify({'ok': False, 'motivo': 'datos'}), 400
    u = _usuarios()
    # Para cambiar la del admin hay que saber la actual
    if objetivo == 'admin' and not _coincide(body.get('actual') or '', u.get('admin')):
        return jsonify({'ok': False, 'motivo': 'actual_incorrecta'}), 401
    u[objetivo] = _cifrar(nueva)
    db_set('ap_users', u)
    return jsonify({'ok': True})

@app.route('/')
def index():
    from flask import make_response
    resp = make_response(send_file('index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/api/efectivo')
def efectivo():
    val = db_get('efectivo_actual', 0)
    r = jsonify({'efectivo': val})
    return _cors(r)

@app.route('/api/load')
def load():
    if not _sesion():
        return jsonify({'ok': False, 'motivo': 'no_autorizado'}), 401
    return jsonify({'data': db_get('ap_all_v2')})

def _cuenta(d):
    """Cuantos registros trae un paquete de datos."""
    if not isinstance(d, dict):
        return {'P': 0, 'PG': 0, 'G': 0}
    return {k: len(d.get(k) or []) for k in ('P', 'PG', 'G')}


def _snapshot(data):
    """Guarda una copia con fecha y mantiene las ultimas 30."""
    from datetime import datetime, timezone
    sello = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    db_set('snap_' + sello, data)
    idx = db_get('snap_index', []) or []
    idx.append(sello)
    for viejo in idx[:-30]:
        try:
            db_set('snap_' + viejo, None)
        except Exception:
            pass
    db_set('snap_index', idx[-30:])


@app.route('/api/save', methods=['POST'])
def save():
    if not _sesion():
        return jsonify({'ok': False, 'motivo': 'no_autorizado'}), 401
    body = request.json
    if not body:
        return jsonify({'ok': False}), 400

    if 'data' in body:
        nuevo = body['data']
        actual = db_get('ap_all_v2')
        n, a = _cuenta(nuevo), _cuenta(actual)

        # Proteccion contra borrado masivo: si el navegador manda menos
        # registros de los que ya hay guardados, se rechaza. Antes esto
        # borraba meses de pagos cuando la carga inicial fallaba.
        if actual and not body.get('forzar'):
            for k in ('P', 'PG', 'G'):
                if a[k] > 0 and n[k] < a[k]:
                    return jsonify({
                        'ok': False,
                        'motivo': 'menos_registros',
                        'detalle': 'Recibidos %s de %s en %s' % (n[k], a[k], k),
                        'actual': a, 'recibido': n
                    }), 409

        _snapshot(nuevo)
        db_set('ap_all_v2', nuevo)

    # 'pass' ya no se acepta aqui: las contrasenas van por /api/password
    if 'efectivo_actual' in body:
        db_set('efectivo_actual', body['efectivo_actual'])
    return jsonify({'ok': True})


@app.route('/api/snapshots')
def snapshots():
    """Lista las copias automaticas guardadas en el servidor."""
    if not _sesion():
        return jsonify({'ok': False, 'motivo': 'no_autorizado'}), 401
    idx = db_get('snap_index', []) or []
    salida = []
    for sello in reversed(idx):
        d = db_get('snap_' + sello)
        if d:
            salida.append({'sello': sello, 'conteo': _cuenta(d)})
    r = jsonify({'snapshots': salida})
    return _cors(r)


@app.route('/api/snapshot/<sello>')
def snapshot_uno(sello):
    if not _sesion():
        return jsonify({'ok': False, 'motivo': 'no_autorizado'}), 401
    d = db_get('snap_' + sello)
    if not d:
        return jsonify({'ok': False, 'motivo': 'no_existe'}), 404
    return jsonify({'data': d, 'conteo': _cuenta(d)})


try:
    init_db()
except Exception as e:
    print(f'DB init warning: {e}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
