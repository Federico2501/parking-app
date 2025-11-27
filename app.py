import streamlit as st
import requests
import pandas as pd
import random
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
    # 2) Semana actual (lunes-viernes)
    # ---------------------------
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    dias_semana = [lunes + timedelta(days=i) for i in range(5)]
    fin_semana = dias_semana[-1]

    # Cargar slots de esta semana (de todas las plazas)
    try:
        resp_slots = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,plaza_id,owner_usa,reservado_por",
                "fecha": f"gte.{lunes.isoformat()}",
            },
            timeout=10,
        )
        resp_slots.raise_for_status()
        slots_raw = resp_slots.json()
    except Exception as e:
        st.error("No se han podido cargar los slots de esta semana.")
        st.code(str(e))
        return

    # Normalizamos y filtramos solo la semana (por si hay más fechas)
    slots = []
    for s in slots_raw:
        try:
            fecha_str = s["fecha"][:10]
            f = date.fromisoformat(fecha_str)
        except Exception:
            continue
        if not (lunes <= f <= fin_semana):
            continue
        slots.append({
            "fecha": f,
            "franja": s["franja"],
            "plaza_id": s["plaza_id"],
            "owner_usa": s["owner_usa"],
            "reservado_por": s["reservado_por"],
        })

    # KPIs de la semana
    cedidos = [s for s in slots if s["owner_usa"] is False]
    reservados = [s for s in cedidos if s["reservado_por"] is not None]
    libres = [s for s in cedidos if s["reservado_por"] is None]

    st.markdown("### Semana actual (lu–vi)")

    c1, c2, c3 = st.columns(3)
    c1.metric("Franjas cedidas", len(cedidos))
    c2.metric("Cedidas y reservadas", len(reservados))
    c3.metric("Cedidas libres", len(libres))

    # ---------------------------
    # 3) Tablero visual de plazas con selector de día
    # ---------------------------
    st.markdown("### Ocupación por día (tablero 100 plazas)")

    dia_seleccionado = st.selectbox(
        "Selecciona día de la semana",
        options=dias_semana,
        format_func=lambda d: d.strftime("%a %d/%m"),
    )

    # Por plaza: contamos franjas libres / ocupadas en el día seleccionado
    plazas_stats = {
        pid: {"libres": 0, "ocupadas": 0}
        for pid in plazas_ids
    }

    for s in slots:
        if s["fecha"] != dia_seleccionado:
            continue
        pid = s["plaza_id"]
        if pid not in plazas_stats:
            continue

        owner_usa = s["owner_usa"]
        reservado_por = s["reservado_por"]

        # Libre = cedida por titular y sin reserva
        if owner_usa is False and reservado_por is None:
            plazas_stats[pid]["libres"] += 1
        else:
            plazas_stats[pid]["ocupadas"] += 1

    # Ajuste: franjas que no tienen registro las consideramos ocupadas
    # (2 franjas posibles por día: mañana y tarde)
    for pid, stats in plazas_stats.items():
        total_registradas = stats["libres"] + stats["ocupadas"]
        if total_registradas < 2:
            faltan = 2 - total_registradas
            stats["ocupadas"] += faltan

    # Grid 5x10 = 50 casillas
    rows = 5
    cols = 10

    for i in range(rows):
        cols_streamlit = st.columns(cols)
        for j in range(cols):
            idx = i * cols + j
            if idx < len(plazas_ids):
                pid = plazas_ids[idx]
                stats = plazas_stats.get(pid, {"libres": 0, "ocupadas": 2})
                libres = stats["libres"]

                # Verde: las 2 franjas libres
                # Azul: solo 1 libre
                # Rojo: 0 libres
                if libres == 2:
                    color = "🟩"
                elif libres == 1:
                    color = "🟦"
                else:
                    color = "🟥"

                html = f"""
                <div style='text-align:center; font-size:24px;'>
                    {color}<br/>
                    <span style='font-size:12px;'>P-{pid}</span>
                </div>
                """
                cols_streamlit[j].markdown(html, unsafe_allow_html=True)
            else:
                html = """
                <div style='text-align:center; font-size:24px; color:#bbbbbb;'>
                    ⬜️
                </div>
                """
                cols_streamlit[j].markdown(html, unsafe_allow_html=True)
    # ---------------------------
    # 4) Tabla detalle de la semana
    # ---------------------------
    filas = []
    for s in sorted(slots, key=lambda x: (x["fecha"], x["franja"], x["plaza_id"])):
        fecha = s["fecha"]
        franja = "Mañana" if s["franja"] == "M" else "Tarde"
        plaza_id = s["plaza_id"]
        owner_usa = s["owner_usa"]
        reservado_por = s["reservado_por"]

        titular = plaza_to_titular.get(plaza_id, "-")
        suplente = id_to_nombre.get(reservado_por, "-") if reservado_por else "-"

        if owner_usa and not reservado_por:
            estado = "Titular usa"
        elif not owner_usa and reservado_por is None:
            estado = "Cedido (libre)"
        elif not owner_usa and reservado_por is not None:
            estado = f"Cedido y reservado por {suplente}"
        else:
            estado = "Inconsistente"

        filas.append({
            "Fecha": fecha.strftime("%d/%m/%Y"),
            "Franja": franja,
            "Plaza": f"P-{plaza_id}",
            "Titular": titular,
            "Suplente": suplente,
            "Estado": estado,
        })

    st.markdown("### Detalle de slots semana actual")

    if filas:
        df = pd.DataFrame(filas)

        # ---- Filtros ----
        c1, c2, c3 = st.columns(3)

        # Filtro por fecha
        try:
            unique_fechas = sorted(
                df["Fecha"].unique(),
                key=lambda s: datetime.strptime(s, "%d/%m/%Y")
            )
        except Exception:
            unique_fechas = sorted(df["Fecha"].unique())

        sel_fechas = c1.multiselect(
            "Fecha",
            options=unique_fechas,
            default=unique_fechas,
        )

        # Filtro por plaza
        unique_plazas = sorted(df["Plaza"].unique())
        sel_plazas = c2.multiselect(
            "Plaza",
            options=unique_plazas,
            default=unique_plazas,
        )

        # Filtro por turno/franja
        unique_franjas = sorted(df["Franja"].unique())
        sel_franjas = c3.multiselect(
            "Turno",
            options=unique_franjas,
            default=unique_franjas,
        )

        # Aplicar filtros
        df_filtrado = df[
            df["Fecha"].isin(sel_fechas)
            & df["Plaza"].isin(sel_plazas)
            & df["Franja"].isin(sel_franjas)
        ]

        st.dataframe(df_filtrado, use_container_width=True)

    else:
        st.info("No hay slots registrados para esta semana.")

    # ---------------------------
    # 5) Sorteo de pre-reservas
    # ---------------------------
    st.markdown("### 🎲 Sorteo de plazas (ADMIN)")

    hoy = date.today()
    fecha_por_defecto = hoy + timedelta(days=1)

    fecha_sorteo = st.date_input(
        "Fecha para ejecutar / reiniciar el sorteo",
        value=fecha_por_defecto,
        min_value=hoy,
        max_value=hoy + timedelta(days=30),
        key="fecha_sorteo_admin",
    )

    col_sorteo, col_reset = st.columns(2)

    # ---------- BOTÓN 1: EJECUTAR SORTEO ----------
    if col_sorteo.button("Ejecutar sorteo para esta fecha"):
        try:
            # 1) Cargar pre_reservas PENDIENTES para esa fecha
            resp_pre = requests.get(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={
                    "select": "id,usuario_id,franja",
                    "fecha": f"eq.{fecha_sorteo.isoformat()}",
                    "estado": "eq.PENDIENTE",
                },
                timeout=15,
            )
            if resp_pre.status_code != 200:
                st.error("Error al leer pre_reservas pendientes.")
                st.code(resp_pre.text)
                return

            pre_pendientes = resp_pre.json()
            if not pre_pendientes:
                st.info("No hay pre-reservas pendientes para esa fecha.")
                return

            # 2) Cargar uso mensual de slots por usuario (mismo mes de la fecha del sorteo)
            first_day = fecha_sorteo.replace(day=1)
            if fecha_sorteo.month == 12:
                next_month_first = date(fecha_sorteo.year + 1, 1, 1)
            else:
                next_month_first = date(fecha_sorteo.year, fecha_sorteo.month + 1, 1)

            resp_uso = requests.get(
                f"{rest_url}/slots",
                headers=headers,
                params={
                    "select": "fecha,reservado_por",
                    "fecha": f"gte.{first_day.isoformat()}",
                },
                timeout=15,
            )
            uso_raw = resp_uso.json() if resp_uso.status_code == 200 else []

            usos_mes = {}
            for r in uso_raw:
                try:
                    if r["reservado_por"] is None:
                        continue
                    f = date.fromisoformat(r["fecha"][:10])
                    if not (first_day <= f < next_month_first):
                        continue
                    uid = r["reservado_por"]
                    usos_mes[uid] = usos_mes.get(uid, 0) + 1
                except Exception:
                    continue

            # 3) Cargar slots cedidos para ese día
            resp_slots_dia = requests.get(
                f"{rest_url}/slots",
                headers=headers,
                params={
                    "select": "fecha,franja,plaza_id,owner_usa,reservado_por",
                    "fecha": f"eq.{fecha_sorteo.isoformat()}",
                },
                timeout=15,
            )
            if resp_slots_dia.status_code != 200:
                st.error("Error al leer slots para el día del sorteo.")
                st.code(resp_slots_dia.text)
                return

            slots_dia = resp_slots_dia.json()

            from collections import defaultdict
            libres_por_franja = defaultdict(list)  # franja -> [plaza_id]

            for s_d in slots_dia:
                if s_d["owner_usa"] is False and s_d["reservado_por"] is None:
                    libres_por_franja[s_d["franja"]].append(s_d["plaza_id"])

            total_asignados = 0
            total_rechazados = 0

            # 4) Sorteo por franja (M / T)
            for franja in ["M", "T"]:
                candidatos = [p for p in pre_pendientes if p["franja"] == franja]
                if not candidatos:
                    continue

                plazas_libres = libres_por_franja.get(franja, [])
                if not plazas_libres:
                    # No hay plazas → todos rechazados
                    for pre in candidatos:
                        r_rej = requests.patch(
                            f"{rest_url}/pre_reservas",
                            headers=headers,
                            params={"id": f"eq.{pre['id']}"},
                            json={"estado": "RECHAZADO"},
                            timeout=10,
                        )
                        total_rechazados += 1
                    continue

                # Ordenar candidatos: menos usos primero, empate → random
                def orden(pre):
                    u = pre["usuario_id"]
                    return (usos_mes.get(u, 0), random.random())

                candidatos_ordenados = sorted(candidatos, key=orden)

                for pre in candidatos_ordenados:
                    if not plazas_libres:
                        # Sin plazas → rechazado
                        r_rej = requests.patch(
                            f"{rest_url}/pre_reservas",
                            headers=headers,
                            params={"id": f"eq.{pre['id']}"},
                            json={"estado": "RECHAZADO"},
                            timeout=10,
                        )
                        total_rechazados += 1
                        continue

                    plaza_id = plazas_libres.pop(0)
                    usuario_id = pre["usuario_id"]

                    usos_mes[usuario_id] = usos_mes.get(usuario_id, 0) + 1

                    # a) Upsert en slots
                    payload_slot = [{
                        "fecha": fecha_sorteo.isoformat(),
                        "plaza_id": plaza_id,
                        "franja": franja,
                        "owner_usa": False,
                        "reservado_por": usuario_id,
                        "estado": "CONFIRMADO",
                    }]
                    local_headers = headers.copy()
                    local_headers["Prefer"] = "resolution=merge-duplicates"

                    r_slot = requests.post(
                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                        headers=local_headers,
                        json=payload_slot,
                        timeout=10,
                    )
                    if r_slot.status_code >= 400:
                        st.error("Error al asignar la plaza en slots.")
                        st.code(r_slot.text)
                        return

                    # b) Marcar pre_reserva como ASIGNADO
                    r_asig = requests.patch(
                        f"{rest_url}/pre_reservas",
                        headers=headers,
                        params={"id": f"eq.{pre['id']}"},
                        json={"estado": "ASIGNADO"},
                        timeout=10,
                    )
                    if r_asig.status_code >= 400:
                        st.error("Error al marcar pre_reserva como ASIGNADA.")
                        st.code(r_asig.text)
                        return

                    total_asignados += 1

            st.success(
                f"Sorteo completado para {fecha_sorteo.strftime('%d/%m/%Y')}. "
                f"Asignados: {total_asignados} · Rechazados: {total_rechazados}."
            )
            st.info(
                "Los suplentes verán ahora sus plazas asignadas o las solicitudes no aprobadas "
                "en 'Tus próximas reservas / solicitudes'."
            )

        except Exception as e:
            st.error("Ha ocurrido un error al ejecutar el sorteo.")
            st.code(str(e))

    # ---------- BOTÓN 2: REINICIAR SORTEO (DEMO) ----------
    if col_reset.button("Reiniciar sorteos de esta fecha (demo)"):
        try:
            # 1) Poner en PENDIENTE todas las pre_reservas ASIGNADO/RECHAZADO de esa fecha
            r_pre_reset = requests.patch(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={
                    "fecha": f"eq.{fecha_sorteo.isoformat()}",
                    "estado": "in.(ASIGNADO,RECHAZADO)",
                },
                json={"estado": "PENDIENTE"},
                timeout=15,
            )

            # 2) Liberar slots cedidos y asignados a suplentes para esa fecha
            #    (owner_usa = false, reservado_por NOT NULL)
            resp_slots_reset = requests.get(
                f"{rest_url}/slots",
                headers=headers,
                params={
                    "select": "plaza_id,franja",
                    "fecha": f"eq.{fecha_sorteo.isoformat()}",
                    "owner_usa": "eq.false",
                    "reservado_por": "not.is.null",
                },
                timeout=15,
            )
            if resp_slots_reset.status_code == 200:
                slots_asig = resp_slots_reset.json()
                for s_a in slots_asig:
                    payload_libre = [{
                        "fecha": fecha_sorteo.isoformat(),
                        "plaza_id": s_a["plaza_id"],
                        "franja": s_a["franja"],
                        "owner_usa": False,
                        "reservado_por": None,
                    }]
                    local_headers = headers.copy()
                    local_headers["Prefer"] = "resolution=merge-duplicates"

                    r_lib = requests.post(
                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                        headers=local_headers,
                        json=payload_libre,
                        timeout=10,
                    )

            st.success(
                f"Sorteos reiniciados para {fecha_sorteo.strftime('%d/%m/%Y')}. "
                "Todas las solicitudes vuelven a estado PENDIENTE y las plazas quedan libres."
            )
        except Exception as e:
            st.error("Ha ocurrido un error al reiniciar los sorteos.")
            st.code(str(e))

def view_titular(profile):
    st.subheader("Panel TITULAR")

    nombre = profile.get("nombre")
    plaza_id = profile.get("plaza_id")

    st.write(f"Nombre: {nombre}")

    if not plaza_id:
        st.error("Aún no tienes plaza asignada en app_users (plaza_id es NULL).")
        return

    st.write(f"Tu plaza asignada: **P-{plaza_id}**")
    st.markdown("Marca qué franjas **cedes** tu plaza esta semana (lunes a viernes):")

    # Helper: ¿puede el titular modificar la cesión en esa fecha?
    def se_puede_modificar_cesion(fecha_slot: date) -> bool:
        """
        Reglas titular:
          - HOY: no puede cambiar nada
          - MAÑANA: solo antes de las 20:00
          - FUTURO (pasado mañana): siempre puede
        """
        hoy = date.today()
        ahora = datetime.now().time()
        limite = time(20, 0)

        if fecha_slot == hoy:
            return False

        if fecha_slot == hoy + timedelta(days=1) and ahora >= limite:
            return False

        return True

    # Semana actual (lunes a viernes)
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())  # 0 = lunes
    all_dias_semana = [lunes + timedelta(days=i) for i in range(5)]  # lun–vie
    # Solo mostramos HOY y días futuros
    dias_semana = [d for d in all_dias_semana if d >= hoy]

    rest_url, headers, _ = get_rest_info()

    # Leer todos los slots de esta plaza
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

    # Mapeos:
    estado = {}   # (fecha, franja) -> owner_usa (bool)
    reservas = {} # (fecha, franja) -> reservado_por (uuid o None)

    for s in slots:
        try:
            fecha_str = s["fecha"][:10]   # por si viene con hora
            f = date.fromisoformat(fecha_str)
            franja = s["franja"]
            estado[(f, franja)] = s["owner_usa"]
            reservas[(f, franja)] = s["reservado_por"]
        except Exception:
            continue

    # ---------------------------
    # Agenda del titular (resumen semana)
    # ---------------------------
    filas_agenda = []
    for d in dias_semana:
        fila = {"Día": d.strftime("%a %d/%m")}
        for franja, etiqueta in [("M", "Mañana"), ("T", "Tarde")]:
            owner_usa = estado.get((d, franja), True)
            reservado_por = reservas.get((d, franja))

            if reservado_por is not None:
                texto = "Cedida (reservada)"
            else:
                if owner_usa:
                    texto = "Titular usa"
                else:
                    texto = "Cedida (libre)"

            fila[etiqueta] = texto
        filas_agenda.append(fila)

    st.markdown("### Tu agenda esta semana")
    df_agenda = pd.DataFrame(filas_agenda)
    st.table(df_agenda)

    st.markdown("### Semana actual")

    header_cols = st.columns(4)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")
    header_cols[3].markdown("**Día completo**")

    # Solo guardaremos decisiones en franjas editables
    cedencias = {}

    for d in dias_semana:
        col_dia, col_m, col_t, col_full = st.columns(4)
        col_dia.write(d.strftime("%a %d/%m"))

        editable = se_puede_modificar_cesion(d)

        # Estado actual en BD
        owner_usa_M = estado.get((d, "M"), True)
        owner_usa_T = estado.get((d, "T"), True)
        reservado_M = reservas.get((d, "M"))
        reservado_T = reservas.get((d, "T"))

        # Por defecto, cedida = no la usa el titular
        cedida_M = not owner_usa_M
        cedida_T = not owner_usa_T

        # Caso 1: NO editable (HOY o mañana > 20:00)
        if not editable:
            # Mañana
            if reservado_M is not None:
                col_m.markdown("✅ Cedida (reservada)")
                cedida_M = True
            else:
                texto_M = "Cedida (libre)" if cedida_M else "Titular usa"
                col_m.markdown(texto_M)

            # Tarde
            if reservado_T is not None:
                col_t.markdown("✅ Cedida (reservada)")
                cedida_T = True
            else:
                texto_T = "Cedida (libre)" if cedida_T else "Titular usa"
                col_t.markdown(texto_T)

            # Día completo solo como marcador vacío
            col_full.markdown("—")

        # Caso 2: editable y sin reservas → permitir checkbox día completo
        elif reservado_M is None and reservado_T is None:
            default_M = cedida_M
            default_T = cedida_T
            default_full = default_M and default_T

            # Checkbox día completo
            full_checked = col_full.checkbox(
                "Ceder día completo",
                value=default_full,
                key=f"cede_full_{d.isoformat()}",
            )

            if full_checked:
                # OCULTAR mañana / tarde
                col_m.markdown("_Incluida en día completo_")
                col_t.markdown("_Incluida en día completo_")
                cedida_M = True
                cedida_T = True
            else:
                # Mostrar los checkboxes individuales
                cedida_M = col_m.checkbox(
                    "Cedo",
                    value=default_M,
                    key=f"cede_{d.isoformat()}_M",
                )
                cedida_T = col_t.checkbox(
                    "Cedo",
                    value=default_T,
                    key=f"cede_{d.isoformat()}_T",
                )

        # Caso 3: editable pero hay reservas en alguna franja → sin checkbox día completo
        else:
            col_full.markdown("—")

            # Mañana
            if reservado_M is not None:
                col_m.markdown("✅ Cedida (reservada)")
                cedida_M = True
            else:
                cedida_M = col_m.checkbox(
                    "Cedo",
                    value=cedida_M,
                    key=f"cede_{d.isoformat()}_M",
                )

            # Tarde
            if reservado_T is not None:
                col_t.markdown("✅ Cedida (reservada)")
                cedida_T = True
            else:
                cedida_T = col_t.checkbox(
                    "Cedo",
                    value=cedida_T,
                    key=f"cede_{d.isoformat()}_T",
                )

        # Guardamos resultados para el guardado final
        cedencias[(d, "M")] = cedida_M
        cedencias[(d, "T")] = cedida_T

    st.markdown("---")
    if st.button("Guardar cambios de la semana"):
        try:
            for (d, franja), cedida in cedencias.items():
                # Por seguridad, volvemos a comprobar ventana de edición
                if not se_puede_modificar_cesion(d):
                    continue

                owner_usa = not cedida  # si la cedo, yo no la uso

                payload = [{
                    "fecha": d.isoformat(),
                    "plaza_id": plaza_id,
                    "franja": franja,
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

            st.success("Disponibilidad de la semana actualizada ✅")
        except Exception as e:
            st.error("Error al guardar la disponibilidad.")
            st.code(str(e))


def view_suplente(profile):
    st.subheader("Panel SUPLENTE")

    nombre = profile.get("nombre")
    st.write(f"Nombre: {nombre}")

    # user_id viene de la sesión de Auth
    auth = st.session_state.get("auth")
    if not auth:
        st.error("No se ha podido obtener la información de usuario (auth).")
        return
    user_id = auth["user"]["id"]

    rest_url, headers, _ = get_rest_info()

    hoy = date.today()

    # ---------------------------
    # 1) Franjas usadas este mes (KPI informativo)
    # ---------------------------
    first_day = hoy.replace(day=1)
    if hoy.month == 12:
        next_month_first = date(hoy.year + 1, 1, 1)
    else:
        next_month_first = date(hoy.year, hoy.month + 1, 1)

    try:
        resp_count = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha",
                "reservado_por": f"eq.{user_id}",
                "fecha": f"gte.{first_day.isoformat()}",
            },
            timeout=10,
        )
        reservas_usuario_raw = resp_count.json() if resp_count.status_code == 200 else []
    except Exception as e:
        st.error("No se ha podido comprobar el número de reservas del mes.")
        st.code(str(e))
        reservas_usuario_raw = []

    reservas_mes = []
    for r in reservas_usuario_raw:
        try:
            fecha_str = r["fecha"][:10]
            f = date.fromisoformat(fecha_str)
            if first_day <= f < next_month_first:
                reservas_mes.append(r)
        except Exception:
            continue

    usadas_mes = len(reservas_mes)
    st.write(f"Franjas utilizadas este mes: **{usadas_mes}**")

    # ---------------------------
    # 2) Próximas reservas / solicitudes (slots + pre_reservas)
    # ---------------------------
    # 2.a) Reservas firmes en slots
    try:
        resp_slots_user = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,plaza_id",
                "reservado_por": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc",
            },
            timeout=10,
        )
        slots_user_raw = resp_slots_user.json() if resp_slots_user.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido cargar tus próximas reservas (slots).")
        st.code(str(e))
        slots_user_raw = []

    slots_user = {}
    for r in slots_user_raw:
        try:
            f = date.fromisoformat(r["fecha"][:10])
            franja = r["franja"]
            slots_user[(f, franja)] = r["plaza_id"]
        except Exception:
            continue

    # 2.b) Solicitudes en pre_reservas
    try:
        resp_pre = requests.get(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "select": "fecha,franja,estado",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc",
            },
            timeout=10,
        )
        pre_user_raw = resp_pre.json() if resp_pre.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido cargar tus solicitudes (pre_reservas).")
        st.code(str(e))
        pre_user_raw = []

    pre_user = {}
    for r in pre_user_raw:
        try:
            if r["estado"] == "CANCELADO":
                continue
            f = date.fromisoformat(r["fecha"][:10])
            franja = r["franja"]
            pre_user[(f, franja)] = r["estado"]
        except Exception:
            continue

    claves = set(slots_user.keys()) | set(pre_user.keys())
    claves_ordenadas = sorted(claves, key=lambda x: (x[0], x[1]))

    st.markdown("### 🔜 Tus próximas reservas / solicitudes")
    if not claves_ordenadas:
        st.markdown("_No tienes reservas ni solicitudes futuras._")
    else:
        lineas = []
        for (f, franja) in claves_ordenadas:
            franja_txt = "Mañana" if franja == "M" else "Tarde"
            fecha_txt = f.strftime("%a %d/%m")

            if (f, franja) in slots_user:
                plaza_id = slots_user[(f, franja)]
                if f == hoy:
                    texto = f"- {fecha_txt} – {franja_txt} – Plaza **P-{plaza_id}**"
                else:
                    texto = f"- {fecha_txt} – {franja_txt} – Plaza **P-{plaza_id}** (asignada)"
            else:
                estado = pre_user.get((f, franja), "PENDIENTE")
                if estado == "PENDIENTE":
                    texto = f"- {fecha_txt} – {franja_txt} – _Solicitud pendiente de plaza_"
                elif estado == "ASIGNADO":
                    texto = f"- {fecha_txt} – {franja_txt} – _Plaza asignada (pendiente de reflejar)_"
                elif estado == "RECHAZADO":
                    texto = f"- {fecha_txt} – {franja_txt} – _Solicitud no aprobada_"
                else:
                    continue
            lineas.append(texto)

        if lineas:
            st.markdown("\n".join(lineas))
        else:
            st.markdown("_No tienes reservas ni solicitudes futuras._")

    # ---------------------------
    # 3) Semana actual (hoy → viernes) con vista agregada + día completo
    # ---------------------------
    lunes = hoy - timedelta(days=hoy.weekday())
    all_dias_semana = [lunes + timedelta(days=i) for i in range(5)]
    dias_semana = [d for d in all_dias_semana if d >= hoy]
    if not dias_semana:
        st.info("La semana actual ya ha terminado.")
        return
    fin_semana = dias_semana[-1]

    # 3.a) Cargar slots de la semana
    try:
        resp_slots = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,owner_usa,reservado_por,plaza_id",
                "fecha": f"gte.{lunes.isoformat()}",
            },
            timeout=10,
        )
        datos_slots = resp_slots.json() if resp_slots.status_code == 200 else []
    except Exception as e:
        st.error("No se ha podido leer la disponibilidad de esta semana.")
        st.code(str(e))
        datos_slots = []

    from collections import defaultdict
    libres = defaultdict(int)      # (fecha, franja) -> nº plazas cedidas libres
    reservas_user = {}             # (fecha, franja) -> plaza_id

    for fila in datos_slots:
        try:
            f = date.fromisoformat(fila["fecha"][:10])
        except Exception:
            continue

        if not (lunes <= f <= fin_semana):
            continue

        franja = fila["franja"]
        owner_usa = fila["owner_usa"]
        reservado_por = fila["reservado_por"]
        plaza_id = fila["plaza_id"]

        if reservado_por == user_id:
            reservas_user[(f, franja)] = plaza_id

        if owner_usa is False and reservado_por is None:
            libres[(f, franja)] += 1

    # 3.b) Cargar pre_reservas del usuario para esta semana (excepto CANCELADO)
    try:
        resp_pre_sem = requests.get(
            f"{rest_url}/pre_reservas",
            headers=headers,
            params={
                "select": "fecha,franja,estado",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc",
            },
            timeout=10,
        )
        pre_sem_raw = resp_pre_sem.json() if resp_pre_sem.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido cargar tus solicitudes de esta semana.")
        st.code(str(e))
        pre_sem_raw = []

    pre_sem = {}
    for r in pre_sem_raw:
        try:
            if r["estado"] == "CANCELADO":
                continue
            f = date.fromisoformat(r["fecha"][:10])
            if not (hoy <= f <= fin_semana):
                continue
            franja = r["franja"]
            pre_sem[(f, franja)] = r["estado"]
        except Exception:
            continue

    st.markdown("### Semana actual (plazas agregadas)")
    st.markdown(
        "_Verás la disponibilidad agregada de todas las plazas. "
        "Para hoy reservas directamente; para días futuros, haces solicitudes._"
    )

    header_cols = st.columns(4)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")
    header_cols[3].markdown("**Día completo**")

    reserva_click = None         # (fecha, franja, es_hoy)
    cancelar_slot_click = None   # (fecha, franja, plaza_id)
    cancelar_pre_click = None    # (fecha, franja)
    activar_pack_click = None    # fecha
    quitar_pack_click = None     # fecha

    for d in dias_semana:
        cols = st.columns(4)
        cols[0].write(d.strftime("%a %d/%m"))

        key_M = (d, "M")
        key_T = (d, "T")

        tiene_slot_M = key_M in reservas_user
        tiene_slot_T = key_T in reservas_user

        pre_M = pre_sem.get(key_M)
        pre_T = pre_sem.get(key_T)

        # Día completo "activo" cuando hay solicitud PENDIENTE en M y T y no hay slots
        full_day_pendiente = (
            not tiene_slot_M and not tiene_slot_T and
            pre_M == "PENDIENTE" and pre_T == "PENDIENTE"
        )

        puede_dia_completo = (d > hoy) and se_puede_modificar_slot(d, "reservar")

        # Solo dejamos crear "Día completo" si el día está limpio (sin slots ni pre_reservas)
        puede_solicitar_pack = (
            puede_dia_completo
            and not full_day_pendiente
            and not tiene_slot_M and not tiene_slot_T
            and key_M not in pre_sem
            and key_T not in pre_sem
        )

        # --- Columnas Mañana / Tarde ---
        for idx, franja in enumerate(["M", "T"], start=1):
            col = cols[idx]
            key_ft = (d, franja)

            if full_day_pendiente:
                col.markdown("🕒 Incluida en día completo")
                continue

            # 1) Reserva firme en slots
            if key_ft in reservas_user:
                plaza_id = reservas_user[key_ft]
                if d == hoy:
                    col.markdown(f"✅ Has reservado **P-{plaza_id}**")
                else:
                    col.markdown(f"✅ Plaza asignada **P-{plaza_id}**")

                if col.button("Cancelar", key=f"cancel_slot_{d.isoformat()}_{franja}"):
                    cancelar_slot_click = (d, franja, plaza_id)
                continue

            # 2) Solicitud en pre_reservas
            if key_ft in pre_sem:
                estado = pre_sem[key_ft]
                if estado == "PENDIENTE":
                    col.markdown("🕒 Has solicitado\n_Plaza pendiente de sorteo_")
                    if col.button("Cancelar", key=f"cancel_pre_{d.isoformat()}_{franja}"):
                        cancelar_pre_click = (d, franja)
                elif estado == "ASIGNADO":
                    col.markdown("✅ Plaza asignada (aún sin plaza concreta)")
                    if col.button("Cancelar", key=f"cancel_asig_{d.isoformat()}_{franja}"):
                        cancelar_pre_click = (d, franja)
                elif estado == "RECHAZADO":
                    col.markdown("❌ Solicitud no aprobada")
                else:
                    col.markdown("—")
                continue

            # 3) Sin reserva ni solicitud → botones de solicitar / reservar
            num_disp = libres.get(key_ft, 0)
            if num_disp > 0 and se_puede_modificar_slot(d, "reservar"):
                if d == hoy:
                    label = f"Reservar ahora ({num_disp} disp.)"
                else:
                    label = f"Solicitar ({num_disp} disp.)"
                if col.button(label, key=f"res_{d.isoformat()}_{franja}"):
                    reserva_click = (d, franja, (d == hoy))
            else:
                col.markdown("⬜️ _No disponible_")

        # --- Columna Día completo ---
        col_full = cols[3]
        if not puede_dia_completo:
            col_full.markdown("—")
        elif full_day_pendiente:
            if col_full.button("Quitar día completo", key=f"pack_off_{d.isoformat()}"):
                quitar_pack_click = d
        elif puede_solicitar_pack:
            if col_full.button("Día completo", key=f"pack_on_{d.isoformat()}"):
                activar_pack_click = d
        else:
            col_full.markdown("_Usando franjas sueltas_")

    # ---------------------------
    # 4) Cancelar reserva firme (slots)
    # ---------------------------
    if cancelar_slot_click is not None:
        dia_cancel, franja_cancel, plaza_cancel = cancelar_slot_click

        if not se_puede_modificar_slot(dia_cancel, "cancelar"):
            if dia_cancel == hoy:
                st.error(
                    "No puedes cancelar reservas para HOY. "
                    "Si no vas a usar la plaza, avisa al titular por fuera de la app."
                )
            else:
                st.error(
                    "Ya no puedes cancelar esta reserva: "
                    "las reservas para mañana quedan bloqueadas a partir de las 20:00."
                )
            return

        try:
            payload = [{
                "fecha": dia_cancel.isoformat(),
                "plaza_id": plaza_cancel,
                "franja": franja_cancel,
                "owner_usa": False,
                "reservado_por": None,
            }]
            local_headers = headers.copy()
            local_headers["Prefer"] = "resolution=merge-duplicates"

            r_update = requests.post(
                f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                headers=local_headers,
                json=payload,
                timeout=10,
            )
            if r_update.status_code >= 400:
                st.error("Supabase ha devuelto un error al cancelar la reserva:")
                st.code(r_update.text)
                return

            try:
                requests.patch(
                    f"{rest_url}/pre_reservas",
                    headers=headers,
                    params={
                        "usuario_id": f"eq.{user_id}",
                        "fecha": f"eq.{dia_cancel.isoformat()}",
                        "franja": f"eq.{franja_cancel}",
                        "estado": "in.(PENDIENTE,ASIGNADO)",
                    },
                    json={"estado": "CANCELADO"},
                    timeout=10,
                )
            except Exception:
                pass

            st.success(
                f"Reserva cancelada para {dia_cancel.strftime('%d/%m')} "
                f"{'mañana' if franja_cancel=='M' else 'tarde'} "
                f"(plaza P-{plaza_cancel})."
            )
            st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al intentar cancelar la reserva.")
            st.code(str(e))
            return

    # ---------------------------
    # 5) Cancelar pre-reserva (pendiente/asignada)
    # ---------------------------
    if cancelar_pre_click is not None:
        dia_cancel, franja_cancel = cancelar_pre_click

        if not se_puede_modificar_slot(dia_cancel, "cancelar"):
            if dia_cancel == hoy:
                st.error(
                    "No puedes cancelar solicitudes para HOY. "
                    "Si no vas a usar la plaza, avisa al titular por fuera de la app."
                )
            else:
                st.error(
                    "Ya no puedes cancelar esta solicitud: "
                    "las solicitudes para mañana quedan bloqueadas a partir de las 20:00."
                )
            return

        try:
            r_pre_cancel = requests.patch(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={
                    "usuario_id": f"eq.{user_id}",
                    "fecha": f"eq.{dia_cancel.isoformat()}",
                    "franja": f"eq.{franja_cancel}",
                    "estado": "in.(PENDIENTE,ASIGNADO)",
                },
                json={"estado": "CANCELADO"},
                timeout=10,
            )
            if r_pre_cancel.status_code >= 400:
                st.error("Supabase ha devuelto un error al cancelar la solicitud:")
                st.code(r_pre_cancel.text)
                return

            st.success(
                f"Solicitud cancelada para {dia_cancel.strftime('%d/%m')} "
                f"{'mañana' if franja_cancel=='M' else 'tarde'}."
            )
            st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al intentar cancelar la solicitud.")
            st.code(str(e))
            return

    # ---------------------------
    # 6) Nueva reserva / solicitud (franjas sueltas)
    # ---------------------------
    if reserva_click is not None:
        dia_reserva, franja_reserva, es_hoy = reserva_click

        if not se_puede_modificar_slot(dia_reserva, "reservar"):
            st.error(
                "Ya no puedes reservar/solicitar esta franja: "
                "las franjas para mañana quedan bloqueadas a partir de las 20:00."
            )
            return

        try:
            if es_hoy:
                # Reserva directa en slots
                resp_libre = requests.get(
                    f"{rest_url}/slots",
                    headers=headers,
                    params={
                        "select": "plaza_id",
                        "fecha": f"eq.{dia_reserva.isoformat()}",
                        "franja": f"eq.{franja_reserva}",
                        "owner_usa": "eq.false",
                        "reservado_por": "is.null",
                        "order": "plaza_id.asc",
                        "limit": "1",
                    },
                    timeout=10,
                )
                if resp_libre.status_code != 200:
                    st.error("Error al buscar plaza libre.")
                    st.code(resp_libre.text)
                    return

                libres_hoy = resp_libre.json()
                if not libres_hoy:
                    st.error("Lo siento, ya no queda hueco disponible en esa franja.")
                    return

                plaza_id = libres_hoy[0]["plaza_id"]

                payload = [{
                    "fecha": dia_reserva.isoformat(),
                    "plaza_id": plaza_id,
                    "franja": franja_reserva,
                    "owner_usa": False,
                    "reservado_por": user_id,
                    "estado": "CONFIRMADO",
                }]
                local_headers = headers.copy()
                local_headers["Prefer"] = "resolution=merge-duplicates"

                r_update = requests.post(
                    f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                    headers=local_headers,
                    json=payload,
                    timeout=10,
                )
                if r_update.status_code >= 400:
                    st.error("Supabase ha devuelto un error al guardar la reserva:")
                    st.code(r_update.text)
                    return

                st.success(
                    f"Reserva confirmada para {dia_reserva.strftime('%d/%m')} "
                    f"{'mañana' if franja_reserva=='M' else 'tarde'}. "
                    f"Se te ha asignado una plaza ✅"
                )
                st.rerun()

            else:
                # Solicitud futura → pre_reservas
                payload_pre = [{
                    "usuario_id": user_id,
                    "fecha": dia_reserva.isoformat(),
                    "franja": franja_reserva,
                    "estado": "PENDIENTE",
                }]
                r_pre = requests.post(
                    f"{rest_url}/pre_reservas",
                    headers=headers,
                    json=payload_pre,
                    timeout=10,
                )
                if r_pre.status_code >= 400:
                    st.error("Supabase ha devuelto un error al registrar la solicitud:")
                    st.code(r_pre.text)
                    return

                st.success(
                    f"Solicitud registrada para {dia_reserva.strftime('%d/%m')} "
                    f"{'mañana' if franja_reserva=='M' else 'tarde'}. "
                    "Entrará en el sorteo correspondiente. ✅"
                )
                st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al intentar reservar/solicitar la plaza.")
            st.code(str(e))
            return

    # ---------------------------
    # 7) Día completo: crear / quitar pack (dos solicitudes)
    # ---------------------------
    if activar_pack_click is not None:
        dia = activar_pack_click
        if not se_puede_modificar_slot(dia, "reservar"):
            st.error(
                "Ya no puedes solicitar día completo: "
                "las solicitudes para mañana quedan bloqueadas a partir de las 20:00."
            )
            return
        try:
            payload_pre = [
                {
                    "usuario_id": user_id,
                    "fecha": dia.isoformat(),
                    "franja": "M",
                    "estado": "PENDIENTE",
                },
                {
                    "usuario_id": user_id,
                    "fecha": dia.isoformat(),
                    "franja": "T",
                    "estado": "PENDIENTE",
                },
            ]
            r_pre = requests.post(
                f"{rest_url}/pre_reservas",
                headers=headers,
                json=payload_pre,
                timeout=10,
            )
            if r_pre.status_code >= 400:
                st.error("Supabase ha devuelto un error al registrar el día completo:")
                st.code(r_pre.text)
                return

            st.success(
                f"Solicitado **día completo** para {dia.strftime('%d/%m')}. "
                "Entrará en el sorteo correspondiente. ✅"
            )
            st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al solicitar día completo.")
            st.code(str(e))
            return

    if quitar_pack_click is not None:
        dia = quitar_pack_click
        if not se_puede_modificar_slot(dia, "cancelar"):
            st.error(
                "Ya no puedes cancelar el día completo: "
                "las solicitudes para mañana quedan bloqueadas a partir de las 20:00."
            )
            return
        try:
            r_pre_cancel = requests.patch(
                f"{rest_url}/pre_reservas",
                headers=headers,
                params={
                    "usuario_id": f"eq.{user_id}",
                    "fecha": f"eq.{dia.isoformat()}",
                    "franja": "in.(M,T)",
                    "estado": "in.(PENDIENTE,ASIGNADO)",
                },
                json={"estado": "CANCELADO"},
                timeout=10,
            )
            if r_pre_cancel.status_code >= 400:
                st.error("Supabase ha devuelto un error al cancelar el día completo:")
                st.code(r_pre_cancel.text)
                return

            st.success(
                f"Día completo cancelado para {dia.strftime('%d/%m')}."
            )
            st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al cancelar el día completo.")
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
