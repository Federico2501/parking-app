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

    # Calculamos la semana actual (lunes a viernes)
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())  # weekday(): 0 = lunes
    dias_semana = [lunes + timedelta(days=i) for i in range(5)]  # lun–vie

    # Obtenemos info de Supabase
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
            f = date.fromisoformat(s["fecha"])
            estado[(f, s["franja"])] = s["owner_usa"]
        except Exception:
            continue

    # Construimos la tabla semanal: día vs mañana/tarde
    st.markdown("### Semana actual")

    header_cols = st.columns(3)
    header_cols[0].markdown("**Día**")
    header_cols[1].markdown("**Mañana**")
    header_cols[2].markdown("**Tarde**")

    # Guardamos aquí la decisión de ceder / no por cada día/franja
    cedencias = {}

    for d in dias_semana:
        cols = st.columns(3)
        # Columna día
        cols[0].write(d.strftime("%a %d/%m"))

        for idx, franja in enumerate(["M", "T"], start=1):
            # Por defecto, si no hay registro, el titular USA la plaza
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
            # Para cada día/franja, hacemos upsert del slot
            for (d, franja), cedida in cedencias.items():
                owner_usa = not cedida  # si la cedo, yo no la uso

                payload = [{
                    "fecha": d.isoformat(),
                    "plaza_id": plaza_id,
                    "franja": franja,
                    "owner_usa": owner_usa,
                    # si el titular vuelve a usarla, nos cargamos cualquier reserva anterior
                    "reservado_por": None if owner_usa else None,
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
    st.write(f"Nombre: {profile.get('nombre')}")
    st.info("Aquí añadiremos la pantalla para ver huecos libres y reservar (máx 10 franjas/mes).")

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
