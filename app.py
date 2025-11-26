import streamlit as st
import requests

st.set_page_config(page_title="Parking empresa", page_icon="🅿️")

def main():
    st.title("App de Parking - Conexión a Supabase (REST)")

    try:
        # DEBUG: ver qué hay en secrets
        st.subheader("Debug secrets")
        st.write("Keys disponibles:", list(st.secrets.keys()))
        base_url = st.secrets["SUPABASE_URL"]
        anon_key = st.secrets["SUPABASE_ANON_KEY"]

        st.write("SUPABASE_URL leído:", base_url)
        st.write("Longitud de SUPABASE_ANON_KEY:", len(anon_key))

        # Construimos la URL REST
        base_url = base_url.rstrip("/")
        rest_url = f"{base_url}/rest/v1"
        st.write("REST URL construida:", rest_url)

        headers = {
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Llamada GET a plazas
        resp = requests.get(
            f"{rest_url}/plazas",
            headers=headers,
            params={"select": "id,nombre"},
            timeout=10,
        )
        resp.raise_for_status()

        plazas = resp.json()
        st.success("Conexión a Supab

