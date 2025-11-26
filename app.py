import streamlit as st
import requests
from datetime import date, timedelta

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
# Vistas por rol
# ---------------------------------------------
def view_admin(profile):
    st.subheader("Panel ADMIN")
    st.write(f"Nombre: {profile.get('nombre')}")
    st.write("Desde aquí más adelante podrás:")
    st.markdown("""
    - Gestionar usuarios y roles
    - Ver estadísticas de uso del parking
    - Forzar cancelaciones, etc.
    """)

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

    # Mapeamos (fecha, franja) -> owner_usa
    estado = {}
    for s in slots:
        try:
            # Cortamos a 'YYYY-MM-DD' por si viene con hora/zona
            fecha_str = s["fecha"][:10]
            f = date.fromisoformat(fecha_str)
            estado[(f, s["franja"])] = s["owner_usa"]
        except Exception:
            continue

    st.markdown("### Semana actual")

    header_cols = st.columns(3)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")

    cedencias = {}

    for d in dias_semana:
        cols = st.columns(3)
        cols[0].write(d.strftime("%a %d/%m"))

        for idx, franja in enumerate(["M", "T"], start=1):
            owner_usa = estado.get((d, franja), True)
            cedida_por_defecto = not owner_usa  # si no la usa, es que la cedió

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
                    "reservado_por": None,  # si cambia de opinión, liberamos cualquier reserva
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
        reservas_usuario = resp_count.json() if resp_count.status_code == 200 else []
    except Exception as e:
        st.error("No se ha podido comprobar el número de reservas del mes.")
        st.code(str(e))
        reservas_usuario = []

    # Filtramos en código hasta el primer día del mes siguiente
    reservas_mes = []
    for r in reservas_usuario:
        try:
            fecha_str = r["fecha"][:10]
            f = date.fromisoformat(fecha_str)
            if first_day <= f < next_month_first:
                reservas_mes.append(r)
        except Exception:
            continue

    usadas_mes = len(reservas_mes)
    st.write(f"Franjas reservadas este mes: **{usadas_mes} / 10**")

    if usadas_mes >= 10:
        st.warning("Has llegado al máximo de 10 franjas este mes. No puedes hacer más reservas.")
        return

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

    # Pintamos la semana
    for d in dias_semana:
        cols = st.columns(3)
        cols[0].write(d.strftime("%a %d/%m"))

        for idx, franja in enumerate(["M", "T"], start=1):
            num_disponibles = disponibles.get((d, franja), 0)

            if num_disponibles > 0:
                label = f"Reservar ({num_disponibles} disp.)"
                key = f"res_{d.isoformat()}_{franja}"

                if cols[idx].button(label, key=key):
                    reserva_seleccionada = (d, franja)
            else:
                cols[idx].markdown("⬜️ _No disponible_")

    # ---------------------------
    # 4) Si el usuario pulsa un botón de reserva
    # ---------------------------
    if reserva_seleccionada is not None:
        dia_reserva, franja_reserva = reserva_seleccionada

        # Re-chequeo rápido por si se ha llenado en medio (opcional)
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
            libres = resp_libre.json() if resp_libre.status_code == 200 else []

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
                "owner_usa": False,            # sigue siendo cesión del titular
                "reservado_por": user_id,       # ahora asignada a este suplente
                "estado": "RESERVADO",
            }]

            local_headers = headers.copy()
            local_headers["Prefer"] = "resolution=merge-duplicates"

            r_update = requests.post(
                f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                headers=local_headers,
                json=payload,
                timeout=10,
            )
            r_update.raise_for_status()

            st.success(
                f"Reserva confirmada para {dia_reserva.strftime('%d/%m')} "
                f"{'mañana' if franja_reserva=='M' else 'tarde'}. "
                f"Plaza asignada: **P-{plaza_id}** ✅"
            )

        except Exception as e:
            st.error("Ha ocurrido un error al intentar reservar la plaza.")
            st.code(str(e))

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
