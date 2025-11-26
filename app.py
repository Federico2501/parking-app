import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Parking empresa", page_icon="🅿️")

@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_ANON_KEY"]
    return create_client(url, key)

def main():
    st.title("App de Parking - Conexión a Supabase")

    try:
        supabase = get_supabase_client()
        # Leer plazas de la tabla
        response = supabase.table("plazas").select("id, nombre").execute()
        plazas = response.data or []

        st.success("Conexión a Supabase OK ✅")
        st.write(f"Número de plazas en la base de datos: **{len(plazas)}**")

        st.subheader("Primeras plazas")
        st.write(plazas[:5])

    except Exception as e:
        st.error("Error al conectar con Supabase 😕")
        st.code(str(e))

if __name__ == "__main__":
    main()
