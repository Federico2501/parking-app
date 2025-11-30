import streamlit as st
import requests
import pandas as pd
import random
import uuid
from datetime import date, timedelta, datetime, time

st.set_page_config(
    page_title="Parking GLS",
    page_icon="Logo_GLS.png"
)


# ---------------------------------------------
# Utilidades conexión Supabase
# ---------------------------------------------
def get_rest_info():
    base_url = st.secrets["SUPABASE_URL"].rstrip("/")
    anon_key = st.secrets["SUPABASE_ANON_KEY"]

    rest_url = f"{base_url}/rest/v1"

    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    return rest_url, headers, anon_key

def se_puede_modificar_slot(fecha_slot: date, accion: str) -> bool:
    """
    Devuelve True si la acción está permitida según las reglas:
    
    accion = "reservar"  o  "cancelar"
    Reglas:
      - HOY:
           * reservar: permitido siempre
           * cancelar: NO permitido nunca
      - MAÑANA:
           * reservar/cancelar permitido solo antes de 20:00
      - FECHAS POSTERIORES A MAÑANA:
           * reservado/cancelado permitido siempre
    """
    hoy = date.today()
    ahora = datetime.now().time()
    limite = time(20, 0)

    # --- HOY ---
    if fecha_slot == hoy:
        if accion == "reservar":
            return True   # siempre permitido reservar hoy
        elif accion == "cancelar":
            return False  # prohibido cancelar hoy

    # --- MAÑANA ---
    if fecha_slot == hoy + timedelta(days=1):
        return ahora < limite

    # --- FUTURO (pasado mañana) ---
    return True

# ---------------------------------------------
# Sorteo de plazas     
# ---------------------------------------------

def ejecutar_sorteo(fecha_obj: date):
    """
    Ejecuta el sorteo para una fecha dada (normalmente mañana).

    Lógica:
      - Lee slots cedidos (owner_usa = false, reservado_por = null) para esa fecha.
      - Lee pre_reservas PENDIENTE para esa fecha.
      - Identifica:
          * PACKS de día completo: mismo usuario con M y T PENDIENTE ese día.
          * Solicitudes sueltas (solo M o solo T).
      - Cuenta usos mensuales de cada suplente (slots ya reservados en el mes de la fecha).
      - Reparte en este orden:
          1) PACKS (día completo) → necesitan 1 plaza en M y 1 en T a la vez.
          2) Solicitudes sueltas por franja (M / T).
      - Empate siempre se resuelve por menos usos en el mes + random.
      - Actualiza:
          * slots (reservado_por, es_sorteo = true),
          * pre_reservas → ASIGNADO / RECHAZADO.
    """
    rest_url, headers, _ = get_rest_info()
    fecha_str = fecha_obj.isoformat()

    # 1) Slots cedidos y libres de esa fecha
    try:
        resp_slots = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,plaza_id,owner_usa,reservado_por",
                "fecha": f"eq.{fecha_str}",
                "owner_usa": "eq.false",
                "reservado_por": "is.null",
            },
            timeout=10,
        )
        slots_libres = resp_slots.json() if resp_slots.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido leer los slots cedidos para el sorteo.")
        st.code(str(e))
        return

    if not slots_libres:
        st.info("No hay plazas cedidas y libres para esa fecha. No hay nada que sortear.")
        return

    # 2) Pre-reservas PENDIENTE para esa fecha
    try:
        resp_pre = requests.get(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "select": "id,usuario_id,franja,estado",
                "fecha": f"eq.{fecha_str}",
                "estado": "eq.PENDIENTE",
            },
            timeout=10,
        )
        pre_pendientes = resp_pre.json() if resp_pre.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido leer las pre-reservas para el sorteo.")
        st.code(str(e))
        return

    if not pre_pendientes:
        st.info("No hay pre-reservas pendientes para esa fecha. No hay nada que sortear.")
        return

    # 3) Contar usos mensuales por usuario (slots reservados en el mes de la fecha)
    first_day = fecha_obj.replace(day=1)
    if fecha_obj.month == 12:
        next_month_first = date(fecha_obj.year + 1, 1, 1)
    else:
        next_month_first = date(fecha_obj.year, fecha_obj.month + 1, 1)

    try:
        resp_usos = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,reservado_por",
                "fecha": f"gte.{first_day.isoformat()}",
            },
            timeout=10,
        )
        slots_mes_raw = resp_usos.json() if resp_usos.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido leer los usos del mes para el sorteo.")
        st.code(str(e))
        return

    usos_mes = {}
    for s in slots_mes_raw:
        try:
            uid = s["reservado_por"]
            if uid is None:
                continue
            fecha_slot = date.fromisoformat(s["fecha"][:10])
            if not (first_day <= fecha_slot < next_month_first):
                continue
            usos_mes[uid] = usos_mes.get(uid, 0) + 1
        except Exception:
            continue

    # 4) Agrupamos plazas libres por franja
    from collections import defaultdict

    plazas_por_franja = defaultdict(list)   # franja -> [plaza_id,...]
    for sl in slots_libres:
        fr = sl["franja"]
        plazas_por_franja[fr].append(sl["plaza_id"])

    # 5) Separar PACKS (día completo) de solicitudes sueltas
    #    Un PACK = mismo usuario con M y T PENDIENTE en esa fecha.
    by_user = defaultdict(list)
    for pr in pre_pendientes:
        by_user[pr["usuario_id"]].append(pr)

    packs = []  # cada elemento: {"usuario_id":..., "M":row, "T":row}
    singles_por_franja = {"M": [], "T": []}

    for uid, rows in by_user.items():
        franjas_user = {r["franja"] for r in rows}
        if franjas_user == {"M", "T"} and len(rows) == 2:
            # PACK de día completo
            pack_data = {"usuario_id": uid}
            for r in rows:
                pack_data[r["franja"]] = r
            packs.append(pack_data)
        else:
            # Solicitudes sueltas (puede haber 1 o varias, pero las tratamos independientes)
            for r in rows:
                fr = r["franja"]
                if fr in ("M", "T"):
                    singles_por_franja[fr].append(r)

    asignados_ids = []
    rechazados_ids = []

    # 6) Primero: sortear PACKS de día completo
    if packs:
        # Ordenar packs por (usos_mes asc, random)
        def clave_pack(p):
            uid = p["usuario_id"]
            u = usos_mes.get(uid, 0)
            return (u, random.random())

        packs_ordenados = sorted(packs, key=clave_pack)

        plazas_M = plazas_por_franja.get("M", [])
        plazas_T = plazas_por_franja.get("T", [])

        nuevos_plazas_M = list(plazas_M)
        nuevos_plazas_T = list(plazas_T)

        for pack in packs_ordenados:
            uid = pack["usuario_id"]
            row_M = pack.get("M")
            row_T = pack.get("T")

            # ¿Hay capacidad en ambas franjas?
            if nuevos_plazas_M and nuevos_plazas_T:
                plaza_M = nuevos_plazas_M.pop(0)
                plaza_T = nuevos_plazas_T.pop(0)

                # Asignamos slot mañana
                payload_M = [{
                    "fecha": fecha_str,
                    "plaza_id": plaza_M,
                    "franja": "M",
                    "owner_usa": False,
                    "reservado_por": uid,
                    "es_sorteo": True,
                    "estado": "CONFIRMADO",
                }]
                # Asignamos slot tarde
                payload_T = [{
                    "fecha": fecha_str,
                    "plaza_id": plaza_T,
                    "franja": "T",
                    "owner_usa": False,
                    "reservado_por": uid,
                    "es_sorteo": True,
                    "estado": "CONFIRMADO",
                }]

                try:
                    local_headers = headers.copy()
                    local_headers["Prefer"] = "resolution=merge-duplicates"

                    rM = requests.post(
                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                        headers=local_headers,
                        json=payload_M,
                        timeout=10,
                    )
                    if rM.status_code >= 400:
                        st.error("Error al asignar plaza de mañana a un pack.")
                        st.code(rM.text)
                        return

                    rT = requests.post(
                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                        headers=local_headers,
                        json=payload_T,
                        timeout=10,
                    )
                    if rT.status_code >= 400:
                        st.error("Error al asignar plaza de tarde a un pack.")
                        st.code(rT.text)
                        return

                    # Ambos pre_reservas del pack pasan a ASIGNADO
                    asignados_ids.append(row_M["id"])
                    asignados_ids.append(row_T["id"])

                    # Incrementamos usos del usuario (2 franjas más)
                    usos_mes[uid] = usos_mes.get(uid, 0) + 2

                except Exception as e:
                    st.error("Ha ocurrido un error al asignar un pack de día completo.")
                    st.code(str(e))
                    return

            else:
                # No hay capacidad en M o T → el pack pierde entero
                if "M" in pack and pack["M"]["id"] not in rechazados_ids:
                    rechazados_ids.append(pack["M"]["id"])
                if "T" in pack and pack["T"]["id"] not in rechazados_ids:
                    rechazados_ids.append(pack["T"]["id"])

        # Actualizamos plazas libres tras packs
        plazas_por_franja["M"] = nuevos_plazas_M
        plazas_por_franja["T"] = nuevos_plazas_T

    # 7) Segundo: sortear solicitudes sueltas, franja a franja
    for franja in ["M", "T"]:
        plazas = plazas_por_franja.get(franja, [])
        solicitudes = singles_por_franja.get(franja, [])

        if not plazas or not solicitudes:
            continue

        def clave_single(pr):
            uid = pr["usuario_id"]
            u = usos_mes.get(uid, 0)
            return (u, random.random())

        solicitudes_ordenadas = sorted(solicitudes, key=clave_single)

        num_asignables = min(len(plazas), len(solicitudes_ordenadas))
        plazas_disponibles = list(plazas)

        for i, pr in enumerate(solicitudes_ordenadas):
            uid = pr["usuario_id"]
            if i < num_asignables and plazas_disponibles:
                plaza_id = plazas_disponibles.pop(0)

                payload = [{
                    "fecha": fecha_str,
                    "plaza_id": plaza_id,
                    "franja": franja,
                    "owner_usa": False,
                    "reservado_por": uid,
                    "es_sorteo": True,
                    "estado": "CONFIRMADO",
                }]

                try:
                    local_headers = headers.copy()
                    local_headers["Prefer"] = "resolution=merge-duplicates"

                    r = requests.post(
                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                        headers=local_headers,
                        json=payload,
                        timeout=10,
                    )
                    if r.status_code >= 400:
                        st.error("Error al asignar una plaza en el sorteo.")
                        st.code(r.text)
                        return

                    asignados_ids.append(pr["id"])
                    usos_mes[uid] = usos_mes.get(uid, 0) + 1

                except Exception as e:
                    st.error("Ha ocurrido un error al asignar una solicitud suelta.")
                    st.code(str(e))
                    return
            else:
                rechazados_ids.append(pr["id"])

        # Guardamos las plazas restantes (por si queremos usarlas para debug)
        plazas_por_franja[franja] = plazas_disponibles

    # 8) Actualizar estados de pre_reservas en bloque
    try:
        if asignados_ids:
            ids_str = ",".join(asignados_ids)
            resp_patch_win = requests.patch(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={"id": f"in.({ids_str})"},
                json={"estado": "ASIGNADO"},
                timeout=10,
            )
            if resp_patch_win.status_code >= 400:
                st.error("Error al marcar pre-reservas como ASIGNADO.")
                st.code(resp_patch_win.text)
                return

        if rechazados_ids:
            ids_str = ",".join(rechazados_ids)
            resp_patch_lose = requests.patch(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={"id": f"in.({ids_str})"},
                json={"estado": "RECHAZADO"},
                timeout=10,
            )
            if resp_patch_lose.status_code >= 400:
                st.error("Error al marcar pre-reservas como RECHAZADO.")
                st.code(resp_patch_lose.text)
                return

    except Exception as e:
        st.error("Ha ocurrido un error al actualizar el estado de las pre-reservas.")
        st.code(str(e))
        return

    st.success(
        f"Sorteo ejecutado para el {fecha_obj.strftime('%d/%m/%Y')}: "
        f"{len(asignados_ids)} solicitudes ASIGNADAS, {len(rechazados_ids)} RECHAZADAS."
    )


def cancelar_sorteo(fecha_obj: date):
    """
    Revierte un sorteo ejecutado para una fecha:
      - Pone en PENDIENTE las pre_reservas con estado ASIGNADO/RECHAZADO para esa fecha.
      - Limpia en slots las reservas creadas por sorteo (es_sorteo = true).
    """
    rest_url, headers, _ = get_rest_info()
    fecha_str = fecha_obj.isoformat()

    # 1) Volver a PENDIENTE las pre_reservas ASIGNADO / RECHAZADO
    try:
        resp_patch_pre = requests.patch(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "fecha": f"eq.{fecha_str}",
                "estado": "in.(ASIGNADO,RECHAZADO)",
            },
            json={"estado": "PENDIENTE"},
            timeout=10,
        )
        if resp_patch_pre.status_code >= 400:
            st.error("Error al revertir el estado de pre-reservas en cancelar sorteo.")
            st.code(resp_patch_pre.text)
            return
    except Exception as e:
        st.error("No se han podido actualizar las pre-reservas al cancelar el sorteo.")
        st.code(str(e))
        return

    # 2) Quitar reservas creadas por sorteo en slots (es_sorteo = true)
    try:
        resp_patch_slots = requests.patch(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "fecha": f"eq.{fecha_str}",
                "es_sorteo": "eq.true",
            },
            json={
                "reservado_por": None,
                "es_sorteo": False,
            },
            timeout=10,
        )
        if resp_patch_slots.status_code >= 400:
            st.error("Error al limpiar los slots del sorteo al cancelar.")
            st.code(resp_patch_slots.text)
            return
    except Exception as e:
        st.error("No se han podido limpiar los slots al cancelar el sorteo.")
        st.code(str(e))
        return

    st.success(
        f"Sorteo CANCELADO para el {fecha_obj.strftime('%d/%m/%Y')}. "
        "Todas las solicitudes vuelven a estar PENDIENTES y las plazas liberadas."
    )

# ---------------------------------------------
# LOGIN via SUPABASE AUTH
# ---------------------------------------------
def login(email, password, anon_key):
    url = st.secrets["SUPABASE_URL"].rstrip("/") + "/auth/v1/token?grant_type=password"

    payload = {"email": email, "password": password}
    headers = {
        "apikey": anon_key,
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        return resp.json()  # tokens + user
    else:
        return None

# ---------------------------------------------
# Cargar perfil (rol, plaza) desde app_users
# ---------------------------------------------
def load_profile(user_id):
    rest_url, headers, _ = get_rest_info()

    resp = requests.get(
        f"{rest_url}/app_users",
        headers=headers,
        params={
            "select": "id,nombre,rol,plaza_id",
            "id": f"eq.{user_id}",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    if not data:
        return None

    return data[0]

# ---------------------------------------------
# Cambio contraseña usuario logeado       
# ---------------------------------------------

def password_change_panel():
    """Bloque de UI para que el usuario cambie su contraseña."""
    auth = st.session_state.get("auth")
    if not auth:
        return  # por si acaso

    user = auth["user"]
    email = user["email"]

    # Obtenemos anon_key para usar en llamadas a Auth
    _, _, anon_key = get_rest_info()
    access_token = auth["access_token"]

    with st.expander("Cambiar contraseña"):
        st.write(f"Usuario: **{email}**")

        current_pw = st.text_input(
            "Contraseña actual",
            type="password",
            key="pw_actual"
        )
        new_pw = st.text_input(
            "Nueva contraseña",
            type="password",
            key="pw_nueva"
        )
        new_pw2 = st.text_input(
            "Repite la nueva contraseña",
            type="password",
            key="pw_nueva2"
        )

        if st.button("Actualizar contraseña"):
            # Validaciones básicas
            if not current_pw or not new_pw or not new_pw2:
                st.error("Rellena todos los campos.")
                return
            if new_pw != new_pw2:
                st.error("Las nuevas contraseñas no coinciden.")
                return
            if len(new_pw) < 8:
                st.warning("Te recomiendo una contraseña de al menos 8 caracteres.")
                # seguimos, pero avisamos

            # 1) Verificar que la contraseña actual es correcta
            auth_test = login(email, current_pw, anon_key)
            if not auth_test:
                st.error("La contraseña actual no es correcta.")
                return

            # 2) Llamar a Supabase para cambiar la contraseña
            url = st.secrets["SUPABASE_URL"].rstrip("/") + "/auth/v1/user"
            headers = {
                "apikey": anon_key,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            payload = {"password": new_pw}

            try:
                resp = requests.put(url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    st.success("Contraseña actualizada correctamente ✅")
                else:
                    st.error("No se ha podido actualizar la contraseña.")
                    st.code(resp.text)
            except Exception as e:
                st.error("Error al conectar con el servidor de autenticación.")
                st.code(str(e))
# ---------------------------------------------
# Vistas por rol
# ---------------------------------------------
def view_admin(profile):
    st.subheader("Panel ADMIN")

    st.write(f"Nombre: {profile.get('nombre')}")

    rest_url, headers, _ = get_rest_info()

    # ---------------------------
    # 1) Cargar todos los usuarios
    # ---------------------------
    try:
        resp_users = requests.get(
            f"{rest_url}/app_users",
            headers=headers,
            params={"select": "id,nombre,rol,plaza_id"},
            timeout=10,
        )
        resp_users.raise_for_status()
        usuarios = resp_users.json()
    except Exception as e:
        st.error("No se han podido cargar los usuarios.")
        st.code(str(e))
        return

    # Mapas útiles
    id_to_nombre = {u["id"]: u["nombre"] for u in usuarios}
    plaza_to_titular = {
        u["plaza_id"]: u["nombre"]
        for u in usuarios
        if u.get("rol") == "TITULAR" and u.get("plaza_id") is not None
    }

    plazas_ids = sorted(plaza_to_titular.keys())

    # Contadores
    n_titulares = sum(1 for u in usuarios if u.get("rol") == "TITULAR")
    n_suplentes = sum(1 for u in usuarios if u.get("rol") == "SUPLENTE")
    n_admins    = sum(1 for u in usuarios if u.get("rol") == "ADMIN")
    plazas_totales = len(plazas_ids)

    st.markdown("### Resumen de usuarios / plazas")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Titulares", n_titulares)
    c2.metric("Suplentes", n_suplentes)
    c3.metric("Admins", n_admins)
    c4.metric("Plazas asignadas", plazas_totales)

    # ---------------------------
    # 2) Semana actual o siguiente según día (para KPIs / tablero)
    # ---------------------------
    hoy = date.today()
    weekday = hoy.weekday()  # 0 lunes .. 6 domingo

    lunes_actual = hoy - timedelta(days=weekday)
    semana_actual = [lunes_actual + timedelta(days=i) for i in range(5)]

    lunes_next = lunes_actual + timedelta(days=7)
    semana_siguiente = [lunes_next + timedelta(days=i) for i in range(5)]

    # Lógica ADMIN:
    #   - Lunes a jueves -> se muestra semana actual
    #   - Viernes, sábado, domingo -> se muestra semana siguiente
    if weekday <= 3:  
        dias_semana = semana_actual
    else:            
        dias_semana = semana_siguiente

    fecha_min = dias_semana[0]
    fecha_max = dias_semana[-1]

    # ---------------------------
    # 3) Cargar TODOS los slots (histórico completo)
    # ---------------------------
    try:
        resp_slots = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={"select": "fecha,franja,plaza_id,owner_usa,reservado_por"},
            timeout=10,
        )
        resp_slots.raise_for_status()
        slots_raw = resp_slots.json()
    except Exception as e:
        st.error("No se han podido cargar los slots.")
        st.code(str(e))
        return

    # Normalizar TODOS los slots (histórico)
    slots_all = []
    for s in slots_raw:
        try:
            f = date.fromisoformat(s["fecha"][:10])
        except Exception:
            continue
        slots_all.append({
            "fecha": f,
            "franja": s["franja"],
            "plaza_id": s["plaza_id"],
            "owner_usa": s["owner_usa"],
            "reservado_por": s["reservado_por"],
        })

    # Para KPIs/tablero: solo la semana visible (actual o siguiente)
    slots_semana = [
        s for s in slots_all
        if fecha_min <= s["fecha"] <= fecha_max
    ]

    # ---------------------------
    # 4) KPIs semana visible
    # ---------------------------
    cedidos = [s for s in slots_semana if s["owner_usa"] is False]
    reservados = [s for s in cedidos if s["reservado_por"] is not None]
    libres = [s for s in cedidos if s["reservado_por"] is None]

    st.markdown("### Semana visible")

    c1, c2, c3 = st.columns(3)
    c1.metric("Franjas cedidas", len(cedidos))
    c2.metric("Cedidas y reservadas", len(reservados))
    c3.metric("Cedidas libres", len(libres))

    # ---------------------------
    # 5) Tablero visual (solo semana visible)
    # ---------------------------
    st.markdown("### Ocupación por día (tablero 100 plazas)")

    dia_seleccionado = st.selectbox(
        "Selecciona día",
        options=dias_semana,
        format_func=lambda d: d.strftime("%a %d/%m")
    )

    plazas_stats = {pid: {"libres": 0, "ocupadas": 0} for pid in plazas_ids}

    for s in slots_semana:
        if s["fecha"] != dia_seleccionado:
            continue
        pid = s["plaza_id"]

        if s["owner_usa"] is False and s["reservado_por"] is None:
            plazas_stats[pid]["libres"] += 1
        else:
            plazas_stats[pid]["ocupadas"] += 1

    # Ajuste: si falta un registro, contarlo como ocupado (2 franjas posibles)
    for pid, stx in plazas_stats.items():
        tot = stx["libres"] + stx["ocupadas"]
        if tot < 2:
            stx["ocupadas"] += (2 - tot)

    rows, cols = 5, 10
    for i in range(rows):
        ccols = st.columns(cols)
        for j in range(cols):
            idx = i * cols + j
            if idx >= len(plazas_ids):
                ccols[j].markdown(
                    "<div style='text-align:center;color:#bbb'>⬜️</div>",
                    unsafe_allow_html=True
                )
                continue

            pid = plazas_ids[idx]
            libres = plazas_stats[pid]["libres"]

            color = "🟩" if libres == 2 else ("🟦" if libres == 1 else "🟥")

            html = f"""
            <div style='text-align:center;font-size:24px;'>
                {color}<br/>
                <span style='font-size:12px;'>P-{pid}</span>
            </div>
            """
            ccols[j].markdown(html, unsafe_allow_html=True)

    # ---------------------------
    # 6) Tabla detalle HISTÓRICA con Mes/Año
    #    (usa TODOS los slots, no solo la semana visible)
    # ---------------------------
    st.markdown("### Detalle de slots")

    filas = []
    for s in sorted(slots_all, key=lambda x: (x["fecha"], x["franja"], x["plaza_id"])):
        franja_txt = "08 - 14" if s["franja"] == "M" else "14 - 20"

        titular = plaza_to_titular.get(s["plaza_id"], "-")
        suplente = id_to_nombre.get(s["reservado_por"], "-") if s["reservado_por"] else "-"

        if s["owner_usa"] and not s["reservado_por"]:
            estado = "Titular usa"
        elif not s["owner_usa"] and s["reservado_por"] is None:
            estado = "Cedido (libre)"
        elif not s["owner_usa"] and s["reservado_por"] is not None:
            estado = f"Cedido y reservado por {suplente}"
        else:
            estado = "Inconsistente"

        filas.append({
            "Fecha": s["fecha"].strftime("%d/%m/%Y"),
            "Mes/Año": s["fecha"].strftime("%m/%Y"),
            "Franja": franja_txt,
            "Plaza": f"P-{s['plaza_id']}",
            "Titular": titular,
            "Suplente": suplente,
            "Estado": estado,
        })

    if filas:
        df = pd.DataFrame(filas)

        c1, c2, c3, c4 = st.columns(4)

        # Filtro Mes/Año
        sel_mes = c1.multiselect(
            "Mes/Año",
            options=sorted(df["Mes/Año"].unique()),
            default=sorted(df["Mes/Año"].unique())
        )

        # Filtro Fecha
        fechas_sorted = sorted(
            df["Fecha"].unique(),
            key=lambda x: datetime.strptime(x, "%d/%m/%Y")
        )
        sel_fecha = c2.multiselect("Fecha", fechas_sorted, fechas_sorted)

        # Filtro Plaza
        plazas_sorted = sorted(df["Plaza"].unique())
        sel_plaza = c3.multiselect("Plaza", plazas_sorted, plazas_sorted)

        # Filtro Franja
        franjas_sorted = sorted(df["Franja"].unique())
        sel_franja = c4.multiselect("Turno", franjas_sorted, franjas_sorted)

        df_f = df[
            df["Mes/Año"].isin(sel_mes)
            & df["Fecha"].isin(sel_fecha)
            & df["Plaza"].isin(sel_plaza)
            & df["Franja"].isin(sel_franja)
        ]

        st.dataframe(df_f, use_container_width=True)
    else:
        st.info("No hay datos disponibles.")

    # ---------------------------
    # 7) Sorteo pre-reservas (tu bloque intacto a partir de aquí)
    # ---------------------------
    st.markdown("### 🎲 Sorteo de plazas (ADMIN)")
    fecha_por_defecto = hoy + timedelta(days=1)

    fecha_sorteo = st.date_input(
        "Fecha para ejecutar / reiniciar el sorteo",
        value=fecha_por_defecto,
        min_value=hoy,
        max_value=hoy + timedelta(days=30),
        key="fecha_sorteo_admin",
    )

    col_sorteo, col_reset = st.columns(2)
    # A partir de aquí mantienes exactamente tu lógica actual de sorteo



def view_titular(profile):
    st.subheader("Panel TITULAR")

    nombre = profile.get("nombre")
    plaza_id = profile.get("plaza_id")

    st.write(f"Nombre: {nombre}")

    if not plaza_id:
        st.error("Aún no tienes plaza asignada en app_users (plaza_id es NULL).")
        return

    st.write(f"Tu plaza asignada: **P-{plaza_id}**")
    st.markdown("Marca qué franjas **cedes** tu plaza:")

    # ---------------------------
    # Helper: ¿puede modificar la cesión?
    # ---------------------------
    def se_puede_modificar_cesion(fecha_slot: date) -> bool:
        hoy = date.today()
        ahora = datetime.now().time()
        limite = time(20, 0)

        if fecha_slot == hoy:
            return False
        if fecha_slot == hoy + timedelta(days=1) and ahora >= limite:
            return False
        return True

    # ---------------------------
    # NUEVA LÓGICA DE SEMANAS
    # ---------------------------
    hoy = date.today()
    weekday = hoy.weekday()  # 0=lunes..6=domingo

    # Semana actual (lun→vie)
    lunes_actual = hoy - timedelta(days=weekday)
    semana_actual = [lunes_actual + timedelta(days=i) for i in range(5)]

    # Semana siguiente (lun→vie)
    lunes_siguiente = lunes_actual + timedelta(days=7)
    semana_siguiente = [lunes_siguiente + timedelta(days=i) for i in range(5)]

    # Reglas:
    if weekday <= 3:
        # Lunes→jueves → solo mostrar semana actual desde hoy
        dias_semana = [d for d in semana_actual if d >= hoy]
    elif weekday == 4:
        # Viernes → mostrar viernes + toda semana siguiente
        dias_semana = [hoy] + semana_siguiente
    else:
        # Sábado o domingo → solo semana siguiente
        dias_semana = semana_siguiente

    if not dias_semana:
        st.info("No hay días disponibles para mostrar.")
        return

    rest_url, headers, _ = get_rest_info()

    # Leer slots de esta plaza (todas fechas)
    try:
        resp = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,owner_usa,reservado_por",
                "plaza_id": f"eq.{plaza_id}",
            },
            timeout=10,
        )
        slots = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        st.error("No se ha podido leer el estado actual de la plaza.")
        st.code(str(e))
        slots = []

    # Mapeos
    estado = {}
    reservas = {}
    for s in slots:
        try:
            f = date.fromisoformat(s["fecha"][:10])
            fr = s["franja"]
            estado[(f, fr)] = s["owner_usa"]
            reservas[(f, fr)] = s["reservado_por"]
        except Exception:
            continue

    # ---------------------------
    # INTERFAZ PRINCIPAL
    # ---------------------------
    st.markdown("### Disponibilidad")

    header_cols = st.columns(4)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**08 - 14**")
    header_cols[2].markdown("**14 - 20**")
    header_cols[3].markdown("**Día completo**")

    cedencias = {}

    for d in dias_semana:
        col_dia, col_m, col_t, col_full = st.columns(4)
        col_dia.write(d.strftime("%a %d/%m"))

        editable = se_puede_modificar_cesion(d)

        owner_usa_M = estado.get((d, "M"), True)
        owner_usa_T = estado.get((d, "T"), True)
        reservado_M = reservas.get((d, "M"))
        reservado_T = reservas.get((d, "T"))

        cedida_M = not owner_usa_M
        cedida_T = not owner_usa_T

        # Caso 1: NO editable
        if not editable:
            # Mañana
            if reservado_M is not None:
                col_m.markdown("✅ Cedida (reservada)")
                cedida_M = True
            else:
                col_m.markdown("Cedida (libre)" if cedida_M else "Titular usa")

            # Tarde
            if reservado_T is not None:
                col_t.markdown("✅ Cedida (reservada)")
                cedida_T = True
            else:
                col_t.markdown("Cedida (libre)" if cedida_T else "Titular usa")

            col_full.markdown("—")

        # Caso 2: editable y sin reservas
        elif reservado_M is None and reservado_T is None:
            default_M = cedida_M
            default_T = cedida_T
            full_default = default_M and default_T

            # Checkbox día completo
            full_checked = col_full.checkbox(
                "Ceder día completo",
                value=full_default,
                key=f"cede_full_{d.isoformat()}"
            )

            if full_checked:
                col_m.markdown("_Incluida en día completo_")
                col_t.markdown("_Incluida en día completo_")
                cedida_M = True
                cedida_T = True
            else:
                cedida_M = col_m.checkbox(
                    "Cedo",
                    value=default_M,
                    key=f"cede_{d.isoformat()}_M"
                )
                cedida_T = col_t.checkbox(
                    "Cedo",
                    value=default_T,
                    key=f"cede_{d.isoformat()}_T"
                )

        # Caso 3: editable pero hay reservas
        else:
            col_full.markdown("—")

            if reservado_M is not None:
                col_m.markdown("✔ Cedida (reservada)")
                cedida_M = True
            else:
                cedida_M = col_m.checkbox(
                    "Cedo",
                    value=cedida_M,
                    key=f"cede_{d.isoformat()}_M"
                )

            if reservado_T is not None:
                col_t.markdown("✔ Cedida (reservada)")
                cedida_T = True
            else:
                cedida_T = col_t.checkbox(
                    "Cedo",
                    value=cedida_T,
                    key=f"cede_{d.isoformat()}_T"
                )

        cedencias[(d, "M")] = cedida_M
        cedencias[(d, "T")] = cedida_T

    # ---------------------------
    # GUARDAR CAMBIOS
    # ---------------------------
    st.markdown("---")
    if st.button("Guardar cambios"):
        try:
            for (d, fr), cedida in cedencias.items():
                if not se_puede_modificar_cesion(d):
                    continue
                owner_usa = not cedida

                payload = [{
                    "fecha": d.isoformat(),
                    "plaza_id": plaza_id,
                    "franja": fr,
                    "owner_usa": owner_usa,
                    "estado": "CONFIRMADO",
                }]

                local_headers = headers.copy()
                local_headers["Prefer"] = "resolution=merge-duplicates"

                r = requests.post(
                    f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                    headers=local_headers,
                    json=payload,
                    timeout=10,
                )
                r.raise_for_status()

            st.success("Disponibilidad actualizada correctamente.")
        except Exception as e:
            st.error("Error al guardar la disponibilidad.")
            st.code(str(e))


def view_suplente(profile):
    st.subheader("Panel SUPLENTE")

    nombre = profile.get("nombre")
    st.write(f"Nombre: {nombre}")

    # user_id
    auth = st.session_state.get("auth")
    if not auth:
        st.error("No se ha podido obtener información de usuario.")
        return
    user_id = auth["user"]["id"]

    rest_url, headers, _ = get_rest_info()
    hoy = date.today()

    # ============================
    # 1) KPI de uso mensual
    # ============================
    first_day = hoy.replace(day=1)
    next_month_first = (first_day.replace(month=first_day.month % 12 + 1, year=first_day.year + (first_day.month // 12))
                        if first_day.month == 12 else
                        date(first_day.year, first_day.month + 1, 1))

    try:
        r = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha",
                "reservado_por": f"eq.{user_id}",
                "fecha": f"gte.{first_day.isoformat()}",
            },
            timeout=10,
        )
        usadas_raw = r.json()
    except:
        usadas_raw = []

    usadas_mes = []
    for x in usadas_raw:
        try:
            f = date.fromisoformat(x["fecha"][:10])
            if first_day <= f < next_month_first:
                usadas_mes.append(f)
        except:
            pass

    st.write(f"Franjas utilizadas este mes: **{len(usadas_mes)}**")

    # ============================
    # 2) Próximas reservas
    # ============================
    try:
        r = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,plaza_id",
                "reservado_por": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc"
            },
            timeout=10,
        )
        slots_user_raw = r.json()
    except:
        slots_user_raw = []

    slots_user = {}
    for s in slots_user_raw:
        try:
            f = date.fromisoformat(s["fecha"][:10])
            slots_user[(f, s["franja"])] = s["plaza_id"]
        except:
            pass

    try:
        r = requests.get(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "select": "fecha,franja,estado",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc"
            },
            timeout=10,
        )
        pre_user_raw = r.json()
    except:
        pre_user_raw = []

    pre_user = {}
    for r in pre_user_raw:
        try:
            if r["estado"] != "CANCELADO":
                f = date.fromisoformat(r["fecha"][:10])
                pre_user[(f, r["franja"])] = r["estado"]
        except:
            pass

    claves = sorted(set(slots_user.keys()) | set(pre_user.keys()), key=lambda x: (x[0], x[1]))

    st.markdown("### 🔜 Tus próximas reservas / solicitudes")
    if not claves:
        st.markdown("_No tienes reservas ni solicitudes futuras._")
    else:
        out = []
        for (f, franja) in claves:
            fr_txt = "08 - 14" if franja == "M" else "14 - 20"
            fecha_txt = f.strftime("%a %d/%m")

            if (f, franja) in slots_user:
                plaza = slots_user[(f, franja)]
                out.append(f"- {fecha_txt} – {fr_txt} – Plaza **P-{plaza}** (asignada)")
            else:
                est = pre_user.get((f, franja), "PENDIENTE")
                if est == "PENDIENTE":
                    out.append(f"- {fecha_txt} – {fr_txt} – _Solicitud pendiente de plaza_")
                elif est == "ASIGNADO":
                    out.append(f"- {fecha_txt} – {fr_txt} – _Plaza asignada (pendiente)_")
                elif est == "RECHAZADO":
                    out.append(f"- {fecha_txt} – {fr_txt} – _Solicitud no aprobada_")

        st.markdown("\n".join(out))

    # ============================
    # 3) Semana inteligente
    # ============================
    weekday = hoy.weekday()  # 0 lunes ... 6 domingo

    lunes_actual = hoy - timedelta(days=weekday)
    semana_actual = [lunes_actual + timedelta(days=i) for i in range(5)]

    lunes_next = lunes_actual + timedelta(days=7)
    semana_next = [lunes_next + timedelta(days=i) for i in range(5)]

    if weekday <= 3:
        dias_semana = [d for d in semana_actual if d >= hoy]
    elif weekday == 4:
        dias_semana = [hoy] + semana_next
    else:
        dias_semana = semana_next

    if not dias_semana:
        st.info("No hay días disponibles.")
        return

    # ============================
    # 4) Leer slots agregados
    # ============================
    try:
        r = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={"select": "fecha,franja,owner_usa,reservado_por,plaza_id"},
            timeout=10,
        )
        slots_raw = r.json()
    except:
        slots_raw = []

    from collections import defaultdict
    libres = defaultdict(int)
    reservas_user_sem = {}

    for s in slots_raw:
        try:
            f = date.fromisoformat(s["fecha"][:10])
        except:
            continue

        if f not in dias_semana:
            continue

        fr = s["franja"]

        if s["reservado_por"] == user_id:
            reservas_user_sem[(f, fr)] = s["plaza_id"]

        if s["owner_usa"] is False and s["reservado_por"] is None:
            libres[(f, fr)] += 1

    # ============================
    # 5) Pre-reservas de semana
    # ============================
    try:
        r = requests.get(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "select": "fecha,franja,estado,pack_id",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"in.({','.join(d.isoformat() for d in dias_semana)})",
                "order": "fecha.asc,franja.asc"
            },
            timeout=10,
        )
        pre_sem_raw = r.json()
    except:
        pre_sem_raw = []

    pre_sem = {}
    pack_ids = {}
    for row in pre_sem_raw:
        try:
            if row["estado"] != "CANCELADO":
                f = date.fromisoformat(row["fecha"][:10])
                fr = row["franja"]
                pre_sem[(f, fr)] = row["estado"]
                if row.get("pack_id"):
                    pack_ids[(f, fr)] = row["pack_id"]
        except:
            pass

    # ============================
    # 6) UI tipo TITULAR (checkboxes + día completo)
    # ============================
    st.markdown("### Selecciona las franjas que deseas solicitar:")

    header = st.columns(4)
    header[0].markdown("**Día**")
    header[1].markdown("**08 - 14**")
    header[2].markdown("**14 - 20**")
    header[3].markdown("**Día completo**")

    # Estado editable local
    cambios = {}

    for d in dias_semana:
        is_today = (d == hoy)
        bg = "background-color:rgba(0,123,255,0.08); border-radius:6px; padding:3px;" if is_today else ""

        with st.container():
            st.markdown(f"<div style='{bg}'>", unsafe_allow_html=True)
            cols = st.columns(4)
            cols[0].write(d.strftime("%a %d/%m"))

            # Estado BD actual
            slot_M = reservas_user_sem.get((d, "M"))
            slot_T = reservas_user_sem.get((d, "T"))

            pre_M = pre_sem.get((d, "M"))
            pre_T = pre_sem.get((d, "T"))

            tiene_slot_M = slot_M is not None
            tiene_slot_T = slot_T is not None

            # FULL DAY adjudicado
            adjud_full = tiene_slot_M and tiene_slot_T

            # FULL DAY pendiente
            full_pendiente = (
                not tiene_slot_M and not tiene_slot_T and
                pre_M == "PENDIENTE" and pre_T == "PENDIENTE"
            )

            # Puede modificar este día?
            editable = se_puede_modificar_slot(d, "reservar")

            # ----------------------------
            # Día completo checkbox
            # ----------------------------
            key_full = f"full_{d.isoformat()}"
            default_full = (pre_M is not None and pre_T is not None and pre_M != "RECHAZADO" and pre_T != "RECHAZADO" and not tiene_slot_M and not tiene_slot_T)

            if adjud_full:
                cols[3].markdown("🟩 Día completo adjudicado")
                cambios[(d, "FULL")] = "NOACCION"
                # Franjas individuales no son editables
                cols[1].markdown("—")
                cols[2].markdown("—")
            else:
                if editable:
                    full_checked = cols[3].checkbox(
                        "Día completo",
                        value=default_full,
                        key=key_full
                    )
                else:
                    full_checked = default_full
                    cols[3].markdown("—")

                if full_checked:
                    # Ocultar franjas
                    cols[1].markdown("_Incluida en día completo_")
                    cols[2].markdown("_Incluida en día completo_")
                    cambios[(d, "FULL")] = "SOLICITAR"
                else:
                    cambios[(d, "FULL")] = "NOFULL"

                    # ----------------------------
                    # Checkboxes de franjas
                    # ----------------------------
                    for fr_idx, fr in enumerate(["M", "T"], start=1):
                        col = cols[fr_idx]
                        key = (d, fr)

                        if (d, fr) in reservas_user_sem:
                            plaza = reservas_user_sem[(d, fr)]
                            col.markdown(f"🟩 Adjudicada P-{plaza}")
                            cambios[(d, fr)] = "NOACCION"
                            continue

                        estado_pre = pre_sem.get(key)
                        if estado_pre == "PENDIENTE":
                            marc = True
                        elif estado_pre == "ASIGNADO":
                            marc = True
                        else:
                            marc = False

                        if editable:
                            marc = col.checkbox(
                                "Solicitar",
                                value=marc,
                                key=f"chk_{d.isoformat()}_{fr}"
                            )
                        else:
                            col.markdown("—")

                        cambios[(d, fr)] = "SOLICITAR" if marc else "NOACCION"

            st.markdown("</div>", unsafe_allow_html=True)

    # ============================
    # 7) Botón GUARDAR CAMBIOS
    # ============================
    st.markdown("---")
    if st.button("💾 Guardar cambios"):
        try:
            # Recorremos DIAS y franjas
            for (d, fr) in cambios:

                accion = cambios[(d, fr)]

                # PACK completo
                if fr == "FULL":
                    if accion == "SOLICITAR":
                        # Crear pack_id
                        pack_id = str(uuid.uuid4())
                        payload = [
                            {
                                "usuario_id": user_id,
                                "fecha": d.isoformat(),
                                "franja": "M",
                                "estado": "PENDIENTE",
                                "pack_id": pack_id,
                            },
                            {
                                "usuario_id": user_id,
                                "fecha": d.isoformat(),
                                "franja": "T",
                                "estado": "PENDIENTE",
                                "pack_id": pack_id,
                            },
                        ]
                        requests.post(
                            f"{rest_url}/pre_reservas",
                            headers=headers,
                            json=payload,
                            timeout=10,
                        )
                    continue

                # Franja suelta
                f = fr
                esta_pre = (d, f) in pre_sem
                esta_slot = (d, f) in reservas_user_sem

                if accion == "SOLICITAR":
                    if not esta_pre and not esta_slot:
                        # Crear pre_reserva
                        payload = [{
                            "usuario_id": user_id,
                            "fecha": d.isoformat(),
                            "franja": f,
                            "estado": "PENDIENTE",
                        }]
                        requests.post(
                            f"{rest_url}/pre_reservas",
                            headers=headers,
                            json=payload,
                            timeout=10,
                        )

                elif accion == "NOACCION":
                    # Si existe pre_reserva → CANCELAR
                    if esta_pre:
                        requests.patch(
                            f"{rest_url}/pre_reservas",
                            headers=headers,
                            params={
                                "usuario_id": f"eq.{user_id}",
                                "fecha": f"eq.{d.isoformat()}",
                                "franja": f"eq.{f}",
                                "estado": "in.(PENDIENTE,ASIGNADO)"
                            },
                            json={"estado": "CANCELADO"},
                            timeout=10,
                        )

            st.success("Cambios guardados correctamente.")
            st.rerun()

        except Exception as e:
            st.error("Error al guardar cambios.")
            st.code(str(e))
            return
# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    st.image("Logo_GLS.png", width=200)
    st.title("Parking GLS KM0")

    rest_url, headers, anon_key = get_rest_info()

    # Estado de sesión
    if "auth" not in st.session_state:
        st.session_state.auth = None  # info de Supabase Auth
    if "profile" not in st.session_state:
        st.session_state.profile = None  # fila en app_users

    # Si NO hay sesión → formulario login
    if st.session_state.auth is None:
        st.subheader("Iniciar sesión")

        email = st.text_input("Email")
        password = st.text_input("Contraseña", type="password")

        if st.button("Entrar"):
            auth_data = login(email, password, anon_key)
            if auth_data:
                st.session_state.auth = auth_data
                st.success("Login correcto, cargando perfil…")
                st.rerun()
            else:
                st.error("Email o contraseña incorrectos")
        return

    # Ya hay sesión → coger user_id
    user = st.session_state.auth["user"]
    user_id = user["id"]
    email = user["email"]

    # Cargar perfil si aún no está
    if st.session_state.profile is None:
        profile = load_profile(user_id)
        if profile is None:
            st.error("No se ha encontrado un perfil en app_users para este usuario.")
            st.info("Da de alta este usuario en la tabla app_users (con rol y plaza_id) y recarga.")
            if st.button("Cerrar sesión"):
                st.session_state.auth = None
                st.session_state.profile = None
                st.rerun()
            return
        st.session_state.profile = profile

    profile = st.session_state.profile

    # Cabecera común
    st.success(f"Sesión iniciada como: {email}")
    st.write(f"Rol: **{profile['rol']}**")

    if st.button("Cerrar sesión"):
        st.session_state.auth = None
        st.session_state.profile = None
        st.rerun()

    # Bloque para cambiar la contraseña del usuario logueado
    password_change_panel()

    st.markdown("---")

    
    # Vista según rol
    rol = profile["rol"]
    if rol == "ADMIN":
        view_admin(profile)
    elif rol == "TITULAR":
        view_titular(profile)
    elif rol == "SUPLENTE":
        view_suplente(profile)
    else:
        st.error(f"Rol desconocido: {rol}")

if __name__ == "__main__":
    main()
