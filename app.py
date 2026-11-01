import urllib.parse
from contextlib import contextmanager
from decimal import Decimal as D, ROUND_CEILING, ROUND_HALF_UP

import pandas as pd
import psycopg2
import streamlit as st

import os
from zoneinfo import ZoneInfo
import hmac

from psycopg2 import pool as pg_pool

def secreto(nombre, defecto=""):
    """Lee de .streamlit/secrets.toml (o de la plataforma en la nube)."""
    try:
        if nombre in st.secrets:
            return str(st.secrets[nombre])
    except Exception:
        pass
    return os.getenv(nombre, defecto)

import logging
log = logging.getLogger("mix")


def mostrar_error(e):
    log.exception("Error en la app")                     # queda en la terminal
    if secreto("DEBUG", "false").lower() == "true":      # en pantalla solo si tú lo activas
        st.code(str(e))

PASO = 10                                  # gramos por toque en + / −
MIN_G = 10                                 # mínimo por producto (si lo incluye); 0 = no incluirlo
MONEDA = secreto("MONEDA", "S/")
ADMIN_CLAVE = secreto("ADMIN_CLAVE")
MOSTRAR_SUBTOTAL = secreto("MOSTRAR_SUBTOTAL", "true").lower() != "false"   # subtotal de cada producto elegido
WHATSAPP = "".join(c for c in (secreto("WHATSAPP_NUMERO") or "") if c.isdigit())
ZONA = secreto("ZONA_HORARIA", "UTC")


ESTADOS = ["pendiente", "preparando", "entregado", "cancelado"]

st.set_page_config(page_title="Mi Mix", page_icon="🥜")
st.markdown("""
<style>
div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; align-items: center; }
div[data-testid="stColumn"], div[data-testid="column"] { min-width: 0 !important; }
.stButton > button { min-height: 46px; font-size: 1.2rem; }
div[data-testid="stNumberInput"] input { text-align: center; font-weight: 700; font-size: 1.2rem; min-height: 46px; padding-left: 4px; padding-right: 4px; min-width: 0; }
div[data-testid="stNumberInput"] input { text-align: center; font-weight: 700; font-size: 1.15rem; min-height: 46px; padding-left: 2px; padding-right: 16px; min-width: 0; }
div[data-testid="stNumberInputContainer"] { position: relative; }
div[data-testid="stNumberInputContainer"]::after { content: "g"; position: absolute; right: 8px; top: 50%; transform: translateY(-50%); font-weight: 600; color: #6b7466; pointer-events: none; }
.pn { font-weight: 700; font-size: 1rem; line-height: 1.2; }
.pp { color: #b8b8b8; font-size: .8rem; line-height: 1.3; }
.pp b { color: #7B4A2B; }
[class*="st-key-sel_"] { background: #858d5e; border-radius: 14px; }
[class*="st-key-sel_"] > div, [class*="st-key-card_"] > div { padding: 0; }
.nutri th { white-space: nowrap; }
.nutri td:not(:first-child), .nutri th:not(:first-child) { padding-left: .6rem; }
[data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"], div[data-testid="stNumberInput"] button { display: none !important; }
</style>
""", unsafe_allow_html=True)


# ================= Base de datos =================
@st.cache_resource(show_spinner=False)
def pool_conexiones():
    url = secreto("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta DATABASE_URL en los secretos.")
    return pg_pool.ThreadedConnectionPool(
        1, 10, url, client_encoding="UTF8",
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def conexion_viva():
    """Reutiliza una conexión abierta; si Neon la cerró, la reemplaza."""
    p = pool_conexiones()
    for _ in range(3):
        conn = p.getconn()
        try:
            conn.cursor().execute("SELECT 1")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            p.putconn(conn, close=True)
    raise RuntimeError("No se pudo conectar con la base de datos.")


@contextmanager
def transaccion(escribe=False):
    conn = conexion_viva()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
        if escribe:
            st.cache_data.clear()      # si cambió algo, el caché se renueva
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool_conexiones().putconn(conn, close=bool(conn.closed))


def consultar(sql, params=None):
    with transaccion() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def ejecutar(sql, params=None):
    with transaccion(escribe=True) as cur:
        cur.execute(sql, params)


def sub_linea(gramos, precio_kg):
    """Subtotal de un producto, redondeado al centavo igual que al guardar el pedido (así pantalla y pedido coinciden)."""
    return dinero(D(gramos) * D(str(precio_kg)) / 1000)


def dinero(x):
    return D(x).quantize(D("0.01"), ROUND_HALF_UP)


def hora(d):  # fecha del servidor -> tu zona horaria
    return d.astimezone(ZoneInfo(ZONA)).strftime("%d/%m/%Y %H:%M")


# ================= KARDEX (el corazón del sistema) =================
def mover(cur, item, iid, cant, motivo, costo=None, pedido_id=None, nota=None, nombre="el producto"):
    """Único lugar donde cambia el stock. Actualiza saldo, costo promedio y escribe el kardex.
    item: 'producto' (cantidad en gramos) o 'empaque' (unidades). cant con signo.
    costo: por gramo o por unidad (solo en entradas). Devuelve el costo unitario usado."""
    if item == "producto":
        cur.execute("SELECT stock_g, costo_kg/1000 FROM productos WHERE id=%s FOR UPDATE", (iid,))
    else:
        cur.execute("SELECT stock, costo FROM empaques WHERE id=%s FOR UPDATE", (iid,))
    saldo, prom = cur.fetchone()
    prom = D(prom)
    nuevo = saldo + cant
    if nuevo < 0:
        raise ValueError(f"No hay suficiente stock de {nombre}.")
    if cant > 0 and costo is not None:               # entrada con costo: promedio ponderado
        c_mov = D(costo)
        prom_nuevo = (D(saldo) * prom + D(cant) * c_mov) / D(nuevo)
    else:                                            # salidas y ajustes: al costo promedio
        c_mov, prom_nuevo = prom, prom
    if item == "producto":
        cur.execute("UPDATE productos SET stock_g=%s, costo_kg=%s WHERE id=%s", (nuevo, prom_nuevo * 1000, iid))
    else:
        cur.execute("UPDATE empaques SET stock=%s, costo=%s WHERE id=%s", (nuevo, prom_nuevo, iid))
    cur.execute(
        "INSERT INTO kardex (producto_id, empaque_id, motivo, cantidad, saldo, costo_unitario, "
        "costo_promedio, pedido_id, nota) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (iid if item == "producto" else None, iid if item == "empaque" else None,
         motivo, cant, nuevo, c_mov, prom_nuevo, pedido_id, nota))
    return c_mov


def crear_pedido(nombre, tel, tope, tipo, mix):
    if not mix:
        raise ValueError("Agrega al menos un producto.")
    if sum(mix.values()) > tope:
        raise ValueError(f"Tu mix supera el máximo de {tope} g.")
    if any(g < MIN_G for g in mix.values()):
        raise ValueError(f"Cada producto debe llevar al menos {MIN_G} g.")
    with transaccion(escribe=True) as cur:
        cfg = leer_config_precios(cur)
        cur.execute("SELECT id, precio FROM empaques WHERE tipo=%s AND gramos=%s AND activo", (tipo, tope))
        emp = cur.fetchone()
        if not emp:
            raise ValueError("Ese empaque ya no está disponible.")
        precio_emp = D(emp[1])                      # el empaque se cobra a su precio, sin margen automático
        cur.execute(
            "INSERT INTO pedidos (cliente_nombre, cliente_telefono, presentacion_g, total_g, total, "
            "empaque_id, empaque_tipo, precio_empaque) VALUES (%s,%s,%s,%s,0,%s,%s,%s) RETURNING id",
            (nombre.strip(), tel.strip(), tope, sum(mix.values()), emp[0], tipo, precio_emp))
        pedido_id = cur.fetchone()[0]
        cargo = D(str(cfg["cargo"]))                  # cargo por armado: es ganancia, no tiene costo
        total, costo, lineas = precio_emp + cargo, D(0), []
        for pid in sorted(mix):
            g = mix[pid]
            cur.execute("SELECT nombre, precio_kg, costo_kg, margen, activo FROM productos WHERE id=%s", (pid,))
            nom, pkg, ckg, mprod, activo = cur.fetchone()
            pkg = precio_venta(ckg, pkg, cfg["auto"], margen_efectivo(mprod, tope, cfg))
            if not activo:
                raise ValueError(f"{nom} ya no está disponible.")
            c = mover(cur, "producto", pid, -g, "venta", pedido_id=pedido_id, nombre=nom)
            sub = dinero(D(g) * pkg / 1000)
            cur.execute("INSERT INTO pedido_items (pedido_id, producto_id, nombre, gramos, precio_kg, "
                        "subtotal, costo_g) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (pedido_id, pid, nom, g, pkg, sub, c))
            total += sub
            costo += D(g) * c
            lineas.append((nom, g, sub))
        c_emp = mover(cur, "empaque", emp[0], -1, "venta", pedido_id=pedido_id, nombre=f"frascos {tipo}")
        costo += c_emp
        cur.execute("UPDATE pedidos SET total=%s, costo_total=%s, costo_empaque=%s, cargo_armado=%s WHERE id=%s",
                    (total, costo, c_emp, cargo, pedido_id))
    return {"id": pedido_id, "total": total, "gramos": sum(mix.values()), "lineas": lineas,
            "empaque": tipo, "precio_emp": precio_emp, "cargo": cargo, "nombre": nombre.strip()}


def cancelar_pedido(pid):
    with transaccion(escribe=True) as cur:
        cur.execute("SELECT estado, empaque_id, costo_empaque FROM pedidos WHERE id=%s FOR UPDATE", (pid,))
        estado, emp_id, c_emp = cur.fetchone()
        if estado == "cancelado":
            return
        cur.execute("SELECT producto_id, gramos, costo_g, nombre FROM pedido_items WHERE pedido_id=%s", (pid,))
        for prod, g, c, nom in cur.fetchall():
            mover(cur, "producto", prod, g, "devolucion", costo=c, pedido_id=pid,
                  nota="Pedido cancelado", nombre=nom)
        if emp_id:
            mover(cur, "empaque", emp_id, 1, "devolucion", costo=c_emp, pedido_id=pid, nota="Pedido cancelado")
        cur.execute("UPDATE pedidos SET estado='cancelado' WHERE id=%s", (pid,))


# ================= Estado del mix =================
st.session_state.setdefault("mix", {})
st.session_state.setdefault("receta_items", None)   # receta aplicada: [(producto_id, gramos por 1000 g)]
st.session_state.setdefault("stock_de", {})         # {producto_id: stock en gramos}


def total_usado():
    return sum(st.session_state.mix.values())


def escalar_receta(items, tope, stock_de):
    """Reparte el tope entre los productos de la receta, en pasos de PASO g y llenando el tope."""
    total = sum(g for _, g in items)
    exactos = {pid: tope * g / total for pid, g in items}
    mix = {pid: int(x // PASO) * PASO for pid, x in exactos.items()}
    resto = tope - sum(mix.values())
    orden = sorted(exactos, key=lambda p: exactos[p] - mix[p], reverse=True)
    i = 0
    while resto >= PASO and orden:                      # reparte lo que sobró
        mix[orden[i % len(orden)]] += PASO
        resto -= PASO
        i += 1
    ajustado = {pid: min(g, stock_de.get(pid, 0)) for pid, g in mix.items()}
    return {pid: g for pid, g in ajustado.items() if g > 0}


def aplicar_receta(items):
    st.session_state.receta_items = items
    st.session_state.mix = escalar_receta(items, st.session_state.tope, st.session_state.stock_de)


def cambiar_tamano():
    if st.session_state.receta_items:                   # si eligió un mix listo, se reescala
        aplicar_receta(st.session_state.receta_items)
    elif total_usado() > st.session_state.tope:
        st.session_state.mix = {}
        st.toast("Cambiaste el tamaño: arma tu mix de nuevo.")


def vaciar():
    st.session_state.mix = {}
    st.session_state.receta_items = None


def margen(pid, stock):
    """Gramos que todavía se pueden agregar a este producto (tope y stock)."""
    actual = st.session_state.mix.get(pid, 0)
    return max(0, min(st.session_state.tope - total_usado(), stock - actual))


def fijar_producto(pid, valor):
    st.session_state.receta_items = None
    if valor <= 0:
        st.session_state.mix.pop(pid, None)
    else:
        st.session_state.mix[pid] = valor


def sumar(pid, stock):
    actual = st.session_state.mix.get(pid, 0)
    extra = min(PASO, margen(pid, stock))
    if extra > 0 and actual + extra >= MIN_G:
        fijar_producto(pid, actual + extra)


def restar(pid):
    actual = st.session_state.mix.get(pid, 0)
    nuevo = actual - PASO
    if nuevo < MIN_G:
        nuevo = MIN_G if actual > MIN_G else 0
    fijar_producto(pid, nuevo)


def escribir(pid, stock):
    """El cliente escribió los gramos: se aceptan los que quiera, entre MIN_G y lo que quepa."""
    pedido = int(st.session_state.get(f"g{pid}") or 0)
    actual = st.session_state.mix.get(pid, 0)
    libre = min(st.session_state.tope - (total_usado() - actual), stock)   # máximo para este producto
    if pedido <= 0:
        valor = 0
    elif libre < MIN_G:
        valor = 0
        st.session_state.aviso = "Ya no queda espacio en tu mix para este producto."
    elif pedido < MIN_G:
        valor = MIN_G
        st.session_state.aviso = f"El mínimo por producto es {MIN_G} g."
    elif pedido > libre:
        valor = libre
        st.session_state.aviso = f"Máximo para este producto: {libre} g."
    else:
        valor = pedido
    fijar_producto(pid, valor)


# ================= Información nutricional =================
PORCION_G = 30     # tamaño de porción de la etiqueta (g)

NUTRI_COLS = ["kcal_100g", "proteinas_100g", "grasas_100g", "grasas_sat_100g", "grasas_trans_100g",
              "colesterol_100g", "carbohidratos_100g", "azucares_100g", "fibra_100g", "sodio_100g"]
NUTRI_ETQ = {
    "kcal_100g": "Energía (kCal)", "proteinas_100g": "Proteínas (g)", "grasas_100g": "Grasas totales (g)",
    "grasas_sat_100g": "Grasas saturadas (g)", "grasas_trans_100g": "Grasas trans (g)",
    "colesterol_100g": "Colesterol (mg)", "carbohidratos_100g": "Hidratos de carbono disponibles (g)",
    "azucares_100g": "Azúcares totales (g)", "fibra_100g": "Fibra (g)", "sodio_100g": "Sodio (mg)",
}
NUTRI_SUB = {"grasas_sat_100g", "grasas_trans_100g", "azucares_100g"}   # filas con sangría


def num(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def tabla_nutricional(mix, usado, datos):
    por100 = [sum(g * datos[pid][i] for pid, g in mix.items() if pid in datos) / usado
            for i in range(len(NUTRI_COLS))]
    factor = PORCION_G / 100
    celdas = ""
    for i, col in enumerate(NUTRI_COLS):
        sangria = " style='padding-left:1.1rem'" if col in NUTRI_SUB else ""
        celdas += (f"<tr><td{sangria}>{NUTRI_ETQ[col]}</td>"
                   f"<td>{num(por100[i])}</td><td>{num(por100[i] * factor)}</td></tr>")
    st.markdown(
        "<style>.nutri{border:2px solid currentColor;border-radius:8px;padding:.6rem .8rem;"
        "margin-top:.8rem;font-size:.9rem}"
        ".nutri table{width:100%;border-collapse:collapse}"
        ".nutri td,.nutri th{padding:.2rem 0;border-top:1px solid rgba(128,128,128,.4);text-align:right}"
        ".nutri td:first-child,.nutri th:first-child{text-align:left}</style>"
        "<div class='nutri'><b style='font-size:1.05rem'>Información nutricional</b><br>"
        f"Porción: {PORCION_G} g<br>Porciones por envase: {num(usado / PORCION_G)}"
        f"<table><tr><th></th><th>100 g</th><th>1 porción</th></tr>{celdas}</table>"
        "<small>Valores calculados según los productos de tu mix.</small></div>",
        unsafe_allow_html=True)

@st.cache_data(ttl=300, show_spinner="Cargando tu mix…")
def catalogo():
    cols = ", ".join(NUTRI_COLS)
    asegurar_config()                                            # crea la columna de margen propio si falta
    with transaccion() as cur:
        cur.execute(f"""
            SELECT json_build_object(
              'productos', (SELECT COALESCE(json_agg(json_build_array(id, emoji, nombre, precio_kg, stock_g, costo_kg, margen)
                              ORDER BY nombre), '[]'::json) FROM productos WHERE activo AND stock_g > 0),
              'tamanos',   (SELECT COALESCE(json_agg(gramos ORDER BY gramos), '[]'::json)
                              FROM presentaciones WHERE activo),
              'empaques',  (SELECT COALESCE(json_agg(json_build_array(tipo, gramos, precio, costo)), '[]'::json)
                              FROM empaques WHERE activo AND stock > 0),
              'recetas',   (SELECT COALESCE(json_agg(json_build_array(r.id, r.emoji, r.nombre, r.descripcion,
                              i.producto_id, i.gramos) ORDER BY r.nombre, i.id), '[]'::json)
                              FROM recetas r JOIN receta_items i ON i.receta_id = r.id WHERE r.activo),
              'nutri',     (SELECT COALESCE(json_agg(json_build_array(id, {cols})), '[]'::json) FROM productos)
            )""")
        d = cur.fetchone()[0]
        cfg = leer_config_precios(cur)
    productos = [tuple(f) for f in d["productos"]]                # el precio final se calcula según el tamaño elegido
    empaques = {(t, g): float(p) for t, g, p, _ in d["empaques"]}                  # el empaque no lleva margen automático
    nutri = {f[0]: [float(v or 0) for v in f[1:]] for f in d["nutri"]}
    return productos, d["tamanos"], empaques, [tuple(f) for f in d["recetas"]], nutri, cfg

# ================= Vista del cliente =================
def confirmacion(u):
    st.title("¡Pedido recibido! 🎉")
    st.success(f"Pedido #{u['id']} · {u['gramos']} g · Total {MONEDA}{u['total']:.2f}")
    for nom, g, sub in u["lineas"]:
        st.write(f"**{nom}** — {g} g · {MONEDA}{sub:.2f}")
    st.write(f"Empaque {u['empaque']} — {MONEDA}{u['precio_emp']:.2f}")
    if u.get("cargo", 0) > 0:
        st.write(f"Armado — {MONEDA}{u['cargo']:.2f}")
    if WHATSAPP:
        detalle = "\n".join(f"- {n}: {g} g" for n, g, _ in u["lineas"])
        if u.get("cargo", 0) > 0:
            detalle += f"\n- Armado: {MONEDA}{u['cargo']:.2f}"
        msg = (f"Hola, soy {u['nombre']}. Pedido #{u['id']} (mix de {u['gramos']} g, "
               f"empaque {u['empaque']}):\n{detalle}\nTotal: {MONEDA}{u['total']:.2f}")
        st.link_button("Enviar por WhatsApp", f"https://wa.me/{WHATSAPP}?text={urllib.parse.quote(msg)}",
                       use_container_width=True, type="primary")
    if st.button("Armar otro mix", use_container_width=True):
        st.session_state.ultimo = None
        st.rerun()

@st.fragment
def armador(productos, tope, tipo, precio_empaque, nutri, cargo=0.0):
    st.subheader("Productos")
    if st.session_state.mix:
        st.button("🗑️ Vaciar mi mix", on_click=vaciar)

    aviso = st.session_state.pop("aviso", None)
    if aviso:
        st.toast(aviso)
    st.caption(f"Toca + o − , o escribe los gramos que quieras (mínimo {MIN_G} g por producto). "
               "Después de escribir, toca fuera del cuadro o pulsa Enter.")
    for pid, emoji, nombre, precio_kg, stock in productos:
        gramos = st.session_state.mix.get(pid, 0)
        st.session_state[f"g{pid}"] = gramos          # el cuadro siempre refleja el mix real
        libre = margen(pid, stock)
        sub = float(sub_linea(gramos, precio_kg))
        detalle = f"<b>{MONEDA}{sub:.2f}</b>" if (gramos and MOSTRAR_SUBTOTAL) else ""
        with st.container(border=True, key=("sel_" if gramos else "card_") + str(pid)):
            c1, c2, c3, c4 = st.columns([3.1, 1, 1.7, 1])
            c1.markdown(f"<div class='pn'>{emoji} {nombre}</div><div class='pp'>{detalle}</div>",
                        unsafe_allow_html=True)
            c2.button("−", key=f"m{pid}", on_click=restar, args=(pid,), disabled=gramos == 0,
                      use_container_width=True)
            c3.number_input(f"Gramos de {nombre}", min_value=0, step=PASO, format="%d", key=f"g{pid}",
                            label_visibility="collapsed", on_change=escribir, args=(pid, stock))
            c4.button("+", key=f"p{pid}", on_click=sumar, args=(pid, stock),
                      disabled=libre == 0 or gramos + min(PASO, libre) < MIN_G,
                      use_container_width=True)

    usado = total_usado()
    precios = {pid: pk for pid, _, _, pk, _ in productos}
    subtotal_mix = sum((sub_linea(g, precios[pid]) for pid, g in st.session_state.mix.items() if pid in precios), D(0))
    cargo_d = D(str(cargo)) if usado > 0 else D(0)
    total = subtotal_mix + cargo_d + D(str(precio_empaque))

    st.divider()
    st.progress(usado / tope)
    if usado == tope:
        st.success(f"¡Mix completo! {usado} / {tope} g")
    else:
        st.write(f"**{usado} / {tope} g** · faltan {tope - usado} g")
    armado = f" · Armado: {MONEDA}{cargo_d:.2f}" if cargo_d > 0 else ""
    st.write(f"Mix: {MONEDA}{subtotal_mix:.2f}{armado} · Empaque {tipo}: {MONEDA}{precio_empaque:.2f}")
    st.subheader(f"Total: {MONEDA}{total:.2f}")
    if usado > 0:
        tabla_nutricional(st.session_state.mix, usado, nutri)

def vista_cliente():
    if st.session_state.get("ultimo"):
        return  confirmacion(st.session_state.ultimo)   
    try:
        productos, tamanos, empaques, filas_rec, nutri, cfg_precios = catalogo()
    except Exception as e:
        mostrar_error(e)
        st.error("Estamos teniendo un problema técnico. Intenta de nuevo en unos minutos.")
        return
    if not productos or not tamanos:
        st.info("Por ahora no hay productos disponibles.")
        return

    st.title("🥜 Arma tu mix")
    st.radio("Tamaño de tu mix", tamanos, key="tope", horizontal=True,
             format_func=lambda g: f"{g} g", on_change=cambiar_tamano)
    tope = st.session_state.tope
    # precio por kilo de cada producto para este tamaño: margen propio > margen del tamaño > margen general
    productos = [(i, e, n, float(precio_venta(c, p, cfg_precios["auto"], margen_efectivo(m, tope, cfg_precios))), st_)
                 for i, e, n, p, st_, c, m in productos]

    tipos = sorted({t for (t, g) in empaques if g == tope})
    if not tipos:
        st.warning("Por ahora no hay empaques disponibles para este tamaño. Elige otro tamaño.")
        return
    if st.session_state.get("tipo") not in tipos:
        st.session_state.tipo = tipos[0]
    st.radio("Empaque", tipos, key="tipo", horizontal=True,
             format_func=lambda t: f"{'🫙' if t == 'Vidrio' else '🧴'} {t} (+{MONEDA}{empaques[(t, tope)]:.2f})")
    tipo = st.session_state.tipo
    precio_empaque = empaques[(tipo, tope)]

    st.session_state.stock_de = {pid: stock for pid, _, _, _, stock in productos}
    nombre_de = {pid: nom for pid, _, nom, _, _ in productos}
    recetas = {}
    for rid, emo, nom, desc, pid, g in filas_rec:
        r = recetas.setdefault(rid, {"emoji": emo, "nombre": nom, "desc": desc, "items": []})
        r["items"].append((pid, g))
    # solo se muestran los mixes cuyos productos están todos disponibles
    recetas = {k: r for k, r in recetas.items() if all(p in nombre_de for p, _ in r["items"])}

    if recetas:
        st.subheader("Mixes listos")
        for rid, r in recetas.items():
            st.button(f"{r['emoji']} {r['nombre']}", key=f"rec{rid}", use_container_width=True,
                      on_click=aplicar_receta, args=(r["items"],))
            tot = sum(g for _, g in r["items"])
            comp = " · ".join(f"{round(g * 100 / tot)}% {nombre_de[p]}" for p, g in r["items"])
            st.caption((r["desc"] + " — " if r["desc"] else "") + comp)
        st.caption("Elige uno y, si quieres, ajústalo con + y −.")

    armador(productos, tope, tipo, precio_empaque, nutri, cfg_precios["cargo"])


    with st.form("pedido"):
        nombre = st.text_input("Tu nombre")
        tel = st.text_input("Tu celular (WhatsApp)")
        enviar = st.form_submit_button("Confirmar pedido", type="primary", use_container_width=True)

    if enviar:
        resultado = None
        if not st.session_state.mix:
            st.error("Agrega al menos un producto a tu mix.")
        elif len(nombre.strip()) < 2:
            st.error("Escribe tu nombre.")
        elif len("".join(c for c in tel if c.isdigit())) < 7:
            st.error("Escribe un celular válido, solo números.")
        else:
            try:
                resultado = crear_pedido(nombre, tel, tope, tipo, dict(st.session_state.mix))
            except ValueError as e:
                st.error(str(e))
            except Exception as e:
                mostrar_error(e)
                st.error("No pudimos guardar tu pedido. Inténtalo de nuevo.")
        if resultado:
            st.session_state.ultimo = resultado
            st.session_state.mix = {}
            st.session_state.receta_items = None
            st.rerun()
    

# ================= Administrador =================
def items_inventario():
    prods = consultar("SELECT id, emoji, nombre, stock_g, costo_kg FROM productos ORDER BY nombre")
    emps = consultar("SELECT id, tipo, gramos, stock, costo FROM empaques ORDER BY tipo, gramos")
    items = [("producto", i, f"{e} {n}", s, D(c)) for i, e, n, s, c in prods]
    items += [("empaque", i, f"🫙 {t} {g} g", s, D(c)) for i, t, g, s, c in emps]
    return items


def tab_pedidos():
    filas = consultar("SELECT id, creado_en, cliente_nombre, cliente_telefono, presentacion_g, empaque_tipo, "
                      "total, costo_total, estado FROM pedidos ORDER BY id DESC LIMIT 100")
    if not filas:
        st.info("Todavía no hay pedidos.")
        return
    df = pd.DataFrame([{"Pedido": r[0], "Fecha": hora(r[1]), "Cliente": r[2], "Celular": r[3],
                        "Gramos": r[4], "Empaque": r[5], "Total": float(r[6]),
                        "Ganancia": float(r[6] - r[7]), "Estado": r[8]} for r in filas])
    st.dataframe(df, hide_index=True, use_container_width=True)
    pid = st.selectbox("Ver pedido", df["Pedido"], format_func=lambda i: f"#{i}")
    for nom, g, sub in consultar("SELECT nombre, gramos, subtotal FROM pedido_items WHERE pedido_id=%s ORDER BY id",
                                 (int(pid),)):
        st.write(f"{nom} — {g} g · {MONEDA}{sub:.2f}")
    actual = df.loc[df["Pedido"] == pid, "Estado"].iat[0]
    nuevo = st.selectbox("Estado", ESTADOS, index=ESTADOS.index(actual))
    if st.button("Actualizar estado"):
        if actual == "cancelado":
            st.error("Un pedido cancelado no se puede reactivar. Crea uno nuevo.")
        elif nuevo == "cancelado":
            cancelar_pedido(int(pid))      # devuelve el stock y deja el rastro en el kardex
            st.rerun()
        else:
            ejecutar("UPDATE pedidos SET estado=%s WHERE id=%s", (nuevo, int(pid)))
            st.rerun()


def tab_kardex():
    items = items_inventario()
    i = st.selectbox("Ver kardex de", range(len(items)), format_func=lambda k: items[k][2], key="kx")
    tipo, iid, etq, stock, costo = items[i]
    es_prod = tipo == "producto"
    u, f, cu = ("g", 1000, "kg") if es_prod else ("unid.", 1, "unid.")
    col = "producto_id" if es_prod else "empaque_id"
    filas = consultar(f"SELECT fecha, motivo, cantidad, saldo, costo_unitario, costo_promedio, pedido_id, nota "
                      f"FROM kardex WHERE {col}=%s ORDER BY id DESC LIMIT 500", (iid,))
    a, b = st.columns(2)
    a.metric("Stock actual", f"{stock:,} {u}")
    b.metric(f"Costo promedio por {cu}", f"{MONEDA}{float(costo):.2f}")
    if not filas:
        st.info("Aún no hay movimientos.")
        return
    df = pd.DataFrame(filas, columns=["fecha", "motivo", "cant", "saldo", "cu", "cp", "pedido", "nota"])
    out = pd.DataFrame({
        "Fecha": [hora(r[0]) for r in filas],
        "Movimiento": df["motivo"].str.replace("_", " ").str.capitalize(),
        f"Entrada ({u})": df["cant"].where(df["cant"] > 0),
        f"Salida ({u})": (-df["cant"]).where(df["cant"] < 0),
        f"Saldo ({u})": df["saldo"],
        f"Costo/{cu}": (df["cu"].astype(float) * f).round(2),
        f"Costo prom./{cu}": (df["cp"].astype(float) * f).round(2),
        "Valor saldo": (df["saldo"] * df["cp"].astype(float)).round(2),
        "Pedido": df["pedido"].astype("Int64"),
        "Nota": df["nota"],
    })
    st.dataframe(out, hide_index=True, use_container_width=True)
    st.download_button("⬇️ Descargar kardex (Excel/CSV)", out.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"kardex_{etq}.csv", mime="text/csv")


def tab_inventario():
    items = items_inventario()
    filas, valor_total, sin_costo = [], D(0), False
    for tipo, iid, etq, stock, costo in items:
        if tipo == "producto":
            cant, valor, u = D(stock) / 1000, D(stock) / 1000 * costo, "kg"
        else:
            cant, valor, u = D(stock), D(stock) * costo, "unid."
        valor_total += valor
        sin_costo = sin_costo or costo == 0
        filas.append({"Ítem": etq, "Stock": f"{float(cant):g} {u}",
                      "Costo promedio": f"{MONEDA}{float(costo):.2f}/{u}", "Valor": f"{MONEDA}{float(valor):.2f}"})
    st.metric("Valor del inventario", f"{MONEDA}{float(valor_total):,.2f}")
    if sin_costo:
        st.warning("Hay ítems con costo 0: tu ganancia saldrá inflada. Corrige el costo en la pestaña Precios "
                   "o registra una compra con su costo.")
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)

    st.divider()
    tab_conteo()
    st.divider()

    st.subheader("Registrar movimiento")
    i = st.selectbox("Ítem", range(len(items)), format_func=lambda k: items[k][2])
    tipo, iid, etq, _, _ = items[i]
    es_prod = tipo == "producto"
    modo = st.radio("Movimiento", ["Compra (entra)", "Merma (sale)", "Conteo: sumar", "Conteo: restar"],
                    horizontal=True)
    with st.form("mov", clear_on_submit=True):
        cant = st.number_input("Cantidad en kg" if es_prod else "Cantidad en unidades",
                               min_value=0.0, step=0.5 if es_prod else 1.0)
        costo = None
        if modo.startswith("Compra"):
            costo = st.number_input("Costo por kg" if es_prod else "Costo por unidad", min_value=0.0, step=1.0)
        nota = st.text_input("Nota (proveedor, motivo…)")
        if st.form_submit_button("Registrar", type="primary"):
            q = int(round(cant * 1000)) if es_prod else int(round(cant))
            if q <= 0:
                st.error("La cantidad debe ser mayor que 0.")
            else:
                motivo, signo = {"Compra (entra)": ("compra", 1), "Merma (sale)": ("merma", -1),
                                 "Conteo: sumar": ("ajuste", 1), "Conteo: restar": ("ajuste", -1)}[modo]
                c = D(str(costo)) / (1000 if es_prod else 1) if costo is not None else None
                try:
                    with transaccion(escribe=True) as cur:
                        mover(cur, tipo, iid, signo * q, motivo, costo=c, nota=nota or None, nombre=etq)
                    st.toast("Movimiento registrado ✅")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def ajustar_a(cur, tipo, iid, objetivo, nota, nombre):
    """Deja el stock exactamente en `objetivo` (gramos o unidades) y registra la diferencia como ajuste."""
    if tipo == "producto":
        cur.execute("SELECT stock_g FROM productos WHERE id=%s FOR UPDATE", (iid,))
    else:
        cur.execute("SELECT stock FROM empaques WHERE id=%s FOR UPDATE", (iid,))
    diferencia = objetivo - cur.fetchone()[0]
    if diferencia:
        mover(cur, tipo, iid, diferencia, "ajuste", nota=nota, nombre=nombre)
    return diferencia


def fijar_costo(cur, tipo, iid, costo_unitario, nota):
    """Corrige el costo (por gramo o por unidad) y lo deja en el kardex como un movimiento de cantidad 0."""
    if tipo == "producto":
        cur.execute("UPDATE productos SET costo_kg=%s WHERE id=%s RETURNING stock_g", (costo_unitario * 1000, iid))
    else:
        cur.execute("UPDATE empaques SET costo=%s WHERE id=%s RETURNING stock", (costo_unitario, iid))
    saldo = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO kardex (producto_id, empaque_id, motivo, cantidad, saldo, costo_unitario, "
        "costo_promedio, nota) VALUES (%s,%s,'ajuste',0,%s,%s,%s,%s)",
        (iid if tipo == "producto" else None, iid if tipo == "empaque" else None,
         saldo, costo_unitario, costo_unitario, nota))


def cambios_conteo(editado, original, tipo):
    """Compara lo que escribiste con lo que hay en el sistema y devuelve solo lo que cambió."""
    es_p = tipo == "producto"
    k_nom, k_sis, k_tengo, k_cost = (("Producto", "Sistema", "Tengo", "Costo/kg") if es_p
                                      else ("Empaque", "Sistema", "Tengo", "Costo/unid."))
    mult = 1000 if es_p else 1
    base = {int(o["id"]): o for o in original.to_dict("records")}
    salida = []
    for e in editado.to_dict("records"):
        o = base[int(e["id"])]
        tengo = float(o[k_tengo]) if pd.isna(e[k_tengo]) else float(e[k_tengo])
        costo = float(o[k_cost]) if pd.isna(e[k_cost]) else float(e[k_cost])
        objetivo, actual = int(round(tengo * mult)), int(round(float(o[k_sis]) * mult))
        if objetivo != actual or abs(costo - float(o[k_cost])) > 0.0001:
            salida.append({"tipo": tipo, "id": int(e["id"]), "nombre": o[k_nom], "actual": actual,
                           "objetivo": objetivo, "costo": costo, "costo_antes": float(o[k_cost]), "mult": mult})
    return salida


def tab_conteo():
    st.subheader("Editar todo mi inventario")
    st.caption("Escribe lo que realmente tienes (conteo físico) y lo que te cuesta cada ítem. Al guardar, la app "
               "registra la diferencia en el kardex como «Ajuste», así el historial siempre cuadra.")
    exito = st.session_state.pop("conteo_ok", None)
    if exito is not None:
        st.success(f"Inventario actualizado: {exito} ítem(s) con cambios. El detalle está en la pestaña Kardex.")
    v = st.session_state.get("inv_v", 0)

    prods = consultar("SELECT id, emoji, nombre, stock_g, costo_kg FROM productos ORDER BY nombre")
    emps = consultar("SELECT id, tipo, gramos, stock, costo FROM empaques ORDER BY tipo, gramos")
    dfp = pd.DataFrame([(i, f"{e} {n}", s / 1000, s / 1000, float(c)) for i, e, n, s, c in prods],
                       columns=["id", "Producto", "Sistema", "Tengo", "Costo/kg"])
    dfe = pd.DataFrame([(i, f"{t} {g} g", s, s, float(c)) for i, t, g, s, c in emps],
                       columns=["id", "Empaque", "Sistema", "Tengo", "Costo/unid."])

    st.markdown("**Productos (en kilos)**")
    edp = st.data_editor(
        dfp, hide_index=True, use_container_width=True, key=f"inv_p_{v}",
        disabled=["id", "Producto", "Sistema"],
        column_config={"id": None,
                       "Producto": st.column_config.TextColumn(width="small"),
                       "Sistema": st.column_config.NumberColumn(format="%.3f", width="small"),
                       "Tengo": st.column_config.NumberColumn(min_value=0.0, step=0.001, format="%.3f", width="small"),
                       "Costo/kg": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="%.2f", width="small")})
    st.markdown("**Empaques (en unidades)**")
    ede = st.data_editor(
        dfe, hide_index=True, use_container_width=True, key=f"inv_e_{v}",
        disabled=["id", "Empaque", "Sistema"],
        column_config={"id": None,
                       "Empaque": st.column_config.TextColumn(width="small"),
                       "Sistema": st.column_config.NumberColumn(format="%d", width="small"),
                       "Tengo": st.column_config.NumberColumn(min_value=0, step=1, format="%d", width="small"),
                       "Costo/unid.": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="%.2f", width="small")})

    cambios = cambios_conteo(edp, dfp, "producto") + cambios_conteo(ede, dfe, "empaque")
    if cambios:
        st.markdown("**Cambios pendientes** (todavía no se guardaron)")
        filas = []
        for c in cambios:
            u, d = ("kg", 1000) if c["tipo"] == "producto" else ("unid.", 1)
            filas.append({"Ítem": c["nombre"],
                          "Stock": f"{c['actual'] / d:g} → {c['objetivo'] / d:g} {u}",
                          "Costo": f"{MONEDA}{c['costo_antes']:.2f} → {MONEDA}{c['costo']:.2f}"})
        st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)

    if st.button("💾 Guardar inventario", type="primary", disabled=not cambios, use_container_width=True):
        try:
            with transaccion(escribe=True) as cur:
                for c in cambios:
                    if abs(c["costo"] - c["costo_antes"]) > 0.0001:      # primero el costo, luego la cantidad
                        fijar_costo(cur, c["tipo"], c["id"], D(str(c["costo"])) / c["mult"], "Conteo físico: costo")
                    if c["objetivo"] != c["actual"]:
                        ajustar_a(cur, c["tipo"], c["id"], c["objetivo"], "Conteo físico", c["nombre"])
            st.session_state.conteo_ok = len(cambios)
            st.session_state.inv_v = v + 1                                # reinicia las tablas con los datos nuevos
            st.rerun()
        except ValueError as e:
            st.error(str(e))
        except Exception as e:
            mostrar_error(e)
            st.error("No se pudo guardar el inventario. Inténtalo de nuevo.")


def tab_precios():
    st.caption("El stock no se edita aquí: se mueve desde Inventario para que el kardex siempre cuadre.")
    st.subheader("Productos")
    df = pd.DataFrame(consultar("SELECT id, emoji, nombre, precio_kg, costo_kg, activo FROM productos ORDER BY nombre"),
                      columns=["id", "emoji", "nombre", "precio_kg", "costo_kg", "activo"])
    df[["precio_kg", "costo_kg"]] = df[["precio_kg", "costo_kg"]].astype(float)
    ed = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["id"], key="ed_prod",
                        column_config={"precio_kg": st.column_config.NumberColumn("Precio venta/kg", min_value=0.0, format="%.2f"),
                                       "costo_kg": st.column_config.NumberColumn("Costo/kg", min_value=0.0, format="%.2f"),
                                       "activo": st.column_config.CheckboxColumn("Activo")})
    st.caption("El costo se actualiza solo con cada compra. Edítalo únicamente para corregir el costo inicial.")
    if st.button("Guardar productos", type="primary"):
        for r in ed.to_dict("records"):
            ejecutar("UPDATE productos SET emoji=%s, nombre=%s, precio_kg=%s, costo_kg=%s, activo=%s WHERE id=%s",
                     (r["emoji"] or "🥜", str(r["nombre"]).strip(), float(r["precio_kg"]),
                      float(r["costo_kg"]), bool(r["activo"]), int(r["id"])))
        st.success("Productos guardados.")
    with st.expander("➕ Agregar producto nuevo"):
        with st.form("nuevo_prod", clear_on_submit=True):
            n = st.text_input("Nombre")
            e = st.text_input("Emoji", value="🥜")
            pk = st.number_input("Precio de venta por kg", min_value=0.0, step=1.0)
            if st.form_submit_button("Agregar") and n.strip():
                try:
                    ejecutar("INSERT INTO productos (nombre, emoji, precio_kg, stock_g) VALUES (%s,%s,%s,0)",
                             (n.strip(), e.strip() or "🥜", pk))
                    st.success("Agregado. Registra su primera compra en Inventario.")
                except Exception:
                    st.error("No se pudo agregar. ¿Ya existe ese nombre?")

    st.divider()
    st.subheader("Empaques")
    df = pd.DataFrame(consultar("SELECT id, tipo, gramos, precio, costo, activo FROM empaques ORDER BY tipo, gramos"),
                      columns=["id", "tipo", "gramos", "precio", "costo", "activo"])
    df[["precio", "costo"]] = df[["precio", "costo"]].astype(float)
    ed = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["id", "tipo", "gramos"],
                        key="ed_emp",
                        column_config={"precio": st.column_config.NumberColumn("Precio venta", min_value=0.0, format="%.2f"),
                                       "costo": st.column_config.NumberColumn("Costo unidad", min_value=0.0, format="%.2f"),
                                       "activo": st.column_config.CheckboxColumn("Activo")})
    if st.button("Guardar empaques", type="primary"):
        for r in ed.to_dict("records"):
            ejecutar("UPDATE empaques SET precio=%s, costo=%s, activo=%s WHERE id=%s",
                     (float(r["precio"]), float(r["costo"]), bool(r["activo"]), int(r["id"])))
        st.success("Empaques guardados.")
    with st.expander("➕ Agregar tipo de empaque"):
        tamanos = [r[0] for r in consultar("SELECT gramos FROM presentaciones ORDER BY gramos")]
        with st.form("nuevo_emp", clear_on_submit=True):
            t = st.text_input("Tipo (ejemplo: Vidrio, Plástico, Bolsa)")
            g = st.selectbox("Para el tamaño", tamanos, format_func=lambda x: f"{x} g")
            p = st.number_input("Precio de venta del empaque", min_value=0.0, step=0.1)
            if st.form_submit_button("Agregar") and t.strip():
                try:
                    ejecutar("INSERT INTO empaques (tipo, gramos, precio, stock) VALUES (%s,%s,%s,0)", (t.strip(), g, p))
                    st.success("Agregado. Registra su compra en Inventario.")
                except Exception:
                    st.error("Ese tipo ya existe para ese tamaño.")


def tab_ventas():
    filas = []
    for etiqueta, unidad in [("Hoy", "day"), ("Este mes", "month"), ("Total", None)]:
        filtro, params = "", ()
        if unidad:
            filtro = "AND creado_en >= date_trunc(%s, now() AT TIME ZONE %s) AT TIME ZONE %s"
            params = (unidad, ZONA, ZONA)
        n, venta, costo = consultar(
            "SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(costo_total),0) FROM pedidos "
            "WHERE estado <> 'cancelado' " + filtro, params)[0]    
        
        gan = venta - costo
        filas.append({"Periodo": etiqueta, "Pedidos": n, "Ventas": f"{MONEDA}{float(venta):.2f}",
                      "Costo": f"{MONEDA}{float(costo):.2f}", "Ganancia": f"{MONEDA}{float(gan):.2f}",
                      "Margen": f"{float(gan / venta * 100):.0f}%" if venta else "-"})
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)
    st.subheader("Más vendidos")
    top = consultar("SELECT i.nombre, SUM(i.gramos) FROM pedido_items i JOIN pedidos p ON p.id=i.pedido_id "
                    "WHERE p.estado <> 'cancelado' GROUP BY i.nombre ORDER BY 2 DESC LIMIT 5")
    if top:
        st.dataframe(pd.DataFrame([{"Producto": n, "Gramos vendidos": int(g)} for n, g in top]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("Aún no hay ventas.")

# ================= Márgenes de ganancia =================
@st.cache_resource(show_spinner=False)
def asegurar_config():
    """Crea la tabla de configuración la primera vez (no hace falta ejecutar SQL a mano)."""
    with transaccion() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS config (clave VARCHAR(50) PRIMARY KEY, valor VARCHAR(100) NOT NULL)")
        cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS margen NUMERIC(5,2)")   # margen propio (opcional)
        cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cargo_armado NUMERIC(10,2) NOT NULL DEFAULT 0")
    return True


def leer_margenes():
    """Devuelve (objetivo, mínimo) en porcentaje."""
    asegurar_config()
    cfg = dict(consultar("SELECT clave, valor FROM config WHERE clave IN ('margen_objetivo', 'margen_minimo')"))
    return float(cfg.get("margen_objetivo", 40)), float(cfg.get("margen_minimo", 25))


def leer_config_precios(cur=None):
    """Devuelve {'auto': bool, 'general': margen %, 'tam': {gramos: margen %}}."""
    asegurar_config()
    sql = ("SELECT clave, valor FROM config WHERE clave IN ('precios_auto', 'margen_objetivo', 'cargo_armado') "
           "OR left(clave, 11) = 'margen_tam_'")
    if cur is None:
        cfg = dict(consultar(sql))
    else:
        cur.execute(sql)
        cfg = dict(cur.fetchall())
    tam = {int(k[11:]): float(v) for k, v in cfg.items() if k.startswith("margen_tam_")}
    return {"auto": cfg.get("precios_auto", "0") == "1", "general": float(cfg.get("margen_objetivo", 40)), "tam": tam,
            "cargo": float(cfg.get("cargo_armado", 0))}


def margen_efectivo(margen_producto, tope, cfg):
    """Qué margen se aplica: el propio del producto, si no el del tamaño del mix, si no el general."""
    if margen_producto is not None:
        return float(margen_producto)
    if tope in cfg["tam"]:
        return cfg["tam"][tope]
    return cfg["general"]


def precio_venta(costo, precio_manual, auto, margen):
    """Precio de venta (por kg o por unidad). Con precios automáticos se calcula desde el costo,
    redondeado hacia arriba al centavo. Un ítem sin costo conserva su precio manual."""
    costo, manual = D(str(costo)), D(str(precio_manual))
    if auto and costo > 0 and margen < 100:
        return (costo / (1 - D(str(margen)) / 100)).quantize(D("0.01"), rounding=ROUND_CEILING)
    return manual


def guardar_margenes(objetivo, minimo, auto=False, tam=None, cargo=0.0):
    """tam = {gramos: margen %}. Un margen de 0 (o vacío) borra el del tamaño y vuelve a usar el general."""
    asegurar_config()
    with transaccion(escribe=True) as cur:                      # escribe=True: renueva el catálogo de los clientes
        for clave, valor in (("margen_objetivo", objetivo), ("margen_minimo", minimo),
                             ("precios_auto", "1" if auto else "0"), ("cargo_armado", cargo)):
            cur.execute("INSERT INTO config (clave, valor) VALUES (%s, %s) "
                        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor", (clave, str(valor)))
        for gramos, valor in (tam or {}).items():
            if valor and valor > 0:
                cur.execute("INSERT INTO config (clave, valor) VALUES (%s, %s) "
                            "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor", (f"margen_tam_{gramos}", str(valor)))
            else:
                cur.execute("DELETE FROM config WHERE clave = %s", (f"margen_tam_{gramos}",))


def guardar_margenes_producto(filas):
    """filas = [(id_producto, margen % o None)]. None borra el margen propio."""
    asegurar_config()
    with transaccion(escribe=True) as cur:
        for pid, margen in filas:
            cur.execute("UPDATE productos SET margen = %s WHERE id = %s", (margen, pid))


def analizar_margen(precio, costo, objetivo, minimo):
    """Margen = (precio - costo) / precio. objetivo y minimo en %. Devuelve el análisis de un ítem."""
    precio, costo = D(str(precio)), D(str(costo))
    if costo <= 0:
        return {"margen": None, "ganancia": precio, "estado": "⚪ Sin costo", "sugerido": None}
    if precio <= 0:
        return {"margen": None, "ganancia": -costo, "estado": "⚪ Sin precio", "sugerido": None}
    margen = (precio - costo) / precio * 100
    estado = "🟢 Bien" if margen >= D(str(objetivo)) else "🟡 Mejorable" if margen >= D(str(minimo)) else "🔴 Bajo"
    sugerido = costo / (1 - D(str(objetivo)) / 100)                       # precio que da exactamente el margen objetivo
    sugerido = (sugerido / D("0.1")).to_integral_value(rounding=ROUND_CEILING) * D("0.1")   # redondeado hacia arriba a 0.10
    return {"margen": margen, "ganancia": precio - costo, "estado": estado, "sugerido": sugerido}


def tab_margenes():
    st.subheader("Margen de ganancia")
    st.caption("Margen = (precio − costo) ÷ precio. Se calcula solo con los precios y costos que tienes guardados. "
               "Es margen bruto: no incluye etiquetas, envío, comisiones ni tu tiempo.")
    copiados = st.session_state.pop("costos_copiados", None)
    if copiados:
        st.success(f"Listo: {copiados} ítem(s) ahora tienen costo. Ya puedes activar los precios automáticos.")
    obj_g, min_g = leer_margenes()
    cfg_g = leer_config_precios()
    auto_g = cfg_g["auto"]
    c1, c2 = st.columns(2)
    objetivo = c1.number_input("Margen objetivo (%)", min_value=1.0, max_value=95.0, value=obj_g, step=1.0,
                               help="El margen que quieres ganar. Con él se calcula el precio sugerido.")
    minimo = c2.number_input("Margen mínimo (%)", min_value=0.0, max_value=94.0, value=min_g, step=1.0,
                             help="Por debajo de este margen el producto se marca en rojo.")
    auto = st.toggle("Calcular los precios de los productos automáticamente con el margen objetivo", value=auto_g,
                     help="Precio de venta de cada producto = costo ÷ (1 − margen). El margen es el propio del producto, "
                          "si no el del tamaño del mix, si no el objetivo general. El cliente ve el precio ya "
                          "calculado. Los productos sin costo se venden a su precio manual. Los empaques NO se modifican: "
                          "se cobran a su precio de la tabla Empaques.")
    tamanos_act = [r[0] for r in consultar("SELECT gramos FROM presentaciones WHERE activo ORDER BY gramos")]
    st.markdown("**Margen por tamaño del mix (opcional)** · 0 = usar el margen objetivo")
    tam_nuevo = {}
    for col, g in zip(st.columns(max(len(tamanos_act), 1)), tamanos_act):
        tam_nuevo[g] = col.number_input(f"Margen para {g} g (%)", min_value=0.0, max_value=95.0, step=1.0,
                                        value=float(cfg_g["tam"].get(g, 0.0)), key=f"mtam_{g}",
                                        help="Útil para los mixes chicos: tus gastos por pedido pesan más ahí.")
    tam_vivo = {g: v for g, v in tam_nuevo.items() if v > 0}
    cargo_g = cfg_g["cargo"]
    cargo = st.number_input(f"Cargo por armado por pedido ({MONEDA})", min_value=0.0, step=0.1, value=float(cargo_g),
                            key="cargo_in",
                            help="Un monto fijo que se suma a cada pedido (etiqueta, bolsa, tu tiempo). No tiene costo asociado, "
                                 "así que es ganancia. El cliente lo ve como «Armado». 0 = no cobrar.")
    if auto:
        st.info(f"Precios automáticos en productos: cada uno se vende a su costo ÷ (1 − margen). El margen que se aplica es "
                f"el **propio del producto** (si lo tiene), si no el **del tamaño** (si lo definiste), si no el **general de {objetivo:g}%**. "
                "Los empaques se cobran a su precio normal, sin margen. "
                "Si cambia un costo (por una compra, por ejemplo), el precio del producto se ajusta solo.")
    if minimo >= objetivo:
        st.warning("El margen mínimo debe ser menor que el objetivo.")
    elif (objetivo, minimo, auto, tam_vivo, cargo) != (obj_g, min_g, auto_g, cfg_g["tam"], cargo_g) \
            and st.button("Guardar márgenes y cargo"):
        guardar_margenes(objetivo, minimo, auto, tam_nuevo, cargo)
        st.success("Márgenes y cargo guardados.")
    if minimo >= objetivo:
        return
    cfg_vivo = {"auto": auto, "general": objetivo, "tam": tam_vivo, "cargo": cargo}      # lo que estás viendo, aunque no lo hayas guardado

    if st.session_state.pop("mprod_ok", False):
        st.success("Márgenes por producto guardados.")
    with st.expander("🎯 Margen propio por producto (opcional)"):
        st.caption("Si escribes un margen aquí, ese producto lo usa en lugar del margen del tamaño o del general. "
                   "Déjalo vacío para no cambiarlo. Sirve, por ejemplo, para bajar el margen de un producto caro "
                   "(pistacho) y subirlo en uno barato (maní).")
        dfm = pd.DataFrame(consultar("SELECT id, emoji || ' ' || nombre, margen FROM productos WHERE activo ORDER BY nombre"),
                           columns=["id", "Producto", "Margen propio (%)"])
        dfm["Margen propio (%)"] = pd.to_numeric(dfm["Margen propio (%)"]).astype(float)
        edm = st.data_editor(dfm, hide_index=True, use_container_width=True, disabled=["id", "Producto"],
                             key=f"ed_mprod_{st.session_state.get('mprod_v', 0)}",
                             column_config={"id": None, "Margen propio (%)": st.column_config.NumberColumn(
                                 min_value=1.0, max_value=95.0, step=1.0, format="%.1f")})
        if st.button("Guardar márgenes por producto", key="guardar_mprod"):
            guardar_margenes_producto([(int(r["id"]), None if pd.isna(r["Margen propio (%)"]) else float(r["Margen propio (%)"]))
                                       for r in edm.to_dict("records")])
            st.session_state.mprod_v = st.session_state.get("mprod_v", 0) + 1
            st.session_state.mprod_ok = True
            st.rerun()

    tam_ver = st.selectbox("Ver precios para el tamaño de mix", tamanos_act, format_func=lambda g: f"{g} g",
                           index=tamanos_act.index(230) if 230 in tamanos_act else 0) if tamanos_act else None

    prods = consultar("SELECT emoji, nombre, precio_kg, costo_kg, margen FROM productos WHERE activo ORDER BY nombre")
    emps = consultar("SELECT tipo, gramos, precio, costo FROM empaques WHERE activo ORDER BY tipo, gramos")

    def fila(nombre, precio, costo, unidad, automatico, m_prod=None):
        origen = "precio fijo"
        if automatico:
            if auto and D(str(costo)) > 0:
                origen = "propio" if m_prod is not None else "tamaño" if tam_ver in tam_vivo else "general"
            else:
                origen = "manual"
            precio = precio_venta(costo, precio, auto, margen_efectivo(m_prod, tam_ver, cfg_vivo))   # lo que verá el cliente
        a = analizar_margen(precio, costo, objetivo, minimo)
        sug = a["sugerido"]
        return a["estado"], {
            "Ítem": nombre, f"Precio/{unidad}": f"{float(precio):.2f}", f"Costo/{unidad}": f"{float(costo):.2f}",
            "Ganancia": f"{float(a['ganancia']):.2f}",
            "Margen": f"{float(a['margen']):.1f}%" if a["margen"] is not None else "—",
            "Estado": a["estado"], "Origen": origen,
            "Sugerido": f"{float(sug):.2f}" if sug is not None else "—",
            "Cambio": (f"{float(sug - D(str(precio))):+.2f}" if sug is not None else "—")}

    filas_p = [fila(f"{e} {n}", p, c, "kg", True, m) for e, n, p, c, m in prods]
    filas_e = [fila(f"{t} {g} g", p, c, "unid.", False) for t, g, p, c in emps]

    # resumen: margen real de los últimos 30 días y cuántos ítems están en cada estado
    venta, costo = consultar("SELECT COALESCE(SUM(total), 0), COALESCE(SUM(costo_total), 0) FROM pedidos "
                             "WHERE estado <> 'cancelado' AND creado_en >= now() - interval '30 days'")[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Margen real (30 días)", f"{float((venta - costo) / venta * 100):.1f}%" if venta else "—",
              help="Calculado con tus pedidos reales de los últimos 30 días.")
    todos = [e for e, _ in filas_p + filas_e]
    m2.metric("🟢 Bien", todos.count("🟢 Bien"))
    m3.metric("🟡 Mejorable", todos.count("🟡 Mejorable"))
    m4.metric("🔴 Bajo", todos.count("🔴 Bajo"))
    if any(e.startswith("⚪") for e in todos):
        st.info("Los ítems con ⚪ no tienen costo o precio cargado. Cárgalos en la tabla de arriba o en "
                "Inventario → Editar todo mi inventario.")

    st.markdown("**Productos (por kilo)**" + (f" · precios para un mix de {tam_ver} g" if tam_ver else ""))
    if filas_p:
        st.dataframe(pd.DataFrame([f for _, f in filas_p]), hide_index=True, use_container_width=True)
    st.markdown("**Empaques (por unidad)** · se cobran a su precio de la tabla Empaques; aquí solo ves su margen")
    if filas_e:
        st.dataframe(pd.DataFrame([f for _, f in filas_e]), hide_index=True, use_container_width=True)
    st.markdown("**Margen del pedido completo (simulación con un mix listo)**")
    recetas_sim = consultar("SELECT id, emoji, nombre FROM recetas WHERE activo ORDER BY nombre")
    if not recetas_sim:
        st.info("Guarda un mix listo en Admin → Recetas para ver cuánto margen te deja un pedido completo, con empaque y armado.")
    else:
        nombres_sim = {i: f"{e} {n}" for i, e, n in recetas_sim}
        rid_sim = st.selectbox("Mix listo", list(nombres_sim), format_func=nombres_sim.get, key="sim_receta")
        items_sim = consultar("SELECT producto_id, gramos FROM receta_items WHERE receta_id = %s ORDER BY id", (rid_sim,))
        info_sim = {i: (p, c, m) for i, p, c, m in consultar("SELECT id, precio_kg, costo_kg, margen FROM productos")}
        items_sim = [(pid, g) for pid, g in items_sim if pid in info_sim]
        filas_sim = []
        for tam in tamanos_act:
            mix_sim = escalar_receta(items_sim, tam, {pid: 10 ** 9 for pid, _ in items_sim})
            venta_p, costo_p = D(0), D(0)
            for pid, g in mix_sim.items():
                p, c, m = info_sim[pid]
                venta_p += sub_linea(g, precio_venta(c, p, auto, margen_efectivo(m, tam, cfg_vivo)))
                costo_p += D(g) * D(str(c)) / 1000
            emp = consultar("SELECT precio, costo FROM empaques WHERE activo AND gramos = %s ORDER BY precio LIMIT 1", (tam,))
            e_precio, e_costo = (D(str(emp[0][0])), D(str(emp[0][1]))) if emp else (D(0), D(0))
            arm = D(str(cargo))
            total_s, costo_s = venta_p + arm + e_precio, costo_p + e_costo
            sin_arm = total_s - arm
            filas_sim.append({
                "Tamaño": f"{tam} g", "Productos": f"{float(venta_p):.2f}", "Armado": f"{float(arm):.2f}",
                "Empaque": f"{float(e_precio):.2f}", "Total": f"{float(total_s):.2f}", "Costo": f"{float(costo_s):.2f}",
                "Ganancia": f"{float(total_s - costo_s):.2f}",
                "Margen sin armado": f"{float((sin_arm - costo_s) / sin_arm * 100):.1f}%" if sin_arm > 0 else "—",
                "Margen del pedido": f"{float((total_s - costo_s) / total_s * 100):.1f}%" if total_s > 0 else "—"})
        st.dataframe(pd.DataFrame(filas_sim), hide_index=True, use_container_width=True)
        if any(D(str(c)) <= 0 for pid, _ in items_sim for c in (info_sim[pid][1],)):
            st.warning("Algún producto de este mix no tiene costo: el margen que ves está inflado.")
        st.caption("Usa el empaque más barato de cada tamaño y los márgenes que tienes puestos arriba (aunque no los hayas guardado). "
                   "«Margen sin armado» muestra cuánto subes el margen con el cargo.")

    pend_p = consultar("SELECT id, emoji, nombre, precio_kg FROM productos WHERE activo AND costo_kg = 0 AND precio_kg > 0 ORDER BY nombre")
    pend_e = consultar("SELECT id, tipo, gramos, precio FROM empaques WHERE activo AND costo = 0 AND precio > 0 ORDER BY tipo, gramos")
    if pend_p or pend_e:
        with st.expander("🔁 Usar mis precios actuales como costo (una sola vez)"):
            st.write("Estos ítems no tienen costo cargado. **Si el precio que tienes hoy es lo que te cuesta a ti** "
                     "(y no lo que cobras), puedes copiarlo como costo. Después, con los precios automáticos, el "
                     "sistema calcula el precio de venta de los productos con tu margen (los empaques siguen con su precio). "
                     "Si ese precio ya es lo que cobras, no lo copies: "
                     "escribe el costo real en Inventario → Editar todo mi inventario.")
            st.dataframe(pd.DataFrame(
                [{"Ítem": f"{e} {n}", "Se copiará como costo": f"{float(p):.2f} por kg"} for _, e, n, p in pend_p] +
                [{"Ítem": f"{t} {g} g", "Se copiará como costo": f"{float(p):.2f} por unidad"} for _, t, g, p in pend_e]),
                hide_index=True, use_container_width=True)
            if st.button("Copiar como costo", key="copiar_costo", type="primary"):
                with transaccion(escribe=True) as cur:
                    for i, _, _, p in pend_p:
                        fijar_costo(cur, "producto", i, D(str(p)) / 1000, "Precio actual copiado como costo")
                    for i, _, _, p in pend_e:
                        fijar_costo(cur, "empaque", i, D(str(p)), "Precio actual copiado como costo")
                st.session_state.costos_copiados = len(pend_p) + len(pend_e)
                st.rerun()
    st.caption(f"Sugerido = precio con el que ganarías el {objetivo:g}% de margen, redondeado hacia arriba a 0.10. "
               "Origen = de dónde sale el margen aplicado (propio, tamaño, general). Los semáforos se comparan con el "
               "margen objetivo y mínimo generales. Cambio = cuánto subiría (+) o bajaría (−) tu precio actual. "
               "El sugerido no se aplica solo: cambia el precio en la tabla de arriba. En los empaques mira el margen del pedido completo, no solo del envase.")


def tab_nutricion():
    st.subheader("Información nutricional (por 100 g)")
    st.caption("Escribe los valores de la etiqueta de cada producto. Con eso la app calcula la tabla del mix.")
    cols = ["id", "nombre"] + NUTRI_COLS
    df = pd.DataFrame(consultar("SELECT " + ", ".join(cols) + " FROM productos ORDER BY nombre"), columns=cols)
    df[NUTRI_COLS] = df[NUTRI_COLS].astype(float)
    cfg = {c: st.column_config.NumberColumn(NUTRI_ETQ[c], min_value=0.0, format="%.1f") for c in NUTRI_COLS}
    ed = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["id", "nombre"],
                        key="ed_nutri", column_config=cfg)
    if st.button("Guardar información nutricional", type="primary"):
        sets = ", ".join(f"{c}=%s" for c in NUTRI_COLS)
        for r in ed.to_dict("records"):
            vals = [0 if pd.isna(r[c]) else float(r[c]) for c in NUTRI_COLS]
            ejecutar(f"UPDATE productos SET {sets} WHERE id=%s", (*vals, int(r["id"])))
        st.success("Información nutricional guardada.")


def guardar_receta(nombre, emoji, desc, items):
    with transaccion(escribe=True) as cur:
        cur.execute(
            "INSERT INTO recetas (nombre, emoji, descripcion) VALUES (%s,%s,%s) "
            "ON CONFLICT (nombre) DO UPDATE SET emoji=EXCLUDED.emoji, "
            "descripcion=EXCLUDED.descripcion, activo=TRUE RETURNING id",
            (nombre.strip(), emoji.strip() or "🥜", desc.strip() or None))
        rid = cur.fetchone()[0]
        cur.execute("DELETE FROM receta_items WHERE receta_id=%s", (rid,))
        for pid, g in items:
            cur.execute("INSERT INTO receta_items (receta_id, producto_id, gramos) VALUES (%s,%s,%s)",
                        (rid, pid, g))


def recetas_guardadas(base):
    st.divider()
    st.subheader("Recetas guardadas")
    recetas = consultar("SELECT id, emoji, nombre, descripcion, activo FROM recetas ORDER BY nombre")
    if not recetas:
        st.info("Aún no guardaste recetas. Arma una arriba y toca «Guardar receta».")
        return
    info = base.set_index("id")
    for rid, emoji, nombre, desc, activo in recetas:
        items = consultar("SELECT producto_id, gramos FROM receta_items WHERE receta_id=%s ORDER BY id", (rid,))
        tot = sum(g for _, g in items) or 1
        costo = venta = 0.0
        partes = []
        for pid, g in items:
            if pid in info.index:
                f = info.loc[pid]
                costo += g * 1000 / tot * f["costo_kg"] / 1000
                venta += g * 1000 / tot * f["precio_kg"] / 1000
                partes.append(f"{round(g * 100 / tot)}% {f['nombre']}")
            else:
                partes.append(f"{round(g * 100 / tot)}% (producto inactivo)")
        with st.expander(f"{emoji} {nombre}" + ("" if activo else "  — oculta para clientes")):
            st.write(" · ".join(partes))
            gan = venta - costo
            st.caption(f"Costo 1 kg: {MONEDA}{costo:.2f} · Precio 1 kg: {MONEDA}{venta:.2f} · "
                       f"Margen: {gan / venta * 100:.0f}%" if venta else "Sin precios cargados.")
            a, b, c = st.columns(3)
            if a.button("✏️ Cargar", key=f"cg{rid}", use_container_width=True):
                st.session_state.receta_prefill = {pid: g for pid, g in items}
                st.session_state.receta_nombre, st.session_state.receta_emoji = nombre, emoji
                st.session_state.receta_desc = desc or ""
                st.session_state.receta_v = st.session_state.get("receta_v", 0) + 1
                st.rerun()
            if b.button("👁️ Mostrar/ocultar", key=f"vis{rid}", use_container_width=True):
                ejecutar("UPDATE recetas SET activo = NOT activo WHERE id=%s", (rid,))
                st.rerun()
            if c.button("🗑️ Eliminar", key=f"del{rid}", use_container_width=True):
                ejecutar("DELETE FROM recetas WHERE id=%s", (rid,))
                st.rerun()


def tab_receta():
    st.subheader("Calculadora de receta (por 1000 g)")
    st.caption("Escribe los gramos de cada producto. Si no suman 1000 g, se ajusta proporcionalmente.")
    cols = ["id", "emoji", "nombre", "precio_kg", "costo_kg"] + NUTRI_COLS
    base = pd.DataFrame(consultar("SELECT " + ", ".join(cols) + " FROM productos WHERE activo ORDER BY nombre"),
                        columns=cols)
    base[["precio_kg", "costo_kg"] + NUTRI_COLS] = base[["precio_kg", "costo_kg"] + NUTRI_COLS].astype(float)
    cfg_r = leer_config_precios()
    mp = {int(i): m for i, m in consultar("SELECT id, margen FROM productos")}
    base["precio_kg"] = [float(precio_venta(c, p, cfg_r["auto"], margen_efectivo(mp.get(int(i)), None, cfg_r)))
                         for i, c, p in zip(base["id"], base["costo_kg"], base["precio_kg"])]
    st.caption("El precio por kilo usa el margen propio de cada producto o el general. Los márgenes por tamaño se aplican al vender.")

    prefill = st.session_state.get("receta_prefill", {})
    entrada = pd.DataFrame({"Producto": base["emoji"] + " " + base["nombre"],
                            "Gramos": [int(prefill.get(int(i), 0)) for i in base["id"]]})
    ed = st.data_editor(entrada, hide_index=True, use_container_width=True, disabled=["Producto"],
                        key=f"receta_{st.session_state.get('receta_v', 0)}",
                        column_config={"Gramos": st.column_config.NumberColumn(min_value=0, step=10)})
    gramos = ed["Gramos"].fillna(0).astype(float).values
    total_g = gramos.sum()

    if total_g <= 0:
        st.info("Escribe los gramos de al menos un producto.")
    else:
        if total_g != 1000:
            st.warning(f"Tu receta suma {total_g:.0f} g. Abajo se muestra ajustada a 1000 g.")
        factor = 1000 / total_g
        g1000 = gramos * factor
        costo = float((g1000 * base["costo_kg"].values / 1000).sum())
        venta = float((g1000 * base["precio_kg"].values / 1000).sum())
        ganancia = venta - costo

        a, b, c, d = st.columns(4)
        a.metric("Costo 1 kg", f"{MONEDA}{costo:.2f}")
        b.metric("Precio 1 kg", f"{MONEDA}{venta:.2f}")
        c.metric("Ganancia", f"{MONEDA}{ganancia:.2f}")
        d.metric("Margen", f"{ganancia / venta * 100:.0f}%" if venta else "-")

        st.markdown("**Esta receta en cada tamaño**")
        tamanos = [r[0] for r in consultar("SELECT gramos FROM presentaciones WHERE activo ORDER BY gramos")]
        st.dataframe(pd.DataFrame([{"Tamaño": f"{t} g", "Costo del mix": round(costo * t / 1000, 2),
                                    "Precio del mix": round(venta * t / 1000, 2)} for t in tamanos]),
                     hide_index=True, use_container_width=True)

        st.markdown("**Información nutricional**")
        por100 = [float((gramos * base[col].values).sum() / total_g) for col in NUTRI_COLS]
        st.dataframe(pd.DataFrame({
            "Nutriente": [NUTRI_ETQ[c] for c in NUTRI_COLS],
            "100 g": [round(v, 1) for v in por100],
            f"{PORCION_G} g (1 porción)": [round(v * PORCION_G / 100, 1) for v in por100],
            "1000 g": [round(v * 10, 1) for v in por100],
        }), hide_index=True, use_container_width=True)

        st.markdown("**Guardar como mix para los clientes**")
        with st.form("guardar_receta"):
            nom = st.text_input("Nombre del mix", value=st.session_state.get("receta_nombre", ""),
                                placeholder="Ej: Mix Proteico")
            emo = st.text_input("Emoji", value=st.session_state.get("receta_emoji", "💪"))
            desc = st.text_input("Descripción corta (opcional)", value=st.session_state.get("receta_desc", ""),
                                 placeholder="Ej: Alto en proteína, ideal para entrenar")
            if st.form_submit_button("💾 Guardar receta", type="primary"):
                items = [(int(pid), int(round(g))) for pid, g in zip(base["id"], g1000) if round(g) > 0]
                if len(nom.strip()) < 2:
                    st.error("Escribe un nombre para el mix.")
                elif not items:
                    st.error("La receta no tiene productos.")
                else:
                    guardar_receta(nom, emo, desc, items)
                    st.success(f"«{nom.strip()}» guardado. Ya aparece para tus clientes.")

    recetas_guardadas(base)

def vista_admin():
    if not ADMIN_CLAVE:
        st.warning("Falta configurar ADMIN_CLAVE en los secretos.")
        return
        
    if not st.session_state.get("admin_ok"):
        intentos = st.session_state.get("intentos", 0)
        if intentos >= 5:
            st.error("Demasiados intentos. Recarga la página en unos minutos.")
            return
        with st.form("login"):
            intento = st.text_input("Contraseña de administrador", type="password")
            if st.form_submit_button("Entrar"):
                if hmac.compare_digest(intento.encode(), ADMIN_CLAVE.encode()):
                    st.session_state.admin_ok = True
                    st.session_state.intentos = 0
                    st.rerun()
                else:
                    st.session_state.intentos = intentos + 1
                    st.error("Contraseña incorrecta.")
        return
    if st.button("Cerrar sesión"):
        st.session_state.admin_ok = False
        st.rerun()
    t1, t2, t3, t4, t5, t6 = st.tabs(["📋 Pedidos", "📚 Kardex", "📦 Inventario", "💲 Precios", "📈 Ventas", "🧮 Receta 1 kg"])    
    with t1:
        tab_pedidos()
    with t2:
        tab_kardex()
    with t3:
        tab_inventario()
    with t4:
        tab_precios()
        st.divider()
        tab_margenes()
        st.divider()
        tab_nutricion()
    with t5:
        tab_ventas()
    with t6:
        tab_receta()

tab_cliente, tab_admin = st.tabs(["🥜 Mi mix", "🔐 Admin"])
with tab_cliente:
    vista_cliente()
with tab_admin:
    vista_admin()
