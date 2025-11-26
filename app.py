import streamlit as st
import requests

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
    st.write(f"Nombre: {profile.get('nombre')}")
    if profile.get("plaza_id"):
        st.write(f"Tu plaza asignada: **P-{profile.get('plaza_id')}**")
    else:
        st.warning("Aún no tienes plaza asignada en app_users.")
    st.info("Aquí añadiremos la pantalla para marcar qué días/franjas **NO** usas tu plaza.")

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
