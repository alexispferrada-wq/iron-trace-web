from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import platform
import io
import sys
import csv # Necesario para la carga masiva
from datetime import datetime
import pytz

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

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

def get_chile_time():
    return datetime.now(pytz.timezone('America/Santiago'))

def get_str_now():
    return get_chile_time().strftime("%Y-%m-%d %H:%M:%S")

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
        print(f"SQL Error: {e}")
        raise e
    finally:
        conn.close()

def init_db():
    conn, db_type = get_db_connection()
    c = conn.cursor()
    t_text = "TEXT" if db_type == 'SQLITE' else "VARCHAR(255)"
    
    queries = [
        f"CREATE TABLE IF NOT EXISTS usuarios (username {t_text} PRIMARY KEY, password {t_text}, rol {t_text})",
        f"CREATE TABLE IF NOT EXISTS trabajadores (rut {t_text} PRIMARY KEY, nombre {t_text}, correo {t_text}, seccion {t_text}, faena {t_text})",
        f"CREATE TABLE IF NOT EXISTS productos (id {t_text} PRIMARY KEY, nombre {t_text}, precio INTEGER, stock INTEGER, tipo {t_text})",
        f"CREATE TABLE IF NOT EXISTS config (clave {t_text} PRIMARY KEY, valor {t_text})",
        f"CREATE TABLE IF NOT EXISTS facturas (id SERIAL PRIMARY KEY, numero {t_text}, fecha {t_text}, usuario {t_text})" if db_type == 'POSTGRES' else 
        f"CREATE TABLE IF NOT EXISTS facturas (id INTEGER PRIMARY KEY AUTOINCREMENT, numero {t_text}, fecha {t_text}, usuario {t_text})",
        f"CREATE TABLE IF NOT EXISTS prestamos (id SERIAL PRIMARY KEY, transaction_id {t_text}, worker_id {t_text}, tool_id {t_text}, tipo_item {t_text}, cantidad INTEGER, fecha_salida {t_text}, fecha_regreso {t_text}, estado {t_text})" if db_type == 'POSTGRES' else
        f"CREATE TABLE IF NOT EXISTS prestamos (id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id {t_text}, worker_id {t_text}, tool_id {t_text}, tipo_item {t_text}, cantidad INTEGER, fecha_salida {t_text}, fecha_regreso {t_text}, estado {t_text})",
        f"CREATE TABLE IF NOT EXISTS bajas (id SERIAL PRIMARY KEY, producto_id {t_text}, cantidad INTEGER, motivo {t_text}, fecha {t_text}, usuario {t_text})" if db_type == 'POSTGRES' else
        f"CREATE TABLE IF NOT EXISTS bajas (id INTEGER PRIMARY KEY AUTOINCREMENT, producto_id {t_text}, cantidad INTEGER, motivo {t_text}, fecha {t_text}, usuario {t_text})"
    ]
    
    for q in queries:
        try: c.execute(q); conn.commit()
        except: conn.rollback()
    
    try:
        if not c.execute("SELECT * FROM usuarios WHERE username='admin'").fetchone():
            c.execute(f"INSERT INTO usuarios (username, password, rol) VALUES ('admin', 'admin123', 'admin')")
            conn.commit()
    except: pass
    conn.close()

init_db()

# --- RUTAS ---
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
        except: flash('Error DB')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

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

    server_info = {'time_server': get_chile_time().strftime("%H:%M:%S"), 'db_mode': 'PostgreSQL' if DATABASE_URL else 'SQLite', 'os': platform.system()}
    config_raw = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in config_raw} if config_raw else {}

    return render_template('dashboard.html', stats=stats, en_uso=en_uso, alertas=alertas, rol=session['rol'], server=server_info, config=config)

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
        facturas = ejecutar_sql('SELECT * FROM facturas ORDER BY id DESC LIMIT 20')
        bajas = ejecutar_sql('SELECT * FROM bajas ORDER BY id DESC LIMIT 20')
    except: productos, facturas, bajas = [], [], []
    return render_template('inventario.html', productos=productos, facturas=facturas, bajas=bajas)

@app.route('/inventario/ingreso_manual', methods=['POST'])
def ingreso_manual_stock():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    try:
        pid = request.form['id_producto'].strip()
        cant = int(request.form['cantidad'])
        doc = request.form.get('num_documento', 'MANUAL')
        
        ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)', (f"{doc} ({pid})", get_str_now(), session['user']))
        prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
        if prod:
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (cant, pid))
        else:
            ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)', (pid, f'NUEVO {pid}', 0, cant, 'INSUMO'))
        flash(f'✅ Stock actualizado')
    except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('vista_inventario'))

@app.route('/inventario/dar_baja', methods=['POST'])
def dar_baja_producto():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    pid = request.form['id_producto']; cant = int(request.form['cantidad']); motivo = request.form['motivo']
    try:
        prod = ejecutar_sql("SELECT * FROM productos WHERE id=%s", (pid,), one=True)
        if prod and prod['stock'] >= cant:
            ejecutar_sql("UPDATE productos SET stock = stock - %s WHERE id=%s", (cant, pid))
            ejecutar_sql("INSERT INTO bajas (producto_id, cantidad, motivo, fecha, usuario) VALUES (%s,%s,%s,%s,%s)", (pid, cant, motivo, get_str_now(), session['user']))
            flash(f"⚠️ Baja registrada: {pid}")
        else: flash("Error: Stock insuficiente")
    except Exception as e: flash(f"Error: {e}")
    return redirect(url_for('vista_inventario'))

# --- CONFIGURACIÓN ADMIN Y CARGA MASIVA ---
@app.route('/admin/config', methods=['GET', 'POST'])
def configuracion_global():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        if 'archivo_csv' in request.files:
            # PROCESAR CARGA MASIVA DE FACTURA
            file = request.files['archivo_csv']
            num_fac = request.form.get('num_factura', 'MASIVA')
            if file and file.filename.endswith('.csv'):
                try:
                    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                    csv_input = csv.reader(stream)
                    count = 0
                    # Registrar la factura global
                    ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)', 
                                 (f"MASIVA: {num_fac}", get_str_now(), session['user']))
                    
                    for row in csv_input:
                        if len(row) >= 2: # Minimo ID y Cantidad
                            pid = row[0].strip()
                            cant = int(row[1])
                            precio = int(row[2]) if len(row) > 2 and row[2].isdigit() else 0
                            
                            prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
                            if prod:
                                sql_upd = 'UPDATE productos SET stock = stock + %s'
                                params = [cant]
                                if precio > 0: 
                                    sql_upd += ', precio = %s'
                                    params.append(precio)
                                sql_upd += ' WHERE id=%s'
                                params.append(pid)
                                ejecutar_sql(sql_upd, tuple(params))
                            else:
                                ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)', 
                                             (pid, f'NUEVO {pid}', precio, cant, 'INSUMO'))
                            count += 1
                    flash(f'✅ Se procesaron {count} líneas del CSV')
                except Exception as e: flash(f'Error al procesar CSV: {e}')
        else:
            # GUARDAR CONFIGURACIÓN NORMAL
            for key, val in request.form.items():
                if key != 'archivo_csv' and key != 'num_factura':
                    check = ejecutar_sql("SELECT 1 FROM config WHERE clave=%s", (key,), one=True)
                    if check: ejecutar_sql("UPDATE config SET valor=%s WHERE clave=%s", (val, key))
                    else: ejecutar_sql("INSERT INTO config (clave, valor) VALUES (%s, %s)", (key, val))
            flash('Configuración actualizada')

    conf_list = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in conf_list} if conf_list else {}
    
    # INFO SERVIDOR EXPANDIDA
    server_info = {
        'time_server': get_chile_time().strftime("%H:%M:%S (CLT)"),
        'timezone': 'America/Santiago',
        'db_mode': 'PostgreSQL' if DATABASE_URL else 'SQLite Local',
        'os': f"{platform.system()} {platform.release()}",
        'node': platform.node(),
        'python': platform.python_version(),
        'cpu': os.cpu_count(),
        'app_path': os.getcwd()
    }
    
    return render_template('config_admin.html', config=config, server=server_info)

# --- REPORTES, USUARIOS, APIs ---
@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    term = request.form.get('search_term', '').strip().upper()
    sql = "SELECT p.*, prod.nombre, prod.precio FROM prestamos p JOIN productos prod ON p.tool_id=prod.id"
    if term: sql += f" WHERE p.worker_id LIKE '%{term}%' OR p.tool_id LIKE '%{term}%' OR prod.nombre LIKE '%{term}%'"
    sql += " ORDER BY p.fecha_salida DESC LIMIT 100"
    movs = ejecutar_sql(sql)
    
    stock_bodega = ejecutar_sql("SELECT SUM(stock) as t FROM productos WHERE tipo='HERRAMIENTA'", one=True)['t'] or 0
    stock_terreno = ejecutar_sql("SELECT COUNT(*) as t FROM prestamos WHERE estado='ACTIVO' AND tipo_item='HERRAMIENTA'", one=True)['t'] or 0
    data_comparativa = {'labels': ['En Bodega', 'En Terreno'], 'values': [stock_bodega, stock_terreno]}

    raw_insumos = ejecutar_sql("SELECT p.fecha_salida, (p.cantidad * prod.precio) as total FROM prestamos p JOIN productos prod ON p.tool_id=prod.id WHERE p.tipo_item='INSUMO' ORDER BY p.fecha_salida DESC LIMIT 200")
    chart_days = {}
    for r in raw_insumos:
        dia = str(r['fecha_salida'])[:10]
        chart_days[dia] = chart_days.get(dia, 0) + (r['total'] or 0)
    fechas_ord = sorted(chart_days.keys())
    data_insumos = {'labels': fechas_ord, 'values': [chart_days[d] for d in fechas_ord]}

    return render_template('reportes.html', movimientos=movs, search_term=term, chart_days=data_insumos, chart_comparativa=data_comparativa)

@app.route('/reportes/descargar_pdf')
def descargar_reporte_pdf():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    buffer = io.BytesIO(); c = canvas.Canvas(buffer, pagesize=letter); width, height = letter
    c.drawString(30, height - 40, "Reporte Iron Trace"); y = height - 90
    movs = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id=prod.id ORDER BY p.fecha_salida DESC LIMIT 50")
    for m in movs:
        c.drawString(30, y, f"{str(m['fecha_salida'])[:10]} - {m['worker_id']} - {m['nombre'][:20]} - {m['estado']}"); y-=15
        if y<50: c.showPage(); y=height-50
    c.save(); buffer.seek(0); r=make_response(buffer.getvalue()); r.headers['Content-Type']='application/pdf'; r.headers['Content-Disposition']='attachment; filename=reporte.pdf'; return r

@app.route('/usuarios')
def gestion_usuarios():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    return render_template('users.html', usuarios=ejecutar_sql("SELECT * FROM usuarios"), rol_actual=session['rol'])

@app.route('/usuarios/guardar', methods=['POST'])
def guardar_usuario():
    if session.get('rol') != 'admin': return "Acceso Denegado"
    u, p, r = request.form['username'], request.form['password'], request.form['rol']
    existe = ejecutar_sql("SELECT * FROM usuarios WHERE username=%s", (u,), one=True)
    if existe:
        if p: ejecutar_sql("UPDATE usuarios SET password=%s, rol=%s WHERE username=%s", (p, r, u))
        else: ejecutar_sql("UPDATE usuarios SET rol=%s WHERE username=%s", (r, u))
    else: ejecutar_sql("INSERT INTO usuarios (username, password, rol) VALUES (%s,%s,%s)", (u, p if p else "1234", r))
    return redirect(url_for('gestion_usuarios'))

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
    return jsonify([dict(r) for r in ejecutar_sql("SELECT * FROM trabajadores WHERE rut LIKE %s OR upper(nombre) LIKE %s LIMIT 5", (f'%{q}%', f'%{q}%'))])

@app.route('/api/prestamos_ticket')
def api_prestamos_ticket():
    tid = request.args.get('ticket_id', '').replace('TICKET:', '').strip().upper()
    res = ejecutar_sql("SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.transaction_id=%s AND p.estado='ACTIVO'", (tid,))
    if not res: return jsonify({'status': 'error', 'msg': 'Ticket no encontrado'})
    return jsonify({'status': 'ok', 'data': [dict(r) for r in res]})

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w = request.args.get('worker_id', '').upper().replace('.', '').strip()
    res = ejecutar_sql("SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.worker_id=%s AND p.estado='ACTIVO' AND p.tipo_item='HERRAMIENTA'", (w,))
    if not res: return jsonify({'status':'empty', 'msg': 'Sin pendientes'})
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    data = request.json; w = data.get('worker_id'); items = data.get('items')
    if not w or not items: return jsonify({'error':'Datos faltantes'})
    tx = str(uuid.uuid4())[:8].upper()
    try:
        for it in items:
            ejecutar_sql("UPDATE productos SET stock = stock - %s WHERE id=%s", (it['cantidad'], it['id']))
            st = 'ACTIVO' if it['tipo'] == 'HERRAMIENTA' else 'CONSUMIDO'
            ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)", (tx, w, it['id'], it['tipo'], it['cantidad'], get_str_now(), st))
        return jsonify({'status':'ok', 'ticket_id': tx})
    except Exception as e: return jsonify({'status':'error', 'msg': str(e)})

@app.route('/procesar_devolucion_compleja', methods=['POST'])
def procesar_devolucion():
    items = request.json.get('items', []); ids_out = []
    for it in items:
        try:
            p = ejecutar_sql('SELECT * FROM prestamos WHERE id=%s', (it['id'],), one=True)
            if not p: continue
            qr = int(it['cantidad'])
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (qr, p['tool_id']))
            if qr < p['cantidad']:
                ejecutar_sql('UPDATE prestamos SET cantidad=%s WHERE id=%s', (p['cantidad']-qr, p['id']))
                ejecutar_sql("INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, fecha_regreso, estado) VALUES (%s,%s,%s,%s,%s,%s,%s,'DEVUELTO')", (p['transaction_id'], p['worker_id'], p['tool_id'], p['tipo_item'], qr, p['fecha_salida'], get_str_now()))
                nid = ejecutar_sql("SELECT id FROM prestamos WHERE worker_id=%s AND estado='DEVUELTO' ORDER BY id DESC LIMIT 1", (p['worker_id'],), one=True)
                ids_out.append(str(nid['id']))
            else:
                ejecutar_sql("UPDATE prestamos SET estado='DEVUELTO', fecha_regreso=%s WHERE id=%s", (get_str_now(), p['id'])); ids_out.append(str(p['id']))
        except: pass
    return jsonify({'status':'ok', 'ids': ",".join(ids_out)})

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    items = ejecutar_sql("SELECT p.cantidad, prod.nombre, p.tipo_item FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.transaction_id=%s", (ticket_id,))
    wid = ejecutar_sql("SELECT worker_id, fecha_salida FROM prestamos WHERE transaction_id=%s LIMIT 1", (ticket_id,), one=True)
    if not items: return "Ticket no encontrado"
    worker = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (wid['worker_id'],), one=True)
    conf = ejecutar_sql("SELECT * FROM config"); config = {r['clave']: r['valor'] for r in conf} if conf else {}
    return render_template('ticket.html', ticket_id=ticket_id, items=items, worker=worker, fecha=wid['fecha_salida'], config=config)

@app.route('/ticket_devolucion')
def ticket_devolucion():
    ids = request.args.get('ids', '').split(',')
    safe = [str(int(x)) for x in ids if x.isdigit()]; holder = ','.join(['%s']*len(safe))
    if not safe: return "Error"
    items = ejecutar_sql(f"SELECT p.cantidad, prod.nombre, p.worker_id, p.fecha_regreso FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.id IN ({holder})", tuple(safe))
    worker = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (items[0]['worker_id'],), one=True)
    conf = ejecutar_sql("SELECT * FROM config"); config = {r['clave']: r['valor'] for r in conf} if conf else {}
    return render_template('ticket_devolucion.html', ids=ids[0], items=items, worker=worker, fecha=items[0]['fecha_regreso'], config=config)

@app.route('/fix_db_final')
def fix_db():
    try: ejecutar_sql("CREATE TABLE IF NOT EXISTS facturas (id SERIAL PRIMARY KEY, numero TEXT, fecha TEXT, usuario TEXT)"); return "BD Reparada"
    except Exception as e: return f"Error: {e}"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
