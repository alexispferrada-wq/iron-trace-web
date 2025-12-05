from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import csv
import io
from datetime import datetime

# --- CONFIGURACIÓN HÍBRIDA (SQLite + PostgreSQL) ---
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'clave_super_secreta_iron_trace_v4')
DB_NAME = "irontrace.db"
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Detecta entorno y devuelve conexión apropiada"""
    if DATABASE_URL:
        # MODO NUBE (Render/Postgres)
        if not psycopg2: raise ImportError("Librería psycopg2 no instalada en servidor.")
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn, 'POSTGRES'
    else:
        # MODO LOCAL (Mac/SQLite)
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn, 'SQLITE'

def ejecutar_sql(sql, params=(), one=False):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    try:
        # Adaptador de sintaxis: SQLite usa ? en vez de %s
        if db_type == 'SQLITE': 
            sql = sql.replace('%s', '?')
        
        cursor.execute(sql, params)
        
        # Si es lectura (SELECT)
        if sql.strip().upper().startswith('SELECT'):
            rv = cursor.fetchone() if one else cursor.fetchall()
            # Normalizar resultados de Postgres a Diccionario real
            if db_type == 'POSTGRES' and rv:
                if one:
                    rv = dict(rv)
                else:
                    rv = [dict(row) for row in rv]
            return rv
        else:
            # Si es escritura (INSERT, UPDATE, DELETE)
            conn.commit()
            return cursor.lastrowid
            
    except Exception as e:
        conn.rollback()
        print(f"❌ ERROR SQL: {e} | Query: {sql}")
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
            usuario = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s', (user,), one=True)
            valid = False
            
            if usuario:
                # 1. Intentar validar Hash
                if usuario['password'].startswith(('scrypt:', 'pbkdf2:')):
                    valid = check_password_hash(usuario['password'], pwd)
                # 2. Intentar validar Texto Plano (Legacy)
                elif usuario['password'] == pwd:
                    valid = True
            
            if valid:
                session['user'] = usuario['username']
                session['rol'] = usuario['rol']
                dest = url_for('panel_operador') if session['rol'] == 'operador' else url_for('dashboard')
                return redirect(dest)
            else:
                flash('⛔ Credenciales incorrectas')
        except Exception as e:
            flash(f'Error de conexión: {e}')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- DASHBOARD PRINCIPAL ---

@app.route('/dashboard')
def dashboard():
    if session.get('rol') == 'operador': return redirect(url_for('panel_operador'))
    if 'user' not in session: return redirect(url_for('login'))
    
    # Inicializar variables seguras
    stats = {'insumos_hoy': 0, 'prestamos_valor': 0, 'prestamos_activos_qty': 0}
    en_uso = []
    
    try:
        # KPIs Financieros
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        res = ejecutar_sql("SELECT SUM(p.cantidad * prod.precio) as total FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.tipo_item = 'INSUMO' AND p.fecha_salida LIKE %s", (f'{fecha_hoy}%',), one=True)
        stats['insumos_hoy'] = res['total'] if res and res.get('total') else 0
        
        res2 = ejecutar_sql("SELECT SUM(prod.precio) as total, COUNT(*) as qty FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO'", one=True)
        if res2:
            stats['prestamos_valor'] = res2['total'] if res2.get('total') else 0
            stats['prestamos_activos_qty'] = res2['qty']

        # Tabla En Uso
        en_uso = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO' ORDER BY p.fecha_salida DESC")
    except Exception as e:
        print(f"Error cargando dashboard: {e}")
        # No crasheamos, solo mostramos vacío

    db_status_text = "CONECTADO"
    conn_type = "PostgreSQL" if DATABASE_URL else "SQLite"

    return render_template('dashboard.html', 
                           stats=stats, 
                           en_uso=en_uso, 
                           rol=session['rol'], 
                           db_status=True, 
                           db_path=conn_type)

# --- GESTIÓN DE USUARIOS ---

@app.route('/usuarios')
def gestion_usuarios():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    
    sql = "SELECT * FROM usuarios ORDER BY rol" if session['rol'] == 'admin' else "SELECT * FROM usuarios WHERE rol = 'operador'"
    usuarios = ejecutar_sql(sql)
    return render_template('users.html', usuarios=usuarios, rol_actual=session['rol'])

@app.route('/usuarios/guardar', methods=['POST'])
def guardar_usuario():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    user = request.form['username']
    pwd = request.form['password']
    rol = request.form['rol']
    
    if session['rol'] == 'supervisor' and rol != 'operador':
        flash('⛔ Supervisor solo crea Operadores.')
        return redirect(url_for('gestion_usuarios'))

    try:
        # Encriptación Segura
        pass_hash = generate_password_hash(pwd) if pwd else None
        
        exists = ejecutar_sql('SELECT * FROM usuarios WHERE username=%s', (user,), one=True)
        if exists:
            if pwd:
                ejecutar_sql('UPDATE usuarios SET password=%s, rol=%s WHERE username=%s', (pass_hash, rol, user))
            else:
                ejecutar_sql('UPDATE usuarios SET rol=%s WHERE username=%s', (rol, user))
            flash(f'✅ Usuario {user} actualizado.')
        else:
            if pwd:
                ejecutar_sql('INSERT INTO usuarios (username, password, rol) VALUES (%s, %s, %s)', (user, pass_hash, rol))
                flash(f'✅ Usuario {user} creado.')
            else:
                flash('⚠️ Contraseña requerida.')
    except Exception as e:
        flash(f'❌ Error: {e}')
        
    return redirect(url_for('gestion_usuarios'))

# --- INVENTARIO Y FACTURAS ---

@app.route('/inventario')
def vista_inventario():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    
    try:
        productos = ejecutar_sql('SELECT * FROM productos ORDER BY id')
        facturas = ejecutar_sql('SELECT * FROM facturas ORDER BY id DESC LIMIT 50')
    except:
        productos = []
        facturas = []
        flash('⚠️ Tablas de inventario no encontradas. Ejecute /fix_db_final')
        
    return render_template('inventario.html', productos=productos, facturas=facturas)

@app.route('/inventario/carga_masiva_productos', methods=['POST'])
def carga_masiva_productos():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    f = request.files.get('archivo_csv')
    if f:
        stream = io.StringIO(f.stream.read().decode("UTF8", errors='ignore'), newline=None)
        reader = csv.reader(stream)
        c = 0
        for row in reader:
            if len(row) >= 4 and 'ID' not in row[0].upper():
                tipo = row[4].strip().upper() if len(row)>4 else 'HERRAMIENTA'
                ejecutar_sql('DELETE FROM productos WHERE id=%s', (row[0].strip(),))
                ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)',
                             (row[0].strip(), row[1].strip(), row[2], row[3], tipo))
                c += 1
        flash(f'✅ {c} productos cargados.')
    return redirect(url_for('vista_inventario'))

@app.route('/inventario/subir_factura', methods=['POST'])
def subir_factura():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    f = request.files.get('archivo_factura')
    doc = request.form.get('num_documento')
    if f:
        try:
            ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)',
                         (doc, datetime.now().strftime("%Y-%m-%d %H:%M"), session['user']))
            
            stream = io.StringIO(f.stream.read().decode("UTF8", errors='ignore'), newline=None)
            reader = csv.reader(stream)
            c = 0
            for row in reader:
                if len(row) >= 2 and 'CODIGO' not in row[0].upper():
                    pid, cant = row[0].strip(), int(row[1])
                    precio = float(row[2]) if len(row) > 2 else None
                    
                    prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
                    if prod:
                        sql = 'UPDATE productos SET stock = stock + %s'
                        params = [cant]
                        if precio: 
                            sql += ', precio = %s'
                            params.append(precio)
                        sql += ' WHERE id = %s'
                        params.append(pid)
                        ejecutar_sql(sql, tuple(params))
                    else:
                        ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)',
                                     (pid, f'NUEVO ({pid})', precio or 0, cant, 'INSUMO'))
                    c += 1
            flash(f'✅ Factura procesada. Stock sumado en {c} ítems.')
        except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('vista_inventario'))

# --- NUEVA RUTA PARA INGRESO MANUAL 1 A 1 ---
@app.route('/inventario/ingreso_manual', methods=['POST'])
def ingreso_manual_stock():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    doc = request.form.get('num_documento', 'MANUAL').upper()
    pid = request.form.get('id_producto').strip()
    cant = int(request.form.get('cantidad', 0))
    precio = request.form.get('precio')
    
    if not pid or cant < 1:
        flash('❌ Datos inválidos.')
        return redirect(url_for('vista_inventario'))

    try:
        # 1. Registrar Cabecera en Historial (para que aparezca en la tabla de abajo)
        ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s, %s, %s)',
                     (f"{doc} (Item: {pid})", datetime.now().strftime("%Y-%m-%d %H:%M"), session['user']))

        # 2. Verificar existencia
        prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
        
        if prod:
            # 3. Actualizar Stock
            sql = 'UPDATE productos SET stock = stock + %s'
            params = [cant]
            if precio and float(precio) > 0:
                sql += ', precio = %s'
                params.append(float(precio))
            sql += ' WHERE id = %s'
            params.append(pid)
            
            ejecutar_sql(sql, tuple(params))
            flash(f'✅ Stock actualizado: +{cant} unidades al producto {pid}.')
        else:
            # 3b. Crear si no existe (Como INSUMO por defecto)
            precio_val = float(precio) if precio else 0
            ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s, %s, %s, %s, %s)',
                         (pid, f'NUEVO MANUAL ({pid})', precio_val, cant, 'INSUMO'))
            flash(f'✅ Producto nuevo creado: {pid} con stock {cant}.')
            
    except Exception as e:
        flash(f'❌ Error: {e}')
        
    return redirect(url_for('vista_inventario'))

# --- CARGA TRABAJADORES ---
@app.route('/trabajadores')
def gestion_trabajadores():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('workers.html', trabajadores=ejecutar_sql('SELECT * FROM trabajadores'))

@app.route('/trabajadores/importar', methods=['POST'])
def importar_trabajadores():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    f = request.files.get('archivo_csv')
    if f:
        try:
            stream = io.StringIO(f.stream.read().decode("UTF8", errors='ignore'), newline=None)
            reader = csv.reader(stream)
            c = 0
            for row in reader:
                if len(row) >= 2 and 'RUT' not in row[0].upper():
                    rut = row[0].strip().upper()
                    ejecutar_sql('DELETE FROM trabajadores WHERE rut=%s', (rut,))
                    ejecutar_sql('INSERT INTO trabajadores VALUES (%s,%s,%s,%s,%s)',
                                 (rut, row[1].strip(), row[2] if len(row)>2 else '', row[3] if len(row)>3 else '', row[4] if len(row)>4 else ''))
                    c += 1
            flash(f'✅ {c} trabajadores importados.')
        except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('gestion_trabajadores'))

# --- CONFIGURACIÓN ADMIN ---
@app.route('/admin/config', methods=['GET', 'POST'])
def admin_config():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        for k in ['empresa_nombre', 'ticket_footer', 'impresora_nombre', 'empresa_direccion']:
            ejecutar_sql('DELETE FROM config WHERE clave=%s', (k,))
            ejecutar_sql('INSERT INTO config (clave, valor) VALUES (%s,%s)', (k, request.form.get(k, '')))
        flash('✅ Configuración guardada.')
        
    try:
        raw = ejecutar_sql('SELECT * FROM config')
        cfg = {r['clave']: r['valor'] for r in raw}
    except: cfg = {}
    
    # Defaults seguros
    final_cfg = {
        'empresa_nombre': cfg.get('empresa_nombre', 'IRON TRACE DEMO'),
        'ticket_footer': cfg.get('ticket_footer', 'Gracias'),
        'impresora_nombre': cfg.get('impresora_nombre', 'POS-80'),
        'empresa_direccion': cfg.get('empresa_direccion', 'Faena'),
        'db_path': 'PostgreSQL' if DATABASE_URL else 'Local'
    }
    return render_template('config_admin.html', config=final_cfg)

# --- OPERADOR ---
@app.route('/operador')
def panel_operador(): return render_template('operador.html')

@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q', '').lower()
    res = ejecutar_sql("SELECT * FROM productos WHERE lower(nombre) LIKE %s OR lower(id) LIKE %s LIMIT 10", (f'%{q}%', f'%{q}%'))
    return jsonify([dict(r) for r in res])

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    d = request.json
    tid = str(uuid.uuid4())[:8].upper()
    try:
        for i in d['items']:
            ejecutar_sql('UPDATE productos SET stock = stock - %s WHERE id=%s', (i['cantidad'], i['id']))
            ejecutar_sql('INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                         (tid, d['worker_id'], i['id'], i['tipo'], i['cantidad'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'ACTIVO'))
        return jsonify({'status':'ok', 'ticket_id': tid})
    except Exception as e: return jsonify({'status':'error', 'msg': str(e)})

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    movs = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE transaction_id=%s", (ticket_id,))
    if not movs: return "Ticket no encontrado"
    w = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (movs[0]['worker_id'],), one=True)
    try: cfg = {r['clave']: r['valor'] for r in ejecutar_sql("SELECT * FROM config")}
    except: cfg = {'empresa_nombre':'Iron', 'ticket_footer':'Check', 'empresa_direccion':''}
    return render_template('ticket.html', ticket_id=ticket_id, worker=w, items=movs, config=cfg, fecha=movs[0]['fecha_salida'])

# --- REPARACIÓN MAESTRA DE BASE DE DATOS ---
@app.route('/fix_db_final')
def fix_db_final():
    conn, db_type = get_db_connection()
    conn.close()
    
    try:
        # 1. Tabla Facturas (Diferente sintaxis SQLITE vs POSTGRES)
        if db_type == 'POSTGRES':
            # Sintaxis Render/Postgres
            sql_facturas = """CREATE TABLE IF NOT EXISTS facturas (
                id SERIAL PRIMARY KEY, 
                numero TEXT, 
                fecha TEXT, 
                usuario TEXT)"""
                
            # Parche Columnas Grandes
            try: ejecutar_sql("ALTER TABLE usuarios ALTER COLUMN password TYPE VARCHAR(255)")
            except: pass
            try: ejecutar_sql("ALTER TABLE trabajadores ALTER COLUMN rut TYPE VARCHAR(50)")
            except: pass
            
        else:
            # Sintaxis Mac/SQLite
            sql_facturas = """CREATE TABLE IF NOT EXISTS facturas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                numero TEXT, 
                fecha TEXT, 
                usuario TEXT)"""

        ejecutar_sql(sql_facturas)
        
        # 2. Asegurar Configuración
        ejecutar_sql("CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT)")
        
        return f"""
        <h1 style='color:green'>✅ REPARACIÓN EXITOSA ({db_type})</h1>
        <p>Tablas creadas y columnas ajustadas.</p>
        <a href='/dashboard'>IR AL DASHBOARD</a>
        """
    except Exception as e:
        return f"<h1 style='color:red'>FALLO CRÍTICO: {e}</h1>"

# --- MÓDULO REPORTES (FALTABA ESTO) ---
@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))

    search_term = request.form.get('search_term', '').strip().upper()
    
    # Consulta robusta con selección explícita de columnas
    sql = '''
        SELECT 
            p.id, 
            p.fecha_salida, 
            p.worker_id, 
            p.tool_id, 
            p.tipo_item as tipo, 
            p.cantidad, 
            p.estado,
            prod.nombre as nombre, 
            prod.precio as precio
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id
    '''
    params = []
    
    if search_term:
        sql += ' WHERE p.worker_id LIKE %s OR p.tool_id LIKE %s'
        params.append(f'%{search_term}%')
        params.append(f'%{search_term}%')
    
    sql += ' ORDER BY p.fecha_salida DESC'
    
    try:
        movimientos = ejecutar_sql(sql, tuple(params))
    except Exception as e:
        print(f"Error Reportes: {e}")
        movimientos = []
    
    total_insumos = 0
    items_insumos = 0
    
    # Procesar totales
    for m in movimientos:
        # Aseguramos que sea diccionario (para compatibilidad Postgres/SQLite)
        reg = dict(m) if isinstance(m, (dict, sqlite3.Row)) else m
        
        tipo = reg.get('tipo', 'HERRAMIENTA')
        if tipo == 'INSUMO':
            precio = reg.get('precio') or 0
            cantidad = reg.get('cantidad') or 0
            total_insumos += precio * cantidad
            items_insumos += cantidad

    return render_template('reportes.html', 
                           movimientos=movimientos, 
                           total_insumos=total_insumos,
                           items_insumos=items_insumos,
                           search_term=search_term)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
