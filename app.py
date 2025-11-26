import streamlit as st
import requests

st.set_page_config(page_title="Parking empresa", page_icon="🅿️")

@st.cache_resource
def get_supabase_rest():
    """
    Prepara la URL base del API REST de Supabase y los headers
    necesarios para autenticarse con la anon key.
    """
    base_url = st.secrets["SUPABASE_URL"].rstrip("/")
    rest_url = f"{base_url}/rest/v1"
    key = st.secrets["SUPABASE_ANON_KEY"]

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return rest_url, headers

def main():
    st.title("App de Parking - Conexión a Supabase (REST)")

    try:
        rest_url, headers = get_supabase_rest()

        # Llamada GET a la tabla "plazas": seleccionamos id y nombre
        resp = requests.get(
            f"{rest_url}/plazas",
            headers=headers,
            params={"select": "id,nombre"},
            timeout=10,
        )
        resp.raise_for_status()  # lanza error si el código HTTP no es 2xx

        plazas = resp.json()  # lista de dicts

        st.success("Conexión a Supabase OK ✅")
        st.write(f"Número de plazas en la base de datos: **{len(plazas)}**")

        st.subheader("Primeras plazas")
        st.write(plazas[:5])

    except Exception as e:
        st.error("Error al conectar con Supabase 😕")
        st.code(str(e))

if __name__ == "__main__":
    main()

