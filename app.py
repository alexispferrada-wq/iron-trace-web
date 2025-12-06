from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import platform
from datetime import datetime

# --- CONFIGURACIÓN HÍBRIDA (SQLite Local / Postgres Nube) ---
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
    """Establece la conexión según el entorno (Local o Nube)"""
    if DATABASE_URL:
        if not psycopg2: raise ImportError("Falta psycopg2 para modo nube")
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn, 'POSTGRES'
    else:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn, 'SQLITE'

def ejecutar_sql(sql, params=(), one=False):
    """Función auxiliar para ejecutar SQL de forma segura y agnóstica"""
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    try:
        # Adaptar placeholder según motor (%s para PG, ? para SQLite)
        if db_type == 'SQLITE': 
            sql = sql.replace('%s', '?')
        
        cursor.execute(sql, params)
        
        if sql.strip().upper().startswith('SELECT'):
            rv = cursor.fetchone() if one else cursor.fetchall()
            # Convertir resultados de Postgres a diccionarios si es necesario
            if db_type == 'POSTGRES' and rv:
                if one: rv = dict(rv)
                else: rv = [dict(row) for row in rv]
            # En SQLite gracias a row_factory ya se comportan como dicts
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

# --- RUTAS DE ACCESO ---

@app.route('/')
def root(): 
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        try:
            u = ejecutar_sql('SELECT * FROM usuarios WHERE username=%s', (user,), one=True)
            valid = False
            if u:
                # Soporte dual: Hashes nuevos o texto plano antiguo
                if u['password'].startswith(('scrypt:', 'pbkdf2:')): 
                    valid = check_password_hash(u['password'], pwd)
                elif u['password'] == pwd: 
                    valid = True
            
            if valid:
                session['user'] = u['username']
                session['rol'] = u['rol']
                # Redirección inteligente según rol
                if u['rol'] == 'operador':
                    return redirect(url_for('panel_operador'))
                else:
                    return redirect(url_for('dashboard'))
            else: 
                flash('⛔ Usuario o contraseña incorrectos')
        except Exception as e: 
            flash(f'Error de sistema: {e}')
            
    return render_template('login.html')

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('login'))

# --- DASHBOARD PRINCIPAL ---
@app.route('/dashboard')
def dashboard():
    if 'user' not in session: return redirect(url_for('login'))
    if session.get('rol') == 'operador': return redirect(url_for('panel_operador'))
    
    stats = {'insumos_hoy':0, 'prestamos_valor':0, 'prestamos_qty':0}
    en_uso = []
    
    try:
        hoy = datetime.now().strftime("%Y-%m-%d")
        
        # 1. Gasto en Insumos hoy
        res = ejecutar_sql("""
            SELECT SUM(p.cantidad * prod.precio) as t 
            FROM prestamos p 
            JOIN productos prod ON p.tool_id=prod.id 
            WHERE p.tipo_item='INSUMO' AND p.fecha_salida LIKE %s
        """, (f'{hoy}%',), one=True)
        stats['insumos_hoy'] = res['t'] if res and res['t'] else 0
        
        # 2. Valor y Cantidad de Herramientas en Terreno
        res2 = ejecutar_sql("""
            SELECT SUM(prod.precio) as t, COUNT(*) as c 
            FROM prestamos p 
            JOIN productos prod ON p.tool_id=prod.id 
            WHERE p.estado='ACTIVO'
        """, one=True)
        if res2: 
            stats['prestamos_valor'] = res2['t'] or 0
            stats['prestamos_qty'] = res2['c']
        
        # 3. Tabla de Préstamos Activos
        en_uso = ejecutar_sql("""
            SELECT p.*, prod.nombre 
            FROM prestamos p 
            JOIN productos prod ON p.tool_id=prod.id 
            WHERE p.estado='ACTIVO' 
            ORDER BY p.fecha_salida DESC
        """)
    except: pass

    # Datos del servidor para el widget de config
    server_info = {
        'time_server': datetime.now().strftime("%H:%M:%S"),
        'timezone': 'Local',
        'db_mode': 'PostgreSQL' if DATABASE_URL else 'SQLite Local',
        'os': platform.system(),
        'node': platform.node(),
        'python': platform.python_version(),
        'app_path': os.getcwd()
    }
    
    # Cargar config para mostrar nombre empresa
    config_raw = ejecutar_sql("SELECT * FROM config")
    config = {row['clave']: row['valor'] for row in config_raw} if config_raw else {}

    return render_template('dashboard.html', stats=stats, en_uso=en_uso, rol=session['rol'], server=server_info, config=config)

# --- GESTIÓN DE INVENTARIO ---
@app.route('/inventario')
def vista_inventario():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    try:
        productos = ejecutar_sql('SELECT * FROM productos ORDER BY id')
        # Verificar si la tabla facturas existe antes de consultar
        try:
            facturas = ejecutar_sql('SELECT * FROM facturas ORDER BY id DESC LIMIT 50')
        except:
            facturas = [] # Si la tabla no existe, lista vacía
    except: productos, facturas = [], []
    return render_template('inventario.html', productos=productos, facturas=facturas)

@app.route('/inventario/ingreso_manual', methods=['POST'])
def ingreso_manual_stock():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    try:
        pid = request.form['id_producto'].strip()
        cant = int(request.form['cantidad'])
        doc = request.form.get('num_documento', 'MANUAL')
        precio_nuevo = request.form.get('precio')
        
        # Guardar historial (si existe la tabla)
        try:
            ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s,%s,%s)', 
                         (f"{doc} ({pid})", datetime.now().strftime("%Y-%m-%d %H:%M"), session['user']))
        except: pass # Ignorar si la tabla facturas no está creada aún
        
        # Lógica de Producto
        prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
        if prod:
            # Actualizar stock
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (cant, pid))
            # Actualizar precio si se ingresó uno nuevo
            if precio_nuevo and int(precio_nuevo) > 0:
                ejecutar_sql('UPDATE productos SET precio = %s WHERE id=%s', (precio_nuevo, pid))
        else:
            # Crear producto nuevo (asumimos insumo si no existe)
            precio = int(precio_nuevo) if precio_nuevo else 0
            ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s,%s,%s,%s,%s)', 
                         (pid, f'NUEVO {pid}', precio, cant, 'INSUMO'))
            
        flash(f'✅ Stock actualizado: +{cant} para {pid}')
    except Exception as e: flash(f'Error: {e}')
    return redirect(url_for('vista_inventario'))

# --- PANEL OPERADOR Y API ---
@app.route('/operador')
def panel_operador(): 
    return render_template('operador.html') if 'user' in session else redirect(url_for('login'))

@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q','').lower()
    # Busca por ID o Nombre (Límite 10 para autocompletar rápido)
    sql = "SELECT * FROM productos WHERE lower(nombre) LIKE %s OR lower(id) LIKE %s LIMIT 10"
    return jsonify([dict(r) for r in ejecutar_sql(sql, (f'%{q}%', f'%{q}%'))])

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    """Procesa el préstamo o entrega de insumos"""
    data = request.json
    worker_id = data.get('worker_id')
    items = data.get('items', [])
    
    if not worker_id or not items:
        return jsonify({'status':'error', 'msg': 'Datos incompletos'})
        
    transaction_id = str(uuid.uuid4())[:8].upper() # ID único de transacción
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        for item in items:
            # item = {id, nombre, tipo, cantidad, ...}
            
            # 1. Descontar Stock
            ejecutar_sql("UPDATE productos SET stock = stock - %s WHERE id=%s", (item['cantidad'], item['id']))
            
            # 2. Registrar Movimiento (Préstamo)
            estado = 'ACTIVO' if item['tipo'] == 'HERRAMIENTA' else 'CONSUMIDO'
            
            ejecutar_sql("""
                INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (transaction_id, worker_id, item['id'], item['tipo'], item['cantidad'], fecha, estado))
            
        return jsonify({'status':'ok', 'ticket_id': transaction_id})
        
    except Exception as e:
        return jsonify({'status':'error', 'msg': str(e)})

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w = request.args.get('worker_id', '').upper().replace('.', '').strip()
    sql = '''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo 
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id 
        WHERE p.worker_id=%s AND p.estado='ACTIVO' AND p.tipo_item='HERRAMIENTA'
    '''
    res = ejecutar_sql(sql, (w,))
    if not res: return jsonify({'status':'empty', 'msg': f'Sin devoluciones pendientes para {w}'})
    return jsonify({'status':'ok', 'data': [dict(r) for r in res]})

@app.route('/procesar_devolucion_compleja', methods=['POST'])
def procesar_devolucion():
    items = request.json.get('items', [])
    ids_out = []
    
    for it in items:
        try:
            # it = {id: prestamo_id, cantidad: qty_a_devolver}
            p = ejecutar_sql('SELECT * FROM prestamos WHERE id=%s', (it['id'],), one=True)
            if not p: continue
            
            qty_ret = int(it['cantidad'])
            
            # 1. Devolver Stock
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id=%s', (qty_ret, p['tool_id']))
            
            # 2. Actualizar Préstamo
            if qty_ret < p['cantidad']: 
                # Devolución Parcial: Restamos lo devuelto al original y creamos un registro de devolución cerrado
                ejecutar_sql('UPDATE prestamos SET cantidad=%s WHERE id=%s', (p['cantidad']-qty_ret, p['id']))
                
                # Registro histórico de lo que se devolvió
                ejecutar_sql("""
                    INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, fecha_regreso, estado) 
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'DEVUELTO')
                """, (p['transaction_id'], p['worker_id'], p['tool_id'], p['tipo_item'], qty_ret, p['fecha_salida'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                
                # Obtenemos el ID de este nuevo registro para el ticket
                nid = ejecutar_sql("SELECT id FROM prestamos WHERE worker_id=%s AND estado='DEVUELTO' ORDER BY id DESC LIMIT 1", (p['worker_id'],), one=True)
                ids_out.append(str(nid['id']))
                
            else: 
                # Devolución Total
                ejecutar_sql("UPDATE prestamos SET estado='DEVUELTO', fecha_regreso=%s WHERE id=%s", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p['id']))
                ids_out.append(str(p['id']))
                
        except Exception as e: print(f"Error dev: {e}")
        
    return jsonify({'status':'ok', 'ids': ",".join(ids_out)})

# --- REPORTES Y USUARIOS ---
@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    term = request.form.get('search_term', '').strip().upper()
    sql = "SELECT p.*, prod.nombre, prod.precio FROM prestamos p JOIN productos prod ON p.tool_id=prod.id"
    if term: sql += f" WHERE p.worker_id LIKE '%{term}%' OR p.tool_id LIKE '%{term}%'"
    sql += " ORDER BY p.fecha_salida DESC LIMIT 100" # Limitar para rendimiento
    
    movs = ejecutar_sql(sql)
    # Calcular total insumos en la vista actual
    total_insu = sum([(m['precio'] or 0)*m['cantidad'] for m in movs if m.get('tipo_item')=='INSUMO'])
    
    return render_template('reportes.html', movimientos=movs, total_insumos=total_insu, search_term=term)

@app.route('/usuarios')
def gestion_usuarios():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    sql = "SELECT * FROM usuarios"
    return render_template('users.html', usuarios=ejecutar_sql(sql), rol_actual=session['rol'])

@app.route('/usuarios/guardar', methods=['POST'])
def guardar_usuario():
    if session.get('rol') != 'admin': return "Acceso Denegado"
    u = request.form['username']
    p = request.form['password']
    r = request.form['rol']
    
    # Check si existe
    existe = ejecutar_sql("SELECT * FROM usuarios WHERE username=%s", (u,), one=True)
    if existe:
        if p: # Si puso pass, actualizar
             # Idealmente usar hash aquí: p = generate_password_hash(p)
             ejecutar_sql("UPDATE usuarios SET password=%s, rol=%s WHERE username=%s", (p, r, u))
        else: # Solo rol
             ejecutar_sql("UPDATE usuarios SET rol=%s WHERE username=%s", (r, u))
        flash('Usuario actualizado')
    else:
        # Crear nuevo
        if not p: p = "1234" # Pass default
        ejecutar_sql("INSERT INTO usuarios (username, password, rol) VALUES (%s,%s,%s)", (u, p, r))
        flash('Usuario creado')
        
    return redirect(url_for('gestion_usuarios'))

# --- MANTENIMIENTO BD ---
@app.route('/fix_db_final')
def fix_db():
    try:
        # Asegurar tablas críticas
        ejecutar_sql("CREATE TABLE IF NOT EXISTS facturas (id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT, fecha TEXT, usuario TEXT)")
        return "BD Reparada / Tablas verificadas"
    except Exception as e: return f"Error: {e}"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
