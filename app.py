import urllib.parse
from contextlib import contextmanager
from decimal import Decimal as D, ROUND_HALF_UP

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
MONEDA = secreto("MONEDA", "S/")
ADMIN_CLAVE = secreto("ADMIN_CLAVE")
WHATSAPP = secreto("WHATSAPP_NUMERO")
ZONA = secreto("ZONA_HORARIA", "UTC")


ESTADOS = ["pendiente", "preparando", "entregado", "cancelado"]

st.set_page_config(page_title="Mi Mix", page_icon="🥜")
st.markdown("""
<style>
div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; align-items: center; }
div[data-testid="stColumn"], div[data-testid="column"] { min-width: 0 !important; }
.stButton > button { min-height: 46px; font-size: 1.2rem; }
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
    with transaccion(escribe=True) as cur:
        cur.execute("SELECT id, precio FROM empaques WHERE tipo=%s AND gramos=%s AND activo", (tipo, tope))
        emp = cur.fetchone()
        if not emp:
            raise ValueError("Ese empaque ya no está disponible.")
        precio_emp = D(emp[1])
        cur.execute(
            "INSERT INTO pedidos (cliente_nombre, cliente_telefono, presentacion_g, total_g, total, "
            "empaque_id, empaque_tipo, precio_empaque) VALUES (%s,%s,%s,%s,0,%s,%s,%s) RETURNING id",
            (nombre.strip(), tel.strip(), tope, sum(mix.values()), emp[0], tipo, precio_emp))
        pedido_id = cur.fetchone()[0]
        total, costo, lineas = precio_emp, D(0), []
        for pid in sorted(mix):
            g = mix[pid]
            cur.execute("SELECT nombre, precio_kg, activo FROM productos WHERE id=%s", (pid,))
            nom, pkg, activo = cur.fetchone()
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
        cur.execute("UPDATE pedidos SET total=%s, costo_total=%s, costo_empaque=%s WHERE id=%s",
                    (total, costo, c_emp, pedido_id))
    return {"id": pedido_id, "total": total, "gramos": sum(mix.values()), "lineas": lineas,
            "empaque": tipo, "precio_emp": precio_emp, "nombre": nombre.strip()}


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


def sumar(pid, stock):
    st.session_state.receta_items = None
    actual = st.session_state.mix.get(pid, 0)
    if total_usado() + PASO <= st.session_state.tope and actual + PASO <= stock:
        st.session_state.mix[pid] = actual + PASO


def restar(pid):
    st.session_state.receta_items = None
    nuevo = st.session_state.mix.get(pid, 0) - PASO
    if nuevo <= 0:
        st.session_state.mix.pop(pid, None)
    else:
        st.session_state.mix[pid] = nuevo

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
    with transaccion() as cur:
        cur.execute(f"""
            SELECT json_build_object(
              'productos', (SELECT COALESCE(json_agg(json_build_array(id, emoji, nombre, precio_kg, stock_g)
                              ORDER BY nombre), '[]'::json) FROM productos WHERE activo AND stock_g > 0),
              'tamanos',   (SELECT COALESCE(json_agg(gramos ORDER BY gramos), '[]'::json)
                              FROM presentaciones WHERE activo),
              'empaques',  (SELECT COALESCE(json_agg(json_build_array(tipo, gramos, precio)), '[]'::json)
                              FROM empaques WHERE activo AND stock > 0),
              'recetas',   (SELECT COALESCE(json_agg(json_build_array(r.id, r.emoji, r.nombre, r.descripcion,
                              i.producto_id, i.gramos) ORDER BY r.nombre, i.id), '[]'::json)
                              FROM recetas r JOIN receta_items i ON i.receta_id = r.id WHERE r.activo),
              'nutri',     (SELECT COALESCE(json_agg(json_build_array(id, {cols})), '[]'::json) FROM productos)
            )""")
        d = cur.fetchone()[0]
    productos = [tuple(f) for f in d["productos"]]
    empaques = {(t, g): float(p) for t, g, p in d["empaques"]}
    nutri = {f[0]: [float(v or 0) for v in f[1:]] for f in d["nutri"]}
    return productos, d["tamanos"], empaques, [tuple(f) for f in d["recetas"]], nutri

# ================= Vista del cliente =================
def confirmacion(u):
    st.title("¡Pedido recibido! 🎉")
    st.success(f"Pedido #{u['id']} · {u['gramos']} g · Total {MONEDA}{u['total']:.2f}")
    for nom, g, sub in u["lineas"]:
        st.write(f"**{nom}** — {g} g · {MONEDA}{sub:.2f}")
    st.write(f"Empaque {u['empaque']} — {MONEDA}{u['precio_emp']:.2f}")
    if WHATSAPP:
        detalle = "\n".join(f"- {n}: {g} g" for n, g, _ in u["lineas"])
        msg = (f"Hola, soy {u['nombre']}. Pedido #{u['id']} (mix de {u['gramos']} g, "
               f"empaque {u['empaque']}):\n{detalle}\nTotal: {MONEDA}{u['total']:.2f}")
        st.link_button("Enviar por WhatsApp", f"https://wa.me/{WHATSAPP}?text={urllib.parse.quote(msg)}",
                       use_container_width=True, type="primary")
    if st.button("Armar otro mix", use_container_width=True):
        st.session_state.ultimo = None
        st.rerun()

@st.fragment
def armador(productos, tope, tipo, precio_empaque, nutri):
    st.subheader("Productos")
    if st.session_state.mix:
        st.button("🗑️ Vaciar mi mix", on_click=vaciar)

    for pid, emoji, nombre, precio_kg, stock in productos:
        gramos = st.session_state.mix.get(pid, 0)
        c1, c2, c3, c4 = st.columns([4, 1.3, 1.8, 1.3])
        c1.markdown(f"{emoji} **{nombre}**  \n{MONEDA}{float(precio_kg):.2f} /kg")
        c2.button("−", key=f"m{pid}", on_click=restar, args=(pid,), disabled=gramos == 0,
                  use_container_width=True)
        c3.markdown(f"**{gramos} g**")
        c4.button("+", key=f"p{pid}", on_click=sumar, args=(pid, stock),
                  disabled=total_usado() + PASO > tope or gramos + PASO > stock,
                  use_container_width=True)

    usado = total_usado()
    precios = {pid: float(pk) for pid, _, _, pk, _ in productos}
    subtotal_mix = sum(g * precios[pid] / 1000 for pid, g in st.session_state.mix.items() if pid in precios)
    total = subtotal_mix + precio_empaque

    st.divider()
    st.progress(usado / tope)
    if usado == tope:
        st.success(f"¡Mix completo! {usado} / {tope} g")
    else:
        st.write(f"**{usado} / {tope} g** · faltan {tope - usado} g")
    st.write(f"Mix: {MONEDA}{subtotal_mix:.2f} · Empaque {tipo}: {MONEDA}{precio_empaque:.2f}")
    st.subheader(f"Total: {MONEDA}{total:.2f}")
    if usado > 0:
        tabla_nutricional(st.session_state.mix, usado, nutri)

def vista_cliente():
    if st.session_state.get("ultimo"):
        return  confirmacion(st.session_state.ultimo)   
    try:
        productos, tamanos, empaques, filas_rec, nutri = catalogo()
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

    armador(productos, tope, tipo, precio_empaque, nutri)


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
