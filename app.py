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
app.secret_key = os.environ.get('SECRET_KEY', 'clave_secreta_iron_trace_v2')
DB_NAME = "irontrace.db"

# Detectar si estamos en un servidor (Render/Heroku/Railway) con PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Retorna una conexión a la DB adecuada según el entorno."""
    if DATABASE_URL:
        # Modo Servidor (PostgreSQL)
        if not psycopg2:
            raise ImportError("psycopg2 no está instalado. Agrégalo a requirements.txt")
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn, 'POSTGRES'
    else:
        # Modo Local (SQLite)
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        return conn, 'SQLITE'

def ejecutar_sql(sql, params=(), one=False):
    """
    Ejecuta una consulta SQL compatible con ambos motores.
    Traduce automáticamente %s (Postgres) a ? (SQLite) si es necesario.
    """
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Adaptación de sintaxis
        if db_type == 'SQLITE':
            sql = sql.replace('%s', '?')
        
        cursor.execute(sql, params)
        
        if sql.strip().upper().startswith('SELECT'):
            rv = cursor.fetchone() if one else cursor.fetchall()
            # Convertir resultados de Postgres a dict para mantener compatibilidad con Jinja2
            if db_type == 'POSTGRES' and rv:
                if one:
                    rv = dict(rv)
                else:
                    rv = [dict(row) for row in rv]
            return rv
        else:
            conn.commit()
            return cursor.lastrowid
            
    except Exception as e:
        conn.rollback()
        print(f"❌ Error SQL: {e}")
        raise e
    finally:
        conn.close()

# --- SIMULACIÓN CORREO ---
def enviar_ticket_email(correo, ticket_id, trabajador_nombre):
    print(f"📧 [EMAIL SIMULADO] Enviando Ticket #{ticket_id} a {correo} ({trabajador_nombre})... OK.")

# --- RUTAS DE ACCESO (LOGIN/LOGOUT) ---

@app.route('/')
def root():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form['username']
        pwd = request.form['password']
        
        # Consulta segura
        usuario = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s AND password = %s', (user, pwd), one=True)

        if usuario:
            session['user'] = usuario['username']
            session['rol'] = usuario['rol']
            
            if session['rol'] == 'operador':
                return redirect(url_for('panel_operador'))
            else:
                return redirect(url_for('dashboard'))
        else:
            flash('Credenciales incorrectas')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- DASHBOARD (ADMIN Y SUPERVISOR) ---

@app.route('/dashboard')
def dashboard():
    if 'user' not in session or session['rol'] == 'operador':
        return redirect(url_for('login'))
    
    # 1. Datos Generales
    productos = ejecutar_sql('SELECT * FROM productos')
    try:
        historial = ejecutar_sql('SELECT * FROM auditoria ORDER BY id DESC LIMIT 10')
    except:
        historial = [] # Por si la tabla no existe aún
        
    db_source = "PostgreSQL Cloud" if DATABASE_URL else f"Local SQLite: {os.path.abspath(DB_NAME)}"
    
    # 2. Estadísticas Financieras
    stats = {'insumos_hoy': 0, 'prestamos_valor': 0, 'prestamos_activos_qty': 0}
    
    # Dinero Insumos Hoy
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    
    # Nota: Postgres usa 'LIKE' con texto igual, ajustamos para compatibilidad de fecha string
    res_insumos = ejecutar_sql('''
        SELECT SUM(p.cantidad * prod.precio) as total
        FROM prestamos p
        JOIN productos prod ON p.tool_id = prod.id
        WHERE p.tipo_item = 'INSUMO' AND p.fecha_salida LIKE %s
    ''', (f'{fecha_hoy}%',), one=True)
    
    if res_insumos and res_insumos['total']:
        stats['insumos_hoy'] = res_insumos['total']

    # Valor Prestado Activo
    res_prestamos = ejecutar_sql('''
        SELECT SUM(prod.precio) as total, COUNT(*) as qty
        FROM prestamos p
        JOIN productos prod ON p.tool_id = prod.id
        WHERE p.estado = 'ACTIVO'
    ''', one=True)
    
    if res_prestamos:
        stats['prestamos_valor'] = res_prestamos['total'] if res_prestamos['total'] else 0
        stats['prestamos_activos_qty'] = res_prestamos['qty']

    # 3. Tabla "En Uso Ahora"
    en_uso = ejecutar_sql('''
        SELECT p.*, prod.nombre 
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id 
        WHERE p.estado = 'ACTIVO'
        ORDER BY p.fecha_salida DESC
    ''')

    return render_template('dashboard.html', 
                           productos=productos, 
                           historial=historial, 
                           rol=session['rol'],
                           db_status=True,
                           db_path=db_source,
                           stats=stats,
                           en_uso=en_uso)

# --- PANEL OPERADOR ---

@app.route('/operador')
def panel_operador():
    if 'user' not in session: return redirect(url_for('login'))
    return render_template('operador.html')

# --- GESTIÓN DE USUARIOS ---

@app.route('/usuarios')
def gestion_usuarios():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))
    
    if session['rol'] == 'admin':
        usuarios = ejecutar_sql('SELECT * FROM usuarios')
    else:
        usuarios = ejecutar_sql("SELECT * FROM usuarios WHERE rol = 'operador'")
    
    return render_template('users.html', usuarios=usuarios, rol_actual=session['rol'])

@app.route('/usuarios/guardar', methods=['POST'])
def guardar_usuario():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    
    username = request.form['username']
    password = request.form['password']
    rol_nuevo = request.form['rol']
    
    # Validación de Supervisor
    if session['rol'] == 'supervisor' and rol_nuevo != 'operador':
        flash('⛔ Error: Supervisor solo puede crear Operadores.')
        return redirect(url_for('gestion_usuarios'))

    try:
        # Generar HASH seguro si hay contraseña
        pass_hash = generate_password_hash(password) if password else None

        existe = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s', (username,), one=True)
        if existe:
            if password:
                # Actualizamos con el HASH, no el texto plano
                ejecutar_sql('UPDATE usuarios SET password = %s WHERE username = %s', (pass_hash, username))
            flash(f'✅ Usuario {username} actualizado.')
        else:
            if not password:
                flash('❌ Error: Contraseña requerida para nuevos usuarios.')
            else:
                # Guardamos usuario, HASH y rol
                ejecutar_sql('INSERT INTO usuarios VALUES (%s, %s, %s)', (username, pass_hash, rol_nuevo))
                flash(f'✅ Usuario {username} creado.')
    except Exception as e:
        flash(f'❌ Error: {str(e)}')
        
    return redirect(url_for('gestion_usuarios'))
    try:
        existe = ejecutar_sql('SELECT * FROM usuarios WHERE username = %s', (username,), one=True)
        if existe:
            if password:
                ejecutar_sql('UPDATE usuarios SET password = %s WHERE username = %s', (password, username))
            flash(f'✅ Usuario {username} actualizado.')
        else:
            ejecutar_sql('INSERT INTO usuarios VALUES (%s, %s, %s)', (username, password, rol_nuevo))
            flash(f'✅ Usuario {username} creado.')
    except Exception as e:
        flash(f'❌ Error: {str(e)}')
        
    return redirect(url_for('gestion_usuarios'))

# --- GESTIÓN DE TRABAJADORES (INCLUYE LA NUEVA RUTA DE IMPORTACIÓN) ---

@app.route('/trabajadores')
def gestion_trabajadores():
    if 'user' not in session: return redirect(url_for('login'))
    trabajadores = ejecutar_sql('SELECT * FROM trabajadores')
    return render_template('workers.html', trabajadores=trabajadores)

@app.route('/trabajadores/guardar', methods=['POST'])
def guardar_trabajador():
    if 'user' not in session: return redirect(url_for('login'))
    try:
        rut = request.form['rut'].upper().strip()
        # Nota: En Postgres "INSERT OR REPLACE" no existe igual que en SQLite.
        # Usamos una lógica de intentar Insertar y si falla (catch) actualizar, o DELETE+INSERT
        # Para simplificar compatibilidad: Borramos y creamos (Upsert simple)
        ejecutar_sql('DELETE FROM trabajadores WHERE rut = %s', (rut,))
        ejecutar_sql('INSERT INTO trabajadores VALUES (%s, %s, %s, %s, %s)', 
                     (rut, request.form['nombre'], request.form['correo'], request.form['seccion'], request.form['faena']))
        flash('✅ Trabajador guardado correctamente')
    except Exception as e:
        flash(f'❌ Error al guardar trabajador: {e}')
    return redirect(url_for('gestion_trabajadores'))

# --- RUTA NUEVA PARA CARGA MASIVA CSV ---
@app.route('/trabajadores/importar', methods=['POST'])
def importar_trabajadores():
    if 'user' not in session: return redirect(url_for('login'))
    
    if 'archivo_csv' not in request.files:
        flash('❌ No se seleccionó ningún archivo')
        return redirect(url_for('gestion_trabajadores'))
        
    file = request.files['archivo_csv']
    if file.filename == '':
        flash('❌ Nombre de archivo vacío')
        return redirect(url_for('gestion_trabajadores'))

    if file:
        try:
            # Leemos el archivo en memoria y decodificamos
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.reader(stream)
            
            count = 0
            # Formato esperado: RUT, NOMBRE, CORREO, SECCION, FAENA
            for row in csv_input:
                if len(row) >= 2: # Al menos RUT y Nombre
                    rut = row[0].strip().upper()
                    nombre = row[1].strip()
                    correo = row[2].strip() if len(row) > 2 else ""
                    seccion = row[3].strip() if len(row) > 3 else ""
                    faena = row[4].strip() if len(row) > 4 else ""
                    
                    # Evitar cabeceras si existen
                    if "RUT" in rut and "NOMBRE" in nombre.upper():
                        continue
                        
                    # Upsert simple
                    ejecutar_sql('DELETE FROM trabajadores WHERE rut = %s', (rut,))
                    ejecutar_sql('INSERT INTO trabajadores VALUES (%s, %s, %s, %s, %s)', 
                                 (rut, nombre, correo, seccion, faena))
                    count += 1
            
            flash(f'✅ Se importaron {count} trabajadores correctamente.')
        except Exception as e:
            flash(f'❌ Error al procesar CSV: {str(e)}')
            
    return redirect(url_for('gestion_trabajadores'))

# --- APIs (JSON) ---

@app.route('/api/buscar_herramientas')
def api_buscar():
    q = request.args.get('q', '').lower()
    # Postgres usa ILIKE para case-insensitive, SQLite usa LIKE (que es case-insensitive para ASCII)
    # Para máxima compatibilidad, convertimos todo a lower()
    res = ejecutar_sql("SELECT * FROM productos WHERE lower(nombre) LIKE %s OR lower(id) LIKE %s LIMIT 10", ('%'+q+'%', '%'+q+'%'))
    
    lista = []
    for row in res:
        row_dict = dict(row)
        if 'tipo' not in row_dict or not row_dict['tipo']:
            row_dict['tipo'] = 'HERRAMIENTA'
        lista.append(row_dict)
    return jsonify(lista)

@app.route('/api/prestamos_trabajador')
def api_prestamos_worker():
    w = request.args.get('worker_id', '').upper()
    res = ejecutar_sql('''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id 
        WHERE p.worker_id=%s AND p.estado='ACTIVO'
    ''', (w,))
    return jsonify([dict(row) for row in res])

@app.route('/api/prestamos_ticket')
def api_prestamos_ticket():
    t_id = request.args.get('ticket_id', '').upper()
    res = ejecutar_sql('''
        SELECT p.id as prestamo_id, p.tool_id, p.cantidad, p.fecha_salida, prod.nombre, prod.tipo
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id 
        WHERE p.transaction_id=%s AND p.estado='ACTIVO'
    ''', (t_id,))
    return jsonify([dict(row) for row in res])

# --- LÓGICA DE NEGOCIO (SALIDAS Y ENTRADAS) ---

@app.route('/procesar_salida_masiva', methods=['POST'])
def procesar_salida():
    data = request.json
    worker_rut = data.get('worker_id', '').upper()
    items = data.get('items', [])
    
    if not items or not worker_rut:
        return jsonify({'status': 'error', 'msg': 'Faltan datos'})

    # Validar Trabajador
    trabajador = ejecutar_sql('SELECT * FROM trabajadores WHERE rut = %s', (worker_rut,), one=True)
    if not trabajador:
        return jsonify({'status': 'error', 'msg': 'RUT Trabajador no registrado. Regístrelo primero.'})

    transaccion_id = str(uuid.uuid4())[:8].upper()
    
    try:
        # Nota: La transaccion atomica se maneja diferente en la funcion helper, 
        # aqui hacemos llamadas individuales. Para producción robusta, esto debería ir en un bloque único de DB.
        # Por simplicidad del código híbrido, validamos stock primero.
        
        for item in items:
            item_id = item['id']
            cant = int(item['cantidad'])
            prod = ejecutar_sql('SELECT * FROM productos WHERE id = %s', (item_id,), one=True)
            
            if prod and prod['stock'] >= cant:
                ejecutar_sql('UPDATE productos SET stock = stock - %s WHERE id = %s', (cant, item_id))
                
                tipo = prod['tipo'] if prod['tipo'] else 'HERRAMIENTA'
                estado_final = 'ACTIVO' if tipo == 'HERRAMIENTA' else 'CONSUMIDO'
                
                ejecutar_sql('''INSERT INTO prestamos 
                    (transaction_id, worker_id, tool_id, tipo_item, cantidad, fecha_salida, estado) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                    (transaccion_id, worker_rut, item_id, tipo, cant, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), estado_final))
            else:
                return jsonify({'status': 'error', 'msg': f'Stock insuficiente para {item_id}'})
        
        # Enviar Correo
        if trabajador['correo']:
            enviar_ticket_email(trabajador['correo'], transaccion_id, trabajador['nombre'])
            
        return jsonify({'status': 'ok', 'ticket_id': transaccion_id})
        
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/procesar_devolucion', methods=['POST'])
def procesar_devolucion():
    
    # Opción A: Por ID de Préstamo (Desde lista)
    if 'prestamo_id' in request.form:
        p_id = request.form['prestamo_id']
        prestamo = ejecutar_sql('SELECT * FROM prestamos WHERE id = %s', (p_id,), one=True)
        
        if prestamo and prestamo['estado'] == 'ACTIVO':
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id = %s', (prestamo['cantidad'], prestamo['tool_id']))
            ejecutar_sql('UPDATE prestamos SET estado = %s, fecha_regreso = %s WHERE id = %s', 
                         ('DEVUELTO', datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p_id))
            flash(f'✅ Ítem devuelto correctamente.')
    
    # Opción B: Por código directo de herramienta
    elif 'tool_id' in request.form:
        tid = request.form['tool_id'].upper()
        # Postgres requiere LIMIT al final
        p = ejecutar_sql('SELECT * FROM prestamos WHERE tool_id=%s AND estado=%s ORDER BY id DESC LIMIT 1', (tid, 'ACTIVO'), one=True)
        
        if p:
            ejecutar_sql('UPDATE productos SET stock = stock + %s WHERE id = %s', (p['cantidad'], tid))
            ejecutar_sql('UPDATE prestamos SET estado = %s, fecha_regreso = %s WHERE id = %s', 
                         ('DEVUELTO', datetime.now().strftime("%Y-%m-%d %H:%M:%S"), p['id']))
            flash(f'✅ {tid} devuelto.')
        else:
            flash(f'⚠️ No hay préstamos activos para {tid}')

    return redirect(url_for('panel_operador'))

# --- VISTAS AUXILIARES ---

@app.route('/ticket/<ticket_id>')
def ver_ticket(ticket_id):
    movs = ejecutar_sql('''
        SELECT p.*, prod.nombre 
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id 
        WHERE transaction_id = %s
    ''', (ticket_id,))
    
    if not movs: return "Ticket no encontrado"
    
    worker_rut = movs[0]['worker_id']
    trabajador = ejecutar_sql('SELECT * FROM trabajadores WHERE rut = %s', (worker_rut,), one=True)
    worker_data = trabajador if trabajador else {'nombre': 'No registrado', 'rut': worker_rut, 'seccion': '-', 'faena': '-'}
    
    # Configuración Global
    try:
        cfg_rows = ejecutar_sql('SELECT * FROM config')
        config = {row['clave']: row['valor'] for row in cfg_rows}
    except:
        config = {'empresa_nombre': 'IRON TRACE DEFAULT', 'empresa_direccion': '', 'ticket_footer': ''}
    
    return render_template('ticket.html', 
                           ticket_id=ticket_id, 
                           worker=worker_data, 
                           fecha=movs[0]['fecha_salida'], 
                           items=movs,
                           config=config)

@app.route('/reportes', methods=['GET', 'POST'])
def reportes():
    if session.get('rol') not in ['admin', 'supervisor']: return redirect(url_for('login'))

    search_term = request.form.get('search_term', '').strip().upper()
    
    sql = '''
        SELECT p.*, prod.nombre, prod.precio, prod.tipo 
        FROM prestamos p 
        JOIN productos prod ON p.tool_id = prod.id
    '''
    params = []
    
    if search_term:
        sql += ' WHERE p.worker_id LIKE %s OR p.tool_id LIKE %s'
        params.append(f'%{search_term}%')
        params.append(f'%{search_term}%')
    
    sql += ' ORDER BY p.fecha_salida DESC'
    movimientos = ejecutar_sql(sql, tuple(params))
    
    total_insumos = 0
    items_insumos = 0
    for m in movimientos:
        # Handle dict or Row
        tipo = m['tipo'] if 'tipo' in m else 'HERRAMIENTA'
        if tipo == 'INSUMO':
            total_insumos += m['precio'] * m['cantidad']
            items_insumos += m['cantidad']

    return render_template('reportes.html', 
                           movimientos=movimientos, 
                           total_insumos=total_insumos,
                           items_insumos=items_insumos,
                           search_term=search_term)

@app.route('/admin/config', methods=['GET', 'POST'])
def admin_config():
    if session.get('rol') != 'admin': return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        ejecutar_sql('UPDATE config SET valor = %s WHERE clave = %s', (request.form['empresa_nombre'], 'empresa_nombre'))
        ejecutar_sql('UPDATE config SET valor = %s WHERE clave = %s', (request.form['ticket_footer'], 'ticket_footer'))
        ejecutar_sql('UPDATE config SET valor = %s WHERE clave = %s', (request.form['impresora_nombre'], 'impresora_nombre'))
        flash('✅ Configuración actualizada')
        
    cfg_rows = ejecutar_sql('SELECT * FROM config')
    cfg = {row['clave']: row['valor'] for row in cfg_rows}
    return render_template('config_admin.html', config=cfg)

# --- ACCIONES MANUALES ADMIN ---

@app.route('/add', methods=['POST'])
def add():
    if session.get('rol') != 'admin': return "Acceso Denegado"
    try:
        ejecutar_sql('INSERT INTO productos (id, nombre, precio, stock, tipo) VALUES (%s, %s, %s, %s, %s)', 
                     (request.form['id'], request.form['nombre'], request.form['precio'], request.form['stock'], request.form['tipo']))
    except: pass
    return redirect(url_for('dashboard'))

@app.route('/ajuste_inventario', methods=['POST'])
def ajuste():
    if session.get('rol') not in ['admin', 'supervisor']: return "Acceso Denegado"
    ejecutar_sql('UPDATE productos SET stock = %s WHERE id = %s', (request.form['stock_real'], request.form['id']))
    return redirect(url_for('dashboard'))

@app.route('/fix_security_patch')
def fix_security():
    try:
        # 1. Encriptamos las claves por defecto
        pass_admin = generate_password_hash('admin123') # Clave nueva para admin
        pass_super = generate_password_hash('123')      # Clave nueva para super
        pass_oper  = generate_password_hash('123')      # Clave nueva para oper

        # 2. Actualizamos la Base de Datos
        # Admin
        ejecutar_sql("UPDATE usuarios SET password = %s WHERE username = 'admin'", (pass_admin,))
        # Supervisor
        ejecutar_sql("UPDATE usuarios SET password = %s WHERE username = 'super'", (pass_super,))
        # Operador
        ejecutar_sql("UPDATE usuarios SET password = %s WHERE username = 'oper'", (pass_oper,))
        
        return """
        <h1 style='color:green; font-family:sans-serif;'>✅ PARCHE DE SEGURIDAD APLICADO</h1>
        <p>Las contraseñas han sido encriptadas exitosamente.</p>
        <ul>
            <li><b>admin</b>: admin123</li>
            <li><b>super</b>: 123</li>
            <li><b>oper</b>: 123</li>
        </ul>
        <a href='/login'>[ IR AL LOGIN ]</a>
        """
    except Exception as e:
        return f"<h1 style='color:red'>ERROR: {str(e)}</h1>"



if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
