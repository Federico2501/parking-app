import streamlit as st
import requests
import pandas as pd
from datetime import date, timedelta, datetime

st.set_page_config(page_title="Parking empresa", page_icon="🅿️")

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

    # Semana actual (lunes a viernes)
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())  # 0 = lunes
    dias_semana = [lunes + timedelta(days=i) for i in range(5)]  # lun–vie

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

    # Mapeamos:
    #   - estado[(fecha, franja)] -> owner_usa
    #   - reservas[(fecha, franja)] -> reservado_por (None o uuid)
    estado = {}
    reservas = {}

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
    import pandas as pd
    df_agenda = pd.DataFrame(filas_agenda)
    st.table(df_agenda)

    st.markdown("### Semana actual")

    header_cols = st.columns(3)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")

    # Solo guardaremos decisiones en franjas NO reservadas
    cedencias = {}

    for d in dias_semana:
        cols = st.columns(3)
        cols[0].write(d.strftime("%a %d/%m"))

        for idx, franja in enumerate(["M", "T"], start=1):
            owner_usa = estado.get((d, franja), True)
            cedida_por_defecto = not owner_usa  # si no la usa, es que la cedió
            reservado_por = reservas.get((d, franja))

            if reservado_por is not None:
                # Ya hay un suplente reservado: el titular NO puede tocarlo
                cols[idx].markdown("✅ Cedida (reservada)")
                continue

            # Franja sin reserva: el titular puede marcar/desmarcar "Cedo"
            key = f"cede_{d.isoformat()}_{franja}"
            cedida = cols[idx].checkbox(
                "Cedo",
                value=cedida_por_defecto,
                key=key,
            )
            cedencias[(d, franja)] = cedida

    st.markdown("---")
    if st.button("Guardar cambios de la semana"):
        try:
            for (d, franja), cedida in cedencias.items():
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

    # ---------------------------
    # 1) Franjas usadas este mes (máx 10)
    # ---------------------------
    hoy = date.today()
    first_day = hoy.replace(day=1)
    # primer día del mes siguiente
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

    # Filtramos en código hasta el primer día del mes siguiente
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
    st.write(f"Franjas reservadas este mes: **{usadas_mes} / 10**")

    # ---------------------------
    # 1.b) Próximas reservas (desde hoy en adelante)
    # ---------------------------
    try:
        resp_upcoming = requests.get(
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
        upcoming_raw = resp_upcoming.json() if resp_upcoming.status_code == 200 else []
    except Exception as e:
        st.error("No se han podido cargar tus próximas reservas.")
        st.code(str(e))
        upcoming_raw = []

    proximas = []
    for r in upcoming_raw:
        try:
            fecha_str = r["fecha"][:10]
            f = date.fromisoformat(fecha_str)
            franja_txt = "Mañana" if r["franja"] == "M" else "Tarde"
            plaza_id = r["plaza_id"]
            proximas.append(
                f"- {f.strftime('%a %d/%m')} – {franja_txt} – Plaza **P-{plaza_id}**"
            )
        except Exception:
            continue

    st.markdown("### 🔜 Tus próximas reservas")
    if proximas:
        st.markdown("\n".join(proximas))
    else:
        st.markdown("_No tienes reservas futuras._")

    # ---------------------------
    # 2) Construir semana actual (lunes-viernes)
    # ---------------------------
    lunes = hoy - timedelta(days=hoy.weekday())  # 0 = lunes
    dias_semana = [lunes + timedelta(days=i) for i in range(5)]  # lun–vie
    fin_semana = dias_semana[-1]

    # ---------------------------
    # 3) Leer todos los slots de esa semana
    # ---------------------------
    try:
        resp = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,owner_usa,reservado_por,plaza_id",
                "fecha": f"gte.{lunes.isoformat()}",
            },
            timeout=10,
        )
        datos = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        st.error("No se ha podido leer la disponibilidad de esta semana.")
        st.code(str(e))
        datos = []

    from collections import defaultdict
    disponibles = defaultdict(int)
    reservas_usuario = {}  # (fecha, franja) -> plaza_id

    for fila in datos:
        try:
            fecha_str = fila["fecha"][:10]  # cortamos por si viene con hora/zona
            f = date.fromisoformat(fecha_str)
        except Exception:
            continue

        # Nos quedamos solo con esta semana (por si hay más adelante)
        if not (lunes <= f <= fin_semana):
            continue

        franja = fila["franja"]
        owner_usa = fila["owner_usa"]
        reservado_por = fila["reservado_por"]
        plaza_id = fila["plaza_id"]

        # Si este slot está reservado por ESTE usuario, lo marcamos
        if reservado_por == user_id:
            reservas_usuario[(f, franja)] = plaza_id

        # Hay hueco si el titular NO usa la plaza y nadie la ha reservado
        if owner_usa is False and reservado_por is None:
            disponibles[(f, franja)] += 1

    st.markdown("### Semana actual (plazas agregadas)")
    st.markdown(
        "_Verás la disponibilidad agregada de todas las plazas. "
        "Puedes reservar cualquier día/franja de esta semana mientras haya hueco._"
    )

    # Cabecera
    header_cols = st.columns(3)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")

    reserva_seleccionada = None
    cancel_seleccionada = None

    # Pintamos la semana
    for d in dias_semana:
        cols = st.columns(3)
        cols[0].write(d.strftime("%a %d/%m"))

        for idx, franja in enumerate(["M", "T"], start=1):
            # ¿Este usuario ya ha reservado esta franja?
            if (d, franja) in reservas_usuario:
                plaza_id = reservas_usuario[(d, franja)]
                cols[idx].markdown(
                    f"✅ Has reservado\n**P-{plaza_id}**"
                )
                if cols[idx].button("Cancelar", key=f"cancel_{d.isoformat()}_{franja}"):
                    cancel_seleccionada = (d, franja, plaza_id)
                continue

            num_disponibles = disponibles.get((d, franja), 0)

            if num_disponibles > 0:
                label = f"Reservar ({num_disponibles} disp.)"
                key = f"res_{d.isoformat()}_{franja}"

                if cols[idx].button(label, key=key):
                    reserva_seleccionada = (d, franja)
            else:
                cols[idx].markdown("⬜️ _No disponible_")

    # ---------------------------
    # 4) Cancelar reserva (si ha pulsado 'Cancelar')
    # ---------------------------
    if cancel_seleccionada is not None:
        dia_cancel, franja_cancel, plaza_cancel = cancel_seleccionada
        try:
            payload = [{
                "fecha": dia_cancel.isoformat(),
                "plaza_id": plaza_cancel,
                "franja": franja_cancel,
                "owner_usa": False,    # sigue cedida por el titular
                "reservado_por": None, # eliminamos la reserva
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
    # 5) Hacer reserva nueva (si ha pulsado 'Reservar')
    # ---------------------------
    if reserva_seleccionada is not None:
        dia_reserva, franja_reserva = reserva_seleccionada

        # Re-chequeo por si ya llegó al límite (otra sesión abierta, etc.)
        if usadas_mes >= 10:
            st.error("Ya has alcanzado el máximo de 10 franjas este mes.")
            return

        try:
            # Buscar una plaza concreta cedida y libre
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

            libres = resp_libre.json()
            if not libres:
                st.error("Lo siento, ya no queda hueco disponible en esa franja.")
                return

            slot = libres[0]
            plaza_id = slot["plaza_id"]

            # Upsert del slot: misma fecha/plaza/franja pero con reservado_por = usuario
            payload = [{
                "fecha": dia_reserva.isoformat(),
                "plaza_id": plaza_id,
                "franja": franja_reserva,
                "owner_usa": False,       # sigue siendo cesión del titular
                "reservado_por": user_id, # ahora asignada a este suplente
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
                f"Plaza asignada: **P-{plaza_id}** ✅"
            )
            st.rerun()

        except Exception as e:
            st.error("Ha ocurrido un error al intentar reservar la plaza.")
            st.code(str(e))
            return

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    st.title("🅿️ Parking Empresa")

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
