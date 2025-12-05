from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import csv
import io
import platform
import time
from datetime import datetime

# --- CONFIGURACIÓN HÍBRIDA ---
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'clave_maestra_iron_trace_final')
DB_NAME = "irontrace.db"
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if DATABASE_URL:
        if not psycopg2: raise ImportError("Falta psycopg2")
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn, 'POSTGRES'
    else:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn, 'SQLITE'

def ejecutar_sql(sql, params=(), one=False):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    try:
        if db_type == 'SQLITE': sql = sql.replace('%s', '?')
        cursor.execute(sql, params)
        if sql.strip().upper().startswith('SELECT'):
            rv = cursor.fetchone() if one else cursor.fetchall()
            if db_type == 'POSTGRES' and rv:
                rv = dict(rv) if one else [dict(row) for row in rv]
            return rv
        else:
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# --- RUTAS DE ACCESO ---
@app.route('/')
def root(): return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        try:
            u = ejecutar_sql('SELECT * FROM usuarios WHERE username=%s', (user,), one=True)
            valid = False
            if u:
                if u['password'].startswith(('scrypt:', 'pbkdf2:')): valid = check_password_hash(u['password'], pwd)
                elif u['password'] == pwd: valid = True
            
            if valid:
                session['user'] = u['username']; session['rol'] = u['rol']
                return redirect(url_for('panel_operador') if u['rol'] == 'operador' else url_for('dashboard'))
            else: flash('⛔ Acceso Denegado')
        except: flash('Error de conexión DB')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

# --- DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    if session.get('rol') == 'operador': return redirect(url_for('panel_operador'))
    if 'user' not in session: return redirect(url_for('login'))
    
    # Inicializar variables
    stats = {'insumos_hoy': 0, 'prestamos_valor': 0, 'prestamos_activos_qty': 0}
    en_uso = []
    total_items = 0  # Variable nueva
    
    try:
        # KPIs Financieros
        hoy = datetime.now().strftime("%Y-%m-%d")
        res = ejecutar_sql("SELECT SUM(p.cantidad * prod.precio) as total FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.tipo_item = 'INSUMO' AND p.fecha_salida LIKE %s", (f'{hoy}%',), one=True)
        stats['insumos_hoy'] = res['total'] if res and res.get('total') else 0
        
        res2 = ejecutar_sql("SELECT SUM(prod.precio) as total, COUNT(*) as qty FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO'", one=True)
        if res2:
            stats['prestamos_valor'] = res2['total'] if res2.get('total') else 0
            stats['prestamos_activos_qty'] = res2['qty']

        # Tabla En Uso (Si quieres mostrarla, si no, puedes quitarla del HTML)
        en_uso = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO' ORDER BY p.fecha_salida DESC")
        
        # Conteo Total Inventario (PARA EL BOTÓN 4)
        res_count = ejecutar_sql("SELECT COUNT(*) as c FROM productos", one=True)
        total_items = res_count['c'] if res_count else 0

    except Exception as e:
        print(f"Error dashboard: {e}")

    conn_type = "PostgreSQL" if DATABASE_URL else "SQLite"

    return render_template('dashboard.html', 
                           stats=stats, 
                           en_uso=en_uso, 
                           rol=session['rol'], 
                           total_items=total_items,  # <--- Enviamos el dato
                           db_path=conn_type)
# --- APIS (CON FILTRO DE HERRAMIENTAS) ---
@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q', '').lower().strip()
    res = ejecutar_sql("SELECT * FROM productos WHERE lower(nombre) LIKE %s OR lower(id) LIKE %s LIMIT 15", (f'%{q}%', f'%{q}%'))
    return jsonify([dict(r) for r in res])

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w_raw = request.args.get('worker_id', '').upper()
    w_clean = w_raw.replace('.', '').strip()
    
    # FILTRO CLAVE: AND p.tipo_item = 'HERRAMIENTA'
    # Solo mostramos lo que se debe devolver. Los insumos se ignoran.
    sql = '''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo
        FROM prestamos p JOIN productos prod ON p.tool_id = prod.id 
        WHERE (p.worker_id=%s OR p.worker_id=%s) 
        AND p.estado='ACTIVO' 
        AND (p.tipo_item='HERRAMIENTA' OR prod.tipo='HERRAMIENTA')
    '''
    res = ejecutar_sql(sql, (w_clean, w_raw))
    
    if not res:
        existe = ejecutar_sql("SELECT nombre FROM trabajadores WHERE rut=%s OR rut=%s", (w_clean, w_raw), one=True)
        if existe: return jsonify({'status':'empty', 'msg': f'Trabajador {existe["nombre"]} sin devoluciones pendientes.'})
        else: return jsonify({'status':'error', 'msg': 'RUT no registrado.'})
            
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

@app.route('/api/prestamos_ticket')
def api_prestamos_ticket():
    t_id = request.args.get('ticket_id', '').strip().upper()
    # Mismo filtro: Solo herramientas
    sql = '''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo
        FROM prestamos p JOIN productos prod ON p.tool_id = prod.id 
        WHERE p.transaction_id=%s AND p.estado='ACTIVO'
        AND (p.tipo_item='HERRAMIENTA' OR prod.tipo='HERRAMIENTA')
    '''
    res = ejecutar_sql(sql, (t_id,))
    if not res: return jsonify({'status':'error', 'msg': 'Ticket no encontrado o ya devuelto.'})
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

# --- PROCESOS OPERATIVOS ---
@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    d = request.json
    tid = str(uuid.uuid4())[:8].upper()
    try:
        for i in d['items']:
            # Descontar stock
            ejecutar_sql('UPDATE productos SET stock = stock - %s WHERE id=%s', (i['cantidad'], i['id']))
            # Definir estado: Insumo -> CONSUMIDO (Final), Herramienta -> ACTIVO (Pendiente)
            tipo = i.get('tipo', 'HERRAMIENTA')
            estado = 'ACTIVO' if tipo == 'HERRAMIENTA' else 'CONSUMIDO'
            
            ejecutar_sql('INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                         (tid, d['worker_id'], i['id'], tipo, i['cantidad'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), estado))
        return jsonify({'status':'ok', 'ticket_id': tid})
    except Exception as e: return jsonify({'status':'error', 'msg': str(e)})

@app.route('/procesar_devolucion_compleja', methods=['POST'])
def procesar_devolucion():
    items = request.json.get('items', [])
    ids_out = []
    for it in items:
        try:
            p = ejecutar_sql('SELECT * FROM prestamos WHERE id=%s', (it['id'],), one=True)
            qty_ret = int(it['cantidad'])
            
            # Reponer Stock
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (qty_ret, p['tool_id']))
            
            # Cerrar préstamo
            if qty_ret < p['cantidad']:
                ejecutar_sql('UPDATE prestamos SET cantidad=%s WHERE id=%s', (p['cantidad']-qty_ret, p['id']))
                # Crear registro histórico del retorno parcial
                ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, fecha_regreso, estado) VALUES (%s,%s,%s,%s,%s,%s,%s,'DEVUELTO')",
                             (p['transaction_id'], p['worker_id'], p['tool_id'], p['tipo_item'], qty_ret, p['fecha_salida'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                nid = ejecutar_sql("SELECT id FROM prestamos WHERE worker_id=%s AND estado='DEVUELTO' ORDER BY id DESC LIMIT 1", (p['worker_id'],), one=True)
                ids_out.append(str(nid['id']))
            else:
                ejecutar_sql("UPDATE prestamos SET estado='DEVUELTO', fecha_regreso=%s WHERE id=%s", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p['id']))
                ids_out.append(str(p['id']))
        except: pass
    return jsonify({'status':'ok', 'ids': ",".join(ids_out)})

# --- CONFIGURACIÓN CON TELEMETRÍA ---
@app.route('/admin/config', methods=['GET', 'POST'])
def admin_config():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method=='POST':
        for k in ['empresa_nombre', 'ticket_footer', 'impresora_nombre', 'empresa_direccion']:
            ejecutar_sql('DELETE FROM config WHERE clave=%s', (k,))
            ejecutar_sql('INSERT INTO config (clave, valor) VALUES (%s,%s)', (k, request.form.get(k,'')))
        flash('✅ Configuración guardada.')

    # Datos Telemetría Servidor
    server_info = {
        'os': f"{platform.system()} {platform.release()}",
        'node': platform.node(),
        'python': platform.python_version(),
        'time_server': datetime.now().strftime("%H:%M:%S"),
        'timezone': time.tzname[0],
        'db_mode': 'PostgreSQL Cloud' if DATABASE_URL else 'SQLite Local',
        'app_path': os.getcwd()
    }

    try: c = {r['clave']:r['valor'] for r in ejecutar_sql('SELECT * FROM config')}
    except: c = {}
    
    defaults = {'empresa_nombre':'IronTrace', 'ticket_footer':'Gracias', 'impresora_nombre':'POS-80', 'empresa_direccion':''}
    return render_template('config_admin.html', config={**defaults, **c}, server=server_info)

# --- RESTO DE RUTAS (VISTAS) ---
@app.route('/operador')
def panel_operador(): return render_template('operador.html') if 'user' in session else redirect(url_for('login'))

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    movs = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE transaction_id=%s", (ticket_id,))
    if not movs: return "Error Ticket"
    w = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (movs[0]['worker_id'],), one=True)
    try: c = {r['clave']:r['valor'] for r in ejecutar_sql('SELECT * FROM config')}
    except: c = {}
    return render_template('ticket.html', ticket_id=ticket_id, worker=w, items=movs, config=c, fecha=movs[0]['fecha_salida'])

@app.route('/ticket_devolucion')
def ver_ticket_dev():
    ids = request.args.get('ids','').split(',')
    if not ids[0]: return "Error"
    # Generar placeholders para SQL IN (?,?,?)
    ph = ','.join(['%s']*len(ids))
    sql = f"SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.id IN ({ph})"
    items = ejecutar_sql(sql, tuple(ids))
    w = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (items[0]['worker_id'],), one=True)
    try: c = {r['clave']:r['valor'] for r in ejecutar_sql('SELECT * FROM config')}
    except: c = {}
    return render_template('ticket_devolucion.html', items=items, worker=w, config=c, fecha=datetime.now().strftime("%d/%m/%Y %H:%M"), ids=request.args.get('ids'))

# --- MANTENIMIENTO ---
@app.route('/fix_db_final')
def fix_db():
    # ... (Mantener tu lógica de fix anterior aquí)
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
