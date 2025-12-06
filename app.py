from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import csv
import io
import platform
from datetime import datetime

# --- CONFIGURACIÓN HÍBRIDA ---
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'clave_maestra_iron_trace_final_v5')
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
        if sql.strip().upper().startswith(('SELECT', 'WITH')):
            rv = cursor.fetchone() if one else cursor.fetchall()
            if db_type == 'POSTGRES' and rv:
                rv = dict(rv) if one else [dict(row) for row in rv]
            return rv
        else:
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        # print(f"SQL Error: {e}") # Debug off
        raise e
    finally:
        conn.close()

# --- LOGIN / LOGOUT ---
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
    
    stats = {'insumos_hoy':0, 'prestamos_valor':0, 'prestamos_qty':0}
    en_uso = []
    
    try:
        hoy = datetime.now().strftime("%Y-%m-%d")
        res = ejecutar_sql("SELECT SUM(p.cantidad * prod.precio) as t FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.tipo_item='INSUMO' AND p.fecha_salida LIKE %s", (f'{hoy}%',), one=True)
        stats['insumos_hoy'] = res['t'] if res and res.get('t') else 0
        
        res2 = ejecutar_sql("SELECT SUM(prod.precio) as t, COUNT(*) as c FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.estado='ACTIVO'", one=True)
        if res2: stats['prestamos_valor'] = res2['t'] or 0; stats['prestamos_qty'] = res2['c']
        
        en_uso = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.estado='ACTIVO' ORDER BY p.fecha_salida DESC")
    except: pass

    return render_template('dashboard.html', stats=stats, en_uso=en_uso, rol=session['rol'])

# --- INVENTARIO ---
@app.route('/inventario')
def vista_inventario():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    try:
        productos = ejecutar_sql('SELECT * FROM productos ORDER BY id')
        try: facturas = ejecutar_sql('SELECT * FROM facturas ORDER BY id DESC LIMIT 50')
        except: facturas = []
    except: productos, facturas = [], []
    return render_template('inventario.html', productos=productos, facturas=facturas)

@app.route('/inventario/ingreso_manual', methods=['POST'])
def ingreso_manual_stock():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    try:
        pid = request.form['id_producto'].strip()
        cant = int(request.form['cantidad'])
        doc = request.form.get('num_documento', 'MANUAL')
        
        # Intentar guardar factura si existe tabla
        try: ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)', (f"{doc} ({pid})", datetime.now().strftime("%Y-%m-%d %H:%M"), session['user']))
        except: pass

        prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
        if prod:
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (cant, pid))
        else:
            ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)', (pid, f'NUEVO {pid}', 0, cant, 'INSUMO'))
        flash(f'✅ Stock actualizado: +{cant} para {pid}')
    except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('vista_inventario'))

# --- OPERADOR ---
@app.route('/operador')
def panel_operador(): return render_template('operador.html') if 'user' in session else redirect(url_for('login'))

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w = request.args.get('worker_id', '').upper().replace('.', '').strip()
    sql = '''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo 
        FROM prestamos p JOIN productos prod ON p.tool_id = prod.id 
        WHERE (p.worker_id=%s OR p.worker_id=%s) AND p.estado='ACTIVO' AND p.tipo_item='HERRAMIENTA'
    '''
    res = ejecutar_sql(sql, (w, request.args.get('worker_id')))
    if not res: return jsonify({'status':'error', 'msg': f'Sin devoluciones pendientes para {w}'})
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

@app.route('/procesar_devolucion_compleja', methods=['POST'])
def procesar_devolucion():
    items = request.json.get('items', [])
    ids_out = []
    for it in items:
        try:
            p = ejecutar_sql('SELECT * FROM prestamos WHERE id=%s', (it['id'],), one=True)
            qty_ret = int(it['cantidad'])
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (qty_ret, p['tool_id']))
            if qty_ret < p['cantidad']:
                ejecutar_sql('UPDATE prestamos SET cantidad=%s WHERE id=%s', (p['cantidad']-qty_ret, p['id']))
                ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, fecha_regreso, estado) VALUES (%s,%s,%s,%s,%s,%s,%s,'DEVUELTO')",
                             (p['transaction_id'], p['worker_id'], p['tool_id'], p['tipo_item'], qty_ret, p['fecha_salida'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                nid = ejecutar_sql("SELECT id FROM prestamos WHERE worker_id=%s AND estado='DEVUELTO' ORDER BY id DESC LIMIT 1", (p['worker_id'],), one=True)
                ids_out.append(str(nid['id']))
            else:
                ejecutar_sql("UPDATE prestamos SET estado='DEVUELTO', fecha_regreso=%s WHERE id=%s", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p['id']))
                ids_out.append(str(p['id']))
        except: pass
    return jsonify({'status':'ok', 'ids': ",".join(ids_out)})

# --- REPORTES ---
@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    term = request.form.get('search_term', '').strip().upper()
    sql = "SELECT p.*, prod.nombre, prod.precio FROM prestamos p JOIN productos prod ON p.tool_id=prod.id"
    if term: sql += f" WHERE p.worker_id LIKE '%{term}%' OR p.tool_id LIKE '%{term}%'"
    sql += " ORDER BY p.fecha_salida DESC"
    movs = ejecutar_sql(sql)
    total_insu = sum([(m['precio'] or 0)*m['cantidad'] for m in movs if m.get('tipo_item')=='INSUMO'])
    return render_template('reportes.html', movimientos=movs, total_insumos=total_insu, search_term=term)

# --- USUARIOS ---
@app.route('/usuarios')
def gestion_usuarios():
    sql = "SELECT * FROM usuarios" if session['rol']=='admin' else "SELECT * FROM usuarios WHERE rol='operador'"
    return render_template('users.html', usuarios=ejecutar_sql(sql), rol_actual=session['rol'])

# --- MANTENIMIENTO BD ---
@app.route('/fix_db_final')
def fix_db():
    try:
        ejecutar_sql("CREATE TABLE IF NOT EXISTS facturas (id SERIAL PRIMARY KEY, numero TEXT, fecha TEXT, usuario TEXT)")
        return "BD Reparada"
    except Exception as e: return f"Error: {e}"

@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q','').lower()
    return jsonify([dict(r) for r in ejecutar_sql("SELECT * FROM productos WHERE lower(nombre) LIKE %s LIMIT 10", (f'%{q}%',))])

# =========================================================
# === RUTAS FALTANTES AGREGADAS (V5) ======================
# =========================================================

# 1. RUTA TRABAJADORES (FALTABA)
@app.route('/trabajadores')
def gestion_trabajadores():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    trabajadores = ejecutar_sql("SELECT * FROM trabajadores")
    return render_template('trabajadores.html', trabajadores=trabajadores)

@app.route('/trabajadores/guardar', methods=['POST'])
def guardar_trabajador():
    if 'user' not in session: return redirect(url_for('login'))
    rut = request.form['rut'].replace('.', '').strip().upper()
    try:
        ejecutar_sql('DELETE FROM trabajadores WHERE rut=%s', (rut,))
        ejecutar_sql('INSERT INTO trabajadores (rut, nombre, correo, seccion, faena) VALUES (%s,%s,%s,%s,%s)', 
                     (rut, request.form['nombre'], request.form['correo'], request.form['seccion'], request.form['faena']))
        flash('Trabajador guardado exitosamente.')
    except Exception as e:
        flash(f'Error al guardar: {e}')
    return redirect(url_for('gestion_trabajadores'))

# 2. RUTA CONFIGURACIÓN ADMIN (FALTABA)
@app.route('/admin/config', methods=['GET', 'POST'])
def configuracion_global():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        for key, val in request.form.items():
            # Upsert simple para configuración
            check = ejecutar_sql("SELECT 1 FROM config WHERE clave=%s", (key,), one=True)
            if check: ejecutar_sql("UPDATE config SET valor=%s WHERE clave=%s", (val, key))
            else: ejecutar_sql("INSERT INTO config (clave, valor) VALUES (%s, %s)", (key, val))
        flash('Configuración actualizada')

    # Cargar config
    conf_list = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in conf_list} if conf_list else {}
    
    server_info = {
        'time_server': datetime.now().strftime("%H:%M:%S"), 'timezone': 'Local',
        'db_mode': 'PostgreSQL' if DATABASE_URL else 'SQLite Local',
        'os': platform.system(), 'node': platform.node(), 'python': platform.python_version(),
        'app_path': os.getcwd()
    }
    return render_template('config_admin.html', config=config, server=server_info)

# 3. RUTA TICKET SALIDA (FALTABA)
@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    data = request.json
    worker_id = data.get('worker_id', '').upper().replace('.', '').strip()
    items = data.get('items', [])
    
    if not worker_id or not items: return jsonify({'status':'error', 'msg': 'Datos incompletos'})
    
    tx_id = str(uuid.uuid4())[:8].upper()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        for it in items:
            # Descontar stock
            ejecutar_sql("UPDATE productos SET stock = stock - %s WHERE id=%s", (it['cantidad'], it['id']))
            # Registrar préstamo
            estado = 'ACTIVO' if it['tipo'] == 'HERRAMIENTA' else 'CONSUMIDO'
            ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (tx_id, worker_id, it['id'], it['tipo'], it['cantidad'], now, estado))
        
        return jsonify({'status':'ok', 'ticket_id': tx_id})
    except Exception as e:
        return jsonify({'status':'error', 'msg': str(e)})

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    # Buscar items del ticket
    items = ejecutar_sql("SELECT p.cantidad, prod.nombre, p.tipo_item FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.transaction_id=%s", (ticket_id,))
    if not items: return "Ticket no encontrado"
    
    # Buscar trabajador
    wid_res = ejecutar_sql("SELECT worker_id, fecha_salida FROM prestamos WHERE transaction_id=%s LIMIT 1", (ticket_id,), one=True)
    worker = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (wid_res['worker_id'],), one=True)
    
    # Config
    conf_list = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in conf_list} if conf_list else {}
    
    return render_template('ticket.html', ticket_id=ticket_id, items=items, worker=worker, fecha=wid_res['fecha_salida'], config=config)

# 4. RUTA TICKET DEVOLUCIÓN (EXTRA)
@app.route('/ticket_devolucion')
def ticket_devolucion():
    ids = request.args.get('ids', '').split(',')
    # Filtrar IDs seguros
    safe_ids = [str(int(x)) for x in ids if x.isdigit()]
    if not safe_ids: return "IDs inválidos"
    
    placeholders = ','.join(['%s'] * len(safe_ids))
    sql = f"SELECT p.cantidad, prod.nombre, p.tool_id, p.worker_id, p.fecha_regreso FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.id IN ({placeholders})"
    
    items = ejecutar_sql(sql, tuple(safe_ids))
    if not items: return "Error buscando ítems"
    
    worker = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (items[0]['worker_id'],), one=True)
    conf_list = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in conf_list} if conf_list else {}
    
    return render_template('ticket_devolucion.html', ids=ids[0], items=items, worker=worker, fecha=items[0]['fecha_regreso'], config=config)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
