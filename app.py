import streamlit as st
import requests
import json

st.set_page_config(page_title="Parking empresa", page_icon="🅿️")

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
        return resp.json()  # tokens, user info
    else:
        return None

# ---------------------------------------------
# MAIN APP LOGIC
# ---------------------------------------------
def main():
    st.title("🅿️ Parking Empresa")

    rest_url, headers, anon_key = get_rest_info()

    # SESSION STATE: guardar sesión entre recargas
    if "user" not in st.session_state:
        st.session_state.user = None

    # If not logged in → show login form
    if st.session_state.user is None:
        st.subheader("Iniciar sesión")

        email = st.text_input("Email")
        password = st.text_input("Contraseña", type="password")

        if st.button("Entrar"):
            user_data = login(email, password, anon_key)
            if user_data:
                st.session_state.user = user_data
                st.success("Login correcto")
                st.experimental_rerun()
            else:
                st.error("Email o contraseña incorrectos")

        return  # don't continue

    # -------------------------------------
    # USER IS LOGGED IN
    # -------------------------------------
    user = st.session_state.user
    st.success(f"Sesión iniciada como: {user['user']['email']}")

    if st.button("Cerrar sesión"):
        st.session_state.user = None
        st.experimental_rerun()

    st.subheader("Panel de usuario")
    st.write("Aquí construiremos:")
    st.markdown("""
    - Roles (titular / suplente)
    - Ver tu plaza (si eres titular)
    - Informar cuándo NO usas tu plaza
    - Ver disponibilidad (si eres suplente)
    - Reservar franjas (mañana/tarde)
    """)

if __name__ == "__main__":
    main()
