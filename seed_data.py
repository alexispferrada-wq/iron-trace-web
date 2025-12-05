import sqlite3
import random
import os

DB_NAME = "irontrace.db"

# (Tus listas anteriores se mantienen igual...)
tipos = ["Taladro", "Martillo", "Destornillador", "Llave Inglesa", "Sierra Circular", "Esmeril", "Alicate", "Casco Seguridad", "Guantes", "Chaleco Reflectante", "Multímetro", "Soldadora", "Compresor", "Lijadora", "Nivel", "Huincha", "Broca", "Disco Corte", "Arnés", "Zapato Seguridad"]
marcas = ["Makita", "Bosch", "DeWalt", "Stanley", "Ubermann", "Bauker", "3M", "Steelpro", "Fluke", "Indura"]
modelos = ["Pro", "X", "Ultra", "Heavy Duty", "Básico", "Industrial", "V2", "Inalámbrico"]
lista_insumos_nombres = ["Casco Seguridad", "Guantes", "Chaleco Reflectante", "Broca", "Disco Corte", "Arnés", "Zapato Seguridad", "Electrodos", "Mascarilla"]

def poblar_db():
    if os.path.exists(DB_NAME):
        try: os.remove(DB_NAME)
        except: pass

    conn = sqlite3.connect(DB_NAME)
    
    # 1. TABLAS DEL SISTEMA
    conn.execute('CREATE TABLE productos (id TEXT PRIMARY KEY, nombre TEXT, precio INTEGER, stock INTEGER, tipo TEXT)')
    conn.execute('CREATE TABLE usuarios (username TEXT PRIMARY KEY, password TEXT, rol TEXT)')
    conn.execute('CREATE TABLE auditoria (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, usuario TEXT, accion TEXT, detalle TEXT)')
    conn.execute('CREATE TABLE config (clave TEXT PRIMARY KEY, valor TEXT)')
    
    # 2. NUEVA TABLA: TRABAJADORES
    conn.execute('''CREATE TABLE trabajadores (
        rut TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        correo TEXT,
        seccion TEXT,
        faena TEXT
    )''')

    # 3. TABLA PRÉSTAMOS (Vinculada a Trabajadores)
    conn.execute('''CREATE TABLE prestamos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT, worker_id TEXT, tool_id TEXT, tipo_item TEXT, 
        cantidad INTEGER, fecha_salida TEXT, fecha_regreso TEXT, estado TEXT)''')

    # CONFIGURACIÓN DEFAULT
    configs = [
        ('empresa_nombre', 'IRON TRACE CORP'),
        ('empresa_direccion', 'Casa Matriz - Santiago'),
        ('ticket_footer', 'Software irontrace.cl, líderes en inventario'),
        ('impresora_nombre', 'POS-80'),
        ('db_path', os.path.abspath(DB_NAME))
    ]
    conn.executemany("INSERT INTO config VALUES (?, ?)", configs)

    # USUARIOS
    conn.execute("INSERT INTO usuarios VALUES ('admin', 'admin123', 'admin')")
    conn.execute("INSERT INTO usuarios VALUES ('super', 'super123', 'supervisor')")
    conn.execute("INSERT INTO usuarios VALUES ('oper', 'oper123', 'operador')")

    # TRABAJADORES DE PRUEBA
    trabajadores = [
        ('11111111-1', 'Juan Pérez', 'juan@empresa.cl', 'Mantenimiento', 'Norte'),
        ('22222222-2', 'Maria Gonzalez', 'maria@empresa.cl', 'Obras Civiles', 'Centro'),
        ('33333333-3', 'Pedro Tapia', 'pedro@empresa.cl', 'Eléctrica', 'Sur')
    ]
    conn.executemany("INSERT INTO trabajadores VALUES (?, ?, ?, ?, ?)", trabajadores)

    # PRODUCTOS
    for i in range(1, 201):
        t = random.choice(tipos)
        cat = "INSUMO" if t in lista_insumos_nombres else "HERRAMIENTA"
        p = random.randint(1000, 25000) if cat == "INSUMO" else random.randint(15000, 600000)
        s = random.randint(50, 500) if cat == "INSUMO" else random.randint(1, 15)
        n = f"{t} {random.choice(marcas)} {random.choice(modelos)}"
        id_t = f"{t[0].upper()}-{str(i).zfill(3)}"
        try: conn.execute("INSERT INTO productos VALUES (?, ?, ?, ?, ?)", (id_t, n, p, s, cat))
        except: pass

    conn.commit()
    conn.close()
    print("✅ DB Actualizada a v2.0 (Usuarios y Trabajadores).")

if __name__ == "__main__":
    poblar_db()