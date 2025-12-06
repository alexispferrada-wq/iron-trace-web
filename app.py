from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import platform
import io
from datetime import datetime
import pytz # Importante para la hora de Chile

# ReportLab para PDFs
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# --- CONFIGURACIÓN BASE DE DATOS ---
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

# --- CONFIGURACIÓN HORARIA (CHILE) ---
def get_chile_time():
    """Retorna la fecha y hora actual en Santiago de Chile"""
    chile_tz = pytz.timezone('America/Santiago')
    return datetime.now(chile_tz)

def get_str_now():
    """Retorna string fecha hora SQL compatible"""
    return get_chile_time().strftime("%Y-%m-%d %H:%M:%S")

def get_db_connection():
    if DATABASE_URL:
        if not psycopg2: raise ImportError("Falta psycopg2 para modo nube")
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
        print(f"SQL Error: {e}")
        raise e
    finally:
        conn.close()

# --- INICIALIZACIÓN ROBUSTA (AUTO-REPARACIÓN) ---
def init_db():
    conn, db_type = get_db_connection()
    c = conn.cursor()
    # Usamos sintaxis compatible PostgreSQL / SQLite
    tipo_text = "TEXT" if db_type == 'SQLITE' else "VARCHAR(255)"
    
    queries = [
        f"CREATE TABLE IF NOT EXISTS usuarios (username {tipo_text} PRIMARY KEY, password {tipo_text}, rol {tipo_text})",
        f"CREATE TABLE IF NOT EXISTS trabajadores (rut {tipo_text} PRIMARY KEY, nombre {tipo_text}, correo {tipo_text}, seccion {tipo_text}, faena {tipo_text})",
        f"CREATE TABLE IF NOT EXISTS productos (id {tipo_text} PRIMARY KEY, nombre {tipo_text}, precio INTEGER, stock INTEGER, tipo {tipo_text})",
        f"CREATE TABLE IF NOT EXISTS config (clave {tipo_text} PRIMARY KEY, valor {tipo_text})",
        f"CREATE TABLE IF NOT EXISTS facturas (id SERIAL PRIMARY KEY, numero {tipo_text}, fecha {tipo_text}, usuario {tipo_text})" if db_type == 'POSTGRES' else 
        f"CREATE TABLE IF NOT EXISTS facturas (id INTEGER PRIMARY KEY AUTOINCREMENT, numero {tipo_text}, fecha {tipo_text}, usuario {tipo_text})",
        # Prestamos (Ojo con SERIAL vs AUTOINCREMENT)
        f"""CREATE TABLE IF NOT EXISTS prestamos (
            id SERIAL PRIMARY KEY, transaction_id {tipo_text}, worker_id {tipo_text}, tool_id {tipo_text}, 
            tipo_item {tipo_text}, cantidad INTEGER, fecha_salida {tipo_text}, fecha_regreso {tipo_text}, estado {tipo_text})""" if db_type == 'POSTGRES' else
        f"""CREATE TABLE IF NOT EXISTS prestamos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id {tipo_text}, worker_id {tipo_text}, tool_id {tipo_text}, 
            tipo_item {tipo_text}, cantidad INTEGER, fecha_salida {tipo_text}, fecha_regreso {tipo_text}, estado {tipo_text})"""
    ]
    
    for q in queries:
        try: 
            c.execute(q)
            conn.commit()
        except Exception as e: 
            print(f"Init Error ({q[:20]}...): {e}")
            conn.rollback()
            
    # Crear admin default si no existe
    try:
        check = c.execute("SELECT * FROM usuarios WHERE username='admin'")
        if not c.fetchone():
            c.execute(f"INSERT INTO usuarios (username, password, rol) VALUES ('admin', 'admin123', 'admin')")
            conn.commit()
    except: pass
    
    conn.close()

# Ejecutar al inicio (Importante para Render)
init_db()

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
        except Exception as e: flash(f'Error: {e}')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

# --- DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    if 'user' not in session: return redirect(url_for('login'))
    if session.get('rol') == 'operador': return redirect(url_for('panel_operador'))
    
    stats = {'insumos_hoy':0, 'prestamos_valor':0, 'prestamos_qty':0}
    en_uso = []; alertas = []
    
    try:
        hoy = get_chile_time().strftime("%Y-%m-%d")
        res = ejecutar_sql("SELECT SUM(p.cantidad * prod.precio) as t FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.tipo_item='INSUMO' AND p.fecha_salida LIKE %s", (f'{hoy}%',), one=True)
        stats['insumos_hoy'] = res['t'] if res and res['t'] else 0
        
        res2 = ejecutar_sql("SELECT SUM(prod.precio) as t, COUNT(*) as c FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.estado='ACTIVO'", one=True)
        if res2: stats['prestamos_valor'] = res2['t'] or 0; stats['prestamos_qty'] = res2['c']
        
        alertas = ejecutar_sql("SELECT * FROM productos WHERE tipo='INSUMO' AND stock <= 10 ORDER BY stock ASC LIMIT 5")
        en_uso = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.estado='ACTIVO' ORDER BY p.fecha_salida DESC LIMIT 20")
    except: pass

    server_info = {'time_server': get_chile_time().strftime("%H:%M:%S (CLT)"), 'db_mode': 'PostgreSQL' if DATABASE_URL else 'SQLite Local', 'os': platform.system()}
    
    return render_template('dashboard.html', stats=stats, en_uso=en_uso, alertas=alertas, rol=session['rol'], server=server_info)

# --- TRABAJADORES ---
@app.route('/trabajadores')
def gestion_trabajadores():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    trabajadores = ejecutar_sql("SELECT * FROM trabajadores ORDER BY nombre ASC")
    return render_template('trabajadores.html', trabajadores=trabajadores)

@app.route('/trabajadores/guardar', methods=['POST'])
def guardar_trabajador():
    if 'user' not in session: return redirect(url_for('login'))
    rut = request.form['rut'].replace('.', '').strip().upper()
    try:
        ejecutar_sql('DELETE FROM trabajadores WHERE rut=%s', (rut,))
        ejecutar_sql('INSERT INTO trabajadores (rut, nombre, correo, seccion, faena) VALUES (%s,%s,%s,%s,%s)', 
                     (rut, request.form['nombre'], request.form['correo'], request.form['seccion'], request.form['faena']))
        flash('Trabajador guardado.')
    except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('gestion_trabajadores'))

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
        
        try: ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)', (f"{doc} ({pid})", get_str_now(), session['user']))
        except: pass

        prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
        if prod:
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (cant, pid))
        else:
            ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)', (pid, f'NUEVO {pid}', 0, cant, 'INSUMO'))
        flash(f'✅ Stock +{cant} para {pid}')
    except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('vista_inventario'))

# --- OPERADOR ---
@app.route('/operador')
def panel_operador(): return render_template('operador.html') if 'user' in session else redirect(url_for('login'))

@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q','').lower()
    return jsonify([dict(r) for r in ejecutar_sql("SELECT * FROM productos WHERE lower(nombre) LIKE %s LIMIT 10", (f'%{q}%',))])

@app.route('/api/buscar_trabajador')
def api_buscar_trabajador():
    q = request.args.get('q', '').upper().strip()
    if not q: return jsonify([])
    sql = "SELECT * FROM trabajadores WHERE rut LIKE %s OR upper(nombre) LIKE %s LIMIT 5"
    res = ejecutar_sql(sql, (f'%{q}%', f'%{q}%'))
    return jsonify([dict(r) for r in res])

@app.route('/api/prestamos_ticket')
def api_prestamos_ticket():
    tid = request.args.get('ticket_id', '').replace('TICKET:', '').strip().upper()
    sql = '''SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo 
             FROM prestamos p JOIN productos prod ON p.tool_id = prod.id 
             WHERE p.transaction_id=%s AND p.estado='ACTIVO' '''
    res = ejecutar_sql(sql, (tid,))
    if not res: return jsonify({'status': 'error', 'msg': 'Ticket no encontrado o ya devuelto'})
    return jsonify({'status': 'ok', 'data': [dict(r) for r in res]})

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w = request.args.get('worker_id', '').upper().replace('.', '').strip()
    sql = '''SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo 
             FROM prestamos p JOIN productos prod ON p.tool_id = prod.id 
             WHERE p.worker_id=%s AND p.estado='ACTIVO' AND p.tipo_item='HERRAMIENTA' '''
    res = ejecutar_sql(sql, (w,))
    if not res: return jsonify({'status':'empty', 'msg': f'Sin devoluciones pendientes para {w}'})
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    data = request.json
    worker_id = data.get('worker_id', '').upper().replace('.', '').strip()
    items = data.get('items', [])
    if not worker_id or not items: return jsonify({'status':'error', 'msg': 'Datos incompletos'})
    
    tx_id = str(uuid.uuid4())[:8].upper()
    
    try:
        for it in items:
            ejecutar_sql("UPDATE productos SET stock = stock - %s WHERE id=%s", (it['cantidad'], it['id']))
            estado = 'ACTIVO' if it['tipo'] == 'HERRAMIENTA' else 'CONSUMIDO'
            ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (tx_id, worker_id, it['id'], it['tipo'], it['cantidad'], get_str_now(), estado))
        return jsonify({'status':'ok', 'ticket_id': tx_id})
    except Exception as e: return jsonify({'status':'error', 'msg': str(e)})

@app.route('/procesar_devolucion_compleja', methods=['POST'])
def procesar_devolucion():
    items = request.json.get('items', [])
    ids_out = []
    for it in items:
        try:
            p = ejecutar_sql('SELECT * FROM prestamos WHERE id=%s', (it['id'],), one=True)
            if not p: continue
            qty_ret = int(it['cantidad'])
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (qty_ret, p['tool_id']))
            
            if qty_ret < p['cantidad']:
                ejecutar_sql('UPDATE prestamos SET cantidad=%s WHERE id=%s', (p['cantidad']-qty_ret, p['id']))
                ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, fecha_regreso, estado) VALUES (%s,%s,%s,%s,%s,%s,%s,'DEVUELTO')",
                             (p['transaction_id'], p['worker_id'], p['tool_id'], p['tipo_item'], qty_ret, p['fecha_salida'], get_str_now()))
                nid = ejecutar_sql("SELECT id FROM prestamos WHERE worker_id=%s AND estado='DEVUELTO' ORDER BY id DESC LIMIT 1", (p['worker_id'],), one=True)
                ids_out.append(str(nid['id']))
            else:
                ejecutar_sql("UPDATE prestamos SET estado='DEVUELTO', fecha_regreso=%s WHERE id=%s", (get_str_now(), p['id']))
                ids_out.append(str(p['id']))
        except: pass
    return jsonify({'status':'ok', 'ids': ",".join(ids_out)})

# --- REPORTES AVANZADOS (GRÁFICOS) ---
@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    term = request.form.get('search_term', '').strip().upper()
    
    # 1. Lista General
    sql = "SELECT p.*, prod.nombre, prod.precio FROM prestamos p JOIN productos prod ON p.tool_id=prod.id"
    if term: sql += f" WHERE p.worker_id LIKE '%{term}%' OR p.tool_id LIKE '%{term}%' OR prod.nombre LIKE '%{term}%'"
    sql += " ORDER BY p.fecha_salida DESC LIMIT 100"
    movs = ejecutar_sql(sql)
    
    total_insu = sum([(m['precio'] or 0)*m['cantidad'] for m in movs if m.get('tipo_item')=='INSUMO'])
    
    # 2. Datos para Gráfico: Insumos por Día (Últimos 7 días)
    # Nota: SQLite y Postgres usan substr/substring diferente, hacemos algo genérico en Python por ahora para compatibilidad rápida
    raw_insumos = ejecutar_sql("SELECT p.fecha_salida, (p.cantidad * prod.precio) as total FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.tipo_item='INSUMO' ORDER BY p.fecha_salida DESC LIMIT 200")
    
    chart_days = {}
    for r in raw_insumos:
        dia = str(r['fecha_salida'])[:10] # Tomar YYYY-MM-DD
        chart_days[dia] = chart_days.get(dia, 0) + (r['total'] or 0)
    
    # Ordenar por fecha
    fechas_ord = sorted(chart_days.keys())
    data_insumos = {'labels': fechas_ord, 'values': [chart_days[d] for d in fechas_ord]}

    # 3. Datos para Gráfico: Herramientas por Faena
    # Requiere JOIN con tabla trabajadores
    try:
        raw_faenas = ejecutar_sql("SELECT t.faena, COUNT(*) as c FROM prestamos p JOIN trabajadores t ON p.worker_id = t.rut WHERE p.tipo_item='HERRAMIENTA' AND p.estado='ACTIVO' GROUP BY t.faena")
        data_faenas = {'labels': [r['faena'] for r in raw_faenas], 'values': [r['c'] for r in raw_faenas]}
    except:
        data_faenas = {'labels': [], 'values': []} # Por si falla la tabla trabajadores

    return render_template('reportes.html', movimientos=movs, total_insumos=total_insu, search_term=term, chart_days=data_insumos, chart_faenas=data_faenas)

@app.route('/reportes/descargar_pdf')
def descargar_reporte_pdf():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16); c.drawString(30, height - 40, "Reporte Iron Trace")
    c.setFont("Helvetica", 10); c.drawString(30, height - 60, f"Fecha: {get_str_now()}")
    
    y =
