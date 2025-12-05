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
app.secret_key = os.environ.get('SECRET_KEY', 'clave_secreta_iron_trace_v3_enterprise')
DB_NAME = "irontrace.db"
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if DATABASE_URL:
        if not psycopg2: raise ImportError("psycopg2 no instalado.")
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
        print(f"❌ SQL Error: {e}")
        raise e
    finally:
        conn.close()

# --- RUTAS PRINCIPALES ---

@app.route('/')
def root(): return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        usuario = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s', (user,), one=True)
        
        # Validamos Hash o Texto Plano (Legacy)
        valid = False
        if usuario:
            if usuario['password'].startswith('scrypt:') or usuario['password'].startswith('pbkdf2:'):
                valid = check_password_hash(usuario['password'], pwd)
            elif usuario['password'] == pwd:
                valid = True # Legacy fallback
        
        if valid:
            session['user'] = usuario['username']
            session['rol'] = usuario['rol']
            return redirect(url_for('panel_operador') if session['rol'] == 'operador' else url_for('dashboard'))
        else:
            flash('⛔ Credenciales incorrectas')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if session.get('rol') == 'operador': return redirect(url_for('panel_operador'))
    if 'user' not in session: return redirect(url_for('login'))
    
    # 1. KPIs Básicos
    stats = {'insumos_hoy': 0, 'prestamos_valor': 0, 'prestamos_activos_qty': 0}
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    
    try:
        # Gasto Insumos Hoy
        res = ejecutar_sql("SELECT SUM(p.cantidad * prod.precio) as total FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.tipo_item = 'INSUMO' AND p.fecha_salida LIKE %s", (f'{fecha_hoy}%',), one=True)
        stats['insumos_hoy'] = res['total'] if res and res['total'] else 0
        
        # Activos en Préstamo
        res = ejecutar_sql("SELECT SUM(prod.precio) as total, COUNT(*) as qty FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO'", one=True)
        if res:
            stats['prestamos_valor'] = res['total'] if res['total'] else 0
            stats['prestamos_activos_qty'] = res['qty']
    except: pass 

    # 2. Tabla En Uso
    en_uso = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE p.estado = 'ACTIVO' ORDER BY p.fecha_salida DESC")
    
    # 3. Datos de Estado (ESTO FALTABA)
    db_source = "PostgreSQL Nube" if DATABASE_URL else "SQLite Local"
    
    return render_template('dashboard.html', 
                           stats=stats, 
                           en_uso=en_uso, 
                           rol=session['rol'],
                           db_status=True,    # <--- ¡ESTO ENCIENDE EL PUNTO VERDE!
                           db_path=db_source) # <--- Muestra la ruta
# --- MÓDULO GESTIÓN DE USUARIOS (MEJORADO) ---
@app.route('/usuarios')
def gestion_usuarios():
    rol = session.get('rol')
    if rol not in ['admin', 'supervisor']: return redirect(url_for('login'))
    
    # Admin ve todo, Supervisor solo ve Operadores
    if rol == 'admin':
        usuarios = ejecutar_sql('SELECT * FROM usuarios ORDER BY rol')
    else:
        usuarios = ejecutar_sql("SELECT * FROM usuarios WHERE rol = 'operador' ORDER BY username")
        
    return render_template('users.html', usuarios=usuarios, rol_actual=rol)

@app.route('/usuarios/guardar', methods=['POST'])
def guardar_usuario():
    rol_editor = session.get('rol')
    if rol_editor not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    username = request.form['username'].strip()
    password = request.form['password'].strip()
    rol_nuevo = request.form['rol']
    
    # REGLA DE NEGOCIO: Supervisor solo crea Operadores
    if rol_editor == 'supervisor' and rol_nuevo != 'operador':
        flash('⛔ Como Supervisor, solo puedes crear Operadores.')
        return redirect(url_for('gestion_usuarios'))

    try:
        existe = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s', (username,), one=True)
        pass_hash = generate_password_hash(password) if password else None
        
        if existe:
            # Si es supervisor editando, verificar que no edite a un Admin u otro Supervisor
            if rol_editor == 'supervisor' and existe['rol'] != 'operador':
                flash('⛔ No tienes permiso para editar este usuario.')
                return redirect(url_for('gestion_usuarios'))

            if password:
                ejecutar_sql('UPDATE usuarios SET password = %s, rol = %s WHERE username = %s', (pass_hash, rol_nuevo, username))
            else:
                ejecutar_sql('UPDATE usuarios SET rol = %s WHERE username = %s', (rol_nuevo, username))
            flash(f'✅ Usuario {username} actualizado.')
        else:
            if not password:
                flash('⚠️ Contraseña requerida para nuevos usuarios.')
            else:
                ejecutar_sql('INSERT INTO usuarios (username, password, rol) VALUES (%s, %s, %s)', (username, pass_hash, rol_nuevo))
                flash(f'✅ Usuario {username} creado.')
    except Exception as e:
        flash(f'❌ Error: {e}')
        
    return redirect(url_for('gestion_usuarios'))

@app.route('/usuarios/eliminar/<username>')
def eliminar_usuario(username):
    rol_editor = session.get('rol')
    if rol_editor not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    target = ejecutar_sql('SELECT * FROM usuarios WHERE username=%s', (username,), one=True)
    if not target: return redirect(url_for('gestion_usuarios'))
    
    # Reglas de borrado
    if target['username'] == 'admin':
        flash('⛔ No se puede eliminar al Super Admin.')
    elif rol_editor == 'supervisor' and target['rol'] != 'operador':
        flash('⛔ Solo puedes eliminar Operadores.')
    else:
        ejecutar_sql('DELETE FROM usuarios WHERE username=%s', (username,))
        flash(f'🗑️ Usuario {username} eliminado.')
        
    return redirect(url_for('gestion_usuarios'))

# --- MÓDULO INVENTARIO (NUEVO: FACTURAS Y CARGA MASIVA) ---
@app.route('/inventario')
def vista_inventario():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    productos = ejecutar_sql('SELECT * FROM productos ORDER BY id')
    facturas = []
    try:
        facturas = ejecutar_sql('SELECT * FROM facturas ORDER BY id DESC LIMIT 50')
    except: pass # Si tabla no existe aún
    
    return render_template('inventario.html', productos=productos, facturas=facturas)

@app.route('/inventario/carga_masiva_productos', methods=['POST'])
def carga_masiva_productos():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    f = request.files.get('archivo_csv')
    if not f: return redirect(url_for('vista_inventario'))
    
    try:
        stream = io.StringIO(f.stream.read().decode("UTF8", errors='ignore'), newline=None)
        reader = csv.reader(stream)
        count = 0
        # Formato CSV: ID, NOMBRE, PRECIO, STOCK, TIPO
        for row in reader:
            if len(row) >= 4 and 'ID' not in row[0].upper():
                pid, nom, pre, stk = row[0].strip(), row[1].strip(), row[2], row[3]
                tipo = row[4].strip().upper() if len(row) > 4 else 'HERRAMIENTA'
                
                # Upsert (Borrar e insertar para actualizar todo)
                ejecutar_sql('DELETE FROM productos WHERE id=%s', (pid,))
                ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s, %s, %s, %s, %s)',
                             (pid, nom, pre, stk, tipo))
                count += 1
        flash(f'✅ {count} Productos importados/actualizados.')
    except Exception as e:
        flash(f'❌ Error CSV: {e}')
        
    return redirect(url_for('vista_inventario'))

@app.route('/inventario/subir_factura', methods=['POST'])
def subir_factura():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    f = request.files.get('archivo_factura')
    n_doc = request.form.get('num_documento', 'S/N')
    
    if f:
        try:
            stream = io.StringIO(f.stream.read().decode("UTF8", errors='ignore'), newline=None)
            reader = csv.reader(stream)
            count = 0
            
            # Registrar Cabecera Factura
            fid = ejecutar_sql('INSERT INTO facturas (numero, fecha, usuario) VALUES (%s, %s, %s)', 
                         (n_doc, datetime.now().strftime("%Y-%m-%d %H:%M"), session['user']))
            
            # Formato CSV Factura: CODIGO, CANTIDAD, PRECIO_UNITARIO
            for row in reader:
                if len(row) >= 2 and 'CODIGO' not in row[0].upper():
                    pid = row[0].strip()
                    cant = int(row[1])
                    precio_nuevo = float(row[2]) if len(row) > 2 else None
                    
                    # 1. Verificar si existe
                    prod = ejecutar_sql('SELECT * FROM productos WHERE id=%s', (pid,), one=True)
                    
                    if prod:
                        # ACTUALIZAR STOCK (Sumar)
                        sql_upd = 'UPDATE productos SET stock = stock + %s'
                        params = [cant]
                        # Si viene precio, actualizamos costo
                        if precio_nuevo:
                            sql_upd += ', precio = %s'
                            params.append(precio_nuevo)
                        
                        sql_upd += ' WHERE id = %s'
                        params.append(pid)
                        ejecutar_sql(sql_upd, tuple(params))
                        count += 1
                    else:
                        # Si no existe, crearlo como nuevo (con nombre genérico para editar luego)
                        nom_temp = f"NUEVO IMPORTADO ({pid})"
                        ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s, %s, %s, %s, %s)',
                                     (pid, nom_temp, precio_nuevo or 0, cant, 'INSUMO'))
                        count += 1
            
            flash(f'✅ Factura procesada. {count} ítems sumados al stock.')
        except Exception as e:
            flash(f'❌ Error al procesar factura: {e}')
            
    return redirect(url_for('vista_inventario'))

# --- MÓDULO TRABAJADORES (FIX CARGA MASIVA) ---
@app.route('/trabajadores')
def gestion_trabajadores():
    if 'user' not in session: return redirect(url_for('login'))
    trabajadores = ejecutar_sql('SELECT * FROM trabajadores')
    return render_template('workers.html', trabajadores=trabajadores)

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
                    rut = row[0].strip().upper()[:20] # Truncar por seguridad
                    nom = row[1].strip()[:100]
                    mail = row[2].strip()[:100] if len(row)>2 else ''
                    sec = row[3].strip()[:50] if len(row)>3 else ''
                    faena = row[4].strip()[:50] if len(row)>4 else ''
                    
                    ejecutar_sql('DELETE FROM trabajadores WHERE rut=%s', (rut,))
                    ejecutar_sql('INSERT INTO trabajadores VALUES (%s, %s, %s, %s, %s)', (rut, nom, mail, sec, faena))
                    c += 1
            flash(f'✅ {c} Trabajadores cargados exitosamente.')
        except Exception as e: flash(f'❌ Error: {e}')
    return redirect(url_for('gestion_trabajadores'))

# --- OPERADOR Y TICKETS (Sin cambios mayores) ---
@app.route('/operador')
def panel_operador():
    return render_template('operador.html') if 'user' in session else redirect(url_for('login'))

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    data = request.json
    worker = data.get('worker_id')
    items = data.get('items', [])
    t_id = str(uuid.uuid4())[:8].upper()
    
    # Validar existencia trabajador
    w_exist = ejecutar_sql('SELECT * FROM trabajadores WHERE rut=%s', (worker,), one=True)
    if not w_exist: return jsonify({'status':'error', 'msg':'Trabajador no existe'})
    
    try:
        for it in items:
            ejecutar_sql('UPDATE productos SET stock = stock - %s WHERE id=%s', (it['cantidad'], it['id']))
            ejecutar_sql('INSERT INTO prestamos (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                         (t_id, worker, it['id'], it['tipo'], it['cantidad'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'ACTIVO'))
        return jsonify({'status':'ok', 'ticket_id': t_id})
    except Exception as e:
        return jsonify({'status':'error', 'msg': str(e)})

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    movs = ejecutar_sql("SELECT p.*, prod.nombre FROM prestamos p JOIN productos prod ON p.tool_id = prod.id WHERE transaction_id=%s", (ticket_id,))
    if not movs: return "Ticket no encontrado"
    worker = ejecutar_sql("SELECT * FROM trabajadores WHERE rut=%s", (movs[0]['worker_id'],), one=True)
    try: cfg = {r['clave']:r['valor'] for r in ejecutar_sql("SELECT * FROM config")}
    except: cfg = {'empresa_nombre':'IronTrace', 'empresa_direccion':'', 'ticket_footer':''}
    return render_template('ticket.html', ticket_id=ticket_id, worker=worker, items=movs, config=cfg, fecha=movs[0]['fecha_salida'])

# --- MANTENIMIENTO BD ---
@app.route('/fix_db_schema_v3')
def fix_db():
    # 1. Crear tabla Facturas
    ejecutar_sql("""CREATE TABLE IF NOT EXISTS facturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        numero TEXT, 
        fecha TEXT, 
        usuario TEXT)""")
    
    # 2. Agrandar columnas (Parche Postgres)
    if 'POSTGRES' in str(get_db_connection()[1]):
        try: ejecutar_sql("ALTER TABLE trabajadores ALTER COLUMN rut TYPE VARCHAR(50)")
        except: pass
        try: ejecutar_sql("ALTER TABLE trabajadores ALTER COLUMN nombre TYPE VARCHAR(255)")
        except: pass
        try: ejecutar_sql("ALTER TABLE usuarios ALTER COLUMN password TYPE VARCHAR(255)")
        except: pass

    return "<h1>✅ Base de Datos Actualizada: Tabla Facturas creada y columnas expandidas.</h1><a href='/dashboard'>Volver</a>"
# --- RUTA DE EMERGENCIA (RESET ADMIN) ---
@app.route('/reset_admin_force')
def reset_force():
    try:
        # 1. Borramos al admin si existe para recrearlo limpio
        ejecutar_sql("DELETE FROM usuarios WHERE username = 'admin'")
        
        # 2. Lo creamos de nuevo con clave '1234' (TEXTO PLANO, SIN ENCRIPTAR)
        # Esto asegura que entre por la lógica "Legacy" del login
        ejecutar_sql("INSERT INTO usuarios (username, password, rol) VALUES ('admin', '1234', 'admin')")
        
        return """
        <div style='text-align:center; font-family:sans-serif; margin-top:50px;'>
            <h1 style='color:green;'>✅ ACCESO RESTAURADO</h1>
            <p>El usuario <b>admin</b> ha sido reiniciado.</p>
            <hr>
            <h3>Nueva Clave Temporal: 1234</h3>
            <br>
            <a href='/login' style='background:#333; color:white; padding:10px 20px; text-decoration:none; border-radius:5px;'>IR AL LOGIN >></a>
        </div>
        """
    except Exception as e:
        return f"<h1>Error: {e}</h1>"

@app.route('/admin/config', methods=['GET', 'POST'])
def admin_config():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Guardar o Actualizar (Upsert manual)
        campos = ['empresa_nombre', 'ticket_footer', 'impresora_nombre', 'empresa_direccion']
        for campo in campos:
            val = request.form.get(campo, '')
            # Intentamos actualizar, si no afecta filas (no existe), insertamos
            ejecutar_sql('DELETE FROM config WHERE clave = %s', (campo,))
            ejecutar_sql('INSERT INTO config (clave, valor) VALUES (%s, %s)', (campo, val))
            
        flash('✅ Configuración actualizada')
        
    # Cargar configuración con valores por defecto (Anti-Caídas)
    cfg_rows = ejecutar_sql('SELECT * FROM config')
    cfg_db = {row['clave']: row['valor'] for row in cfg_rows}
    
    cfg = {
        'empresa_nombre': cfg_db.get('empresa_nombre', 'IRON TRACE CORP'),
        'ticket_footer': cfg_db.get('ticket_footer', 'Gracias por su trabajo seguro'),
        'impresora_nombre': cfg_db.get('impresora_nombre', 'POS-80'),
        'empresa_direccion': cfg_db.get('empresa_direccion', 'Faena Minera'),
        'db_path': 'PostgreSQL' if DATABASE_URL else 'Local'
    }
    
    return render_template('config_admin.html', config=cfg)
        
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
