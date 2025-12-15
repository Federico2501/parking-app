import streamlit as st
import requests
import pandas as pd
import random
import uuid
import base64
import json
from datetime import date, timedelta, datetime, time, timezone
from zoneinfo import ZoneInfo

st.set_page_config(
    page_title="Parking KM0",
    page_icon="Logo_KM0.png"
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

# ---------------------------------------------
# EV helpers (solicitudes + asignaciones)
# ---------------------------------------------
def ev_upsert_solicitud(fecha_obj: date, usuario_id: str, pref_turno: str, estado: str = "PENDIENTE"):
    """
    Crea/actualiza la solicitud EV del usuario para un día (1 fila por usuario+fecha).
    pref_turno: 'M' | 'T' | 'ANY'
    estado: por defecto PENDIENTE
    """
    rest_url, headers, _ = get_rest_info()

    payload = [{
        "fecha": fecha_obj.isoformat(),
        "usuario_id": usuario_id,
        "pref_turno": pref_turno,
        "estado": estado,
    }]

    local_headers = headers.copy()
    local_headers["Prefer"] = "resolution=merge-duplicates"

    # Upsert por constraint unique(fecha, usuario_id)
    resp = requests.post(
        f"{rest_url}/ev_solicitudes?on_conflict=fecha,usuario_id",
        headers=local_headers,
        json=payload,
        timeout=10,
    )
    return resp


def ev_cancelar_solicitud(fecha_obj: date, usuario_id: str):
    """Marca como CANCELADO la solicitud EV (si existe) para ese día."""
    rest_url, headers, _ = get_rest_info()

    resp = requests.patch(
        f"{rest_url}/ev_solicitudes",
        headers=headers,
        params={
            "fecha": f"eq.{fecha_obj.isoformat()}",
            "usuario_id": f"eq.{usuario_id}",
            "estado": "in.(PENDIENTE,ASIGNADO,RECHAZADO,NO_DISPONIBLE)",
        },
        json={"estado": "CANCELADO"},
        timeout=10,
    )
    return resp

def _decode_jwt_payload(token: str) -> dict:
    """
    Decodifica SOLO el payload de un JWT sin verificar firma.
    Nos sirve para leer 'exp', 'iss', 'aud', etc.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}

        payload_b64 = parts[1]
        # Añadimos padding si falta
        padding = '=' * (-len(payload_b64) % 4)
        payload_b64 += padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        payload_str = payload_bytes.decode("utf-8")
        return json.loads(payload_str)
    except Exception:
        return {}

def is_jwt_expired(token: str) -> bool:
    """
    Devuelve True si el JWT está caducado o no se puede leer bien.
    """
    # Import local para evitar que una variable global 'time' pise el módulo
    import time as _time

    payload = _decode_jwt_payload(token)
    if not payload:
        # Si no podemos leer el token, por seguridad lo consideramos inválido
        return True

    exp = payload.get("exp")
    if exp is None:
        # Si el token no trae 'exp', lo consideramos válido (caso raro)
        return False

    now = int(_time.time())
    return now >= int(exp)

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

def format_ts_madrid(ts_value):
    """
    Convierte un timestamptz de Supabase (string ISO o datetime)
    a string en hora Madrid.
    """
    if ts_value is None:
        return "-"

    if isinstance(ts_value, str):
        try:
            ts_value = ts_value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_value)
        except Exception:
            return ts_value
    else:
        dt = ts_value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_madrid = dt.astimezone(ZoneInfo("Europe/Madrid"))
    return dt_madrid.strftime("%d/%m/%Y %H:%M:%S")

def get_sorteo_log_for_date(fecha_obj: date):
    rest_url, headers, _ = get_rest_info()
    fecha_str = fecha_obj.isoformat()
    try:
        resp = requests.get(
            f"{rest_url}/sorteos_log",
            headers=headers,
            params={
                "select": "fecha,executed_at",
                "fecha": f"eq.{fecha_str}",
                "limit": 1,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception:
        return None


def get_last_sorteo_log():
    rest_url, headers, _ = get_rest_info()
    try:
        resp = requests.get(
            f"{rest_url}/sorteos_log",
            headers=headers,
            params={
                "select": "fecha,executed_at",
                "order": "executed_at.desc",
                "limit": 1,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception:
        return None



# ---------------------------------------------
# Sorteo de plazas     
# ---------------------------------------------

def ejecutar_sorteo(fecha_obj: date):
    """
    Ejecuta el sorteo para una fecha dada llamando a la función RPC
    ejecutar_sorteo_seguro(fecha_sorteo) en Supabase.

    Toda la lógica de:
      - lectura de pre_reservas,
      - lectura de slots,
      - fairness / asignación,
      - actualización de slots y pre_reservas

    se ejecuta ahora **en Postgres**, no en el cliente.
    """
    rest_url, headers, _ = get_rest_info()
    fecha_str = fecha_obj.isoformat()

    try:
        resp = requests.post(
            f"{rest_url}/rpc/ejecutar_sorteo_con_ev",
            headers=headers,
            json={"fecha_sorteo": fecha_str},
            timeout=30,
        )
    except Exception as e:
        st.error("No se ha podido conectar con el servidor para ejecutar el sorteo.")
        st.code(str(e))
        return

    if resp.status_code >= 400:
        st.error("Supabase ha devuelto un error al ejecutar el sorteo.")
        st.code(resp.text)
        return

    # La función SQL devuelve un JSON con el resumen del sorteo
    try:
        resumen = resp.json()
    except Exception:
        resumen = None

    # Si el RPC devuelve una lista de objetos con campo 'tipo',
    # calculamos un resumen similar al que tenías antes.
    asignadas = 0
    rechazadas = 0
    if isinstance(resumen, list):
        for r in resumen:
            tipo = r.get("tipo")
            if tipo in ("ASIGNADO", "PACK_ASIGNADO"):
                asignadas += 1
            elif tipo in ("RECHAZADO", "PACK_RECHAZADO"):
                rechazadas += 1

    st.success(
        f"Sorteo ejecutado para el {fecha_obj.strftime('%d/%m/%Y')}."
    )

    # Si tenemos contadores, mostramos detalle
    if asignadas or rechazadas:
        st.info(
            f"Franjas asignadas: {asignadas} · Solicitudes rechazadas: {rechazadas}."
        )

    # Mostrar el JSON completo para debugging (opcional)
    if resumen is not None:
        st.markdown("**Detalle devuelto por el servidor:**")
        st.json(resumen)


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
# ============================================================
#  CONTROL DE INTENTOS DE LOGIN
# ============================================================
# Constantes                                                   
MAX_INTENTOS = 4
BLOQUEO_MINUTOS = 15


def _parse_supabase_timestamp(value):
    """
    Convierte un timestamp de Supabase (string o datetime) en datetime (UTC naive).
    Si no puede parsearlo, devuelve None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    if isinstance(value, str):
        # Supabase suele devolver algo tipo '2025-12-01T09:15:00+00:00' o con 'Z'
        v = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(v)
            # Lo convertimos a naive UTC para comparar con datetime.utcnow()
            if dt.tzinfo is not None:
                dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            return None

    return None


def get_login_attempt_record(email: str):
    """Lee (si existe) la fila de login_attempts para este email."""
    rest_url, headers, _ = get_rest_info()
    try:
        resp = requests.get(
            f"{rest_url}/login_attempts",
            headers=headers,
            params={
                "select": "email,attempts,blocked_until",
                "email": f"eq.{email}",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        return data[0]
    except Exception:
        return None


def update_login_attempt_record(email: str, attempts: int, blocked_until: datetime | None):
    """Crea/actualiza la fila de login_attempts para este email."""
    rest_url, headers, _ = get_rest_info()

    if blocked_until is not None:
        # Guardamos como ISO string
        blocked_until_str = blocked_until.isoformat() + "Z"
    else:
        blocked_until_str = None

    payload = {
        "email": email,
        "attempts": attempts,
        "blocked_until": blocked_until_str,
    }

    local_headers = headers.copy()
    local_headers["Prefer"] = "resolution=merge-duplicates"

    try:
        requests.post(
            f"{rest_url}/login_attempts?on_conflict=email",
            headers=local_headers,
            json=payload,
            timeout=10,
        )
    except Exception:
        pass


def reset_login_attempts(email: str):
    """Pone attempts=0 y blocked_until=NULL para este email."""
    rest_url, headers, _ = get_rest_info()
    try:
        requests.patch(
            f"{rest_url}/login_attempts",
            headers=headers,
            params={"email": f"eq.{email}"},
            json={"attempts": 0, "blocked_until": None},
            timeout=10,
        )
    except Exception:
        pass


# Constantes globales (si no las tienes ya)
LOGIN_MAX_INTENTOS = 4
LOGIN_BLOQUEO_MINUTOS = 15

def _get_login_state(email: str):
    """
    Devuelve/crea el estado de login para un email concreto:
    - intentos_fallidos: int
    - bloqueado_hasta: datetime o None
    """
    if "login_states" not in st.session_state:
        st.session_state.login_states = {}

    key = f"login_state_{email}"
    state = st.session_state.login_states.get(key, {
        "intentos_fallidos": 0,
        "bloqueado_hasta": None,
    })

    # Normalizar bloqueado_hasta si viene como string
    bloqueado_hasta = state.get("bloqueado_hasta")
    if isinstance(bloqueado_hasta, str):
        try:
            bloqueado_hasta = datetime.fromisoformat(bloqueado_hasta)
        except Exception:
            bloqueado_hasta = None
    state["bloqueado_hasta"] = bloqueado_hasta

    st.session_state.login_states[key] = state
    return state, key


# ---------------------------------------------
# Tabla de seguridad de login (intentos y bloqueo)
#   - Tabla en Supabase: login_security
#   - Columnas: email (PK o unique), failed_attempts (int), blocked_until (timestamptz, puede ser NULL)
# ---------------------------------------------
def load_user_security(email: str) -> dict:
    """
    Devuelve la info de seguridad del usuario:
      - failed_attempts: nº de intentos fallidos
      - blocked_until: ISO string UTC o None
    Si no existe registro, devuelve valores por defecto.
    """
    rest_url, headers, _ = get_rest_info()
    try:
        resp = requests.get(
            f"{rest_url}/login_security",
            headers=headers,
            params={
                "select": "email,failed_attempts,blocked_until",
                "email": f"eq.{email}",
                "limit": 1,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return {"email": email, "failed_attempts": 0, "blocked_until": None}

        rows = resp.json()
        if not rows:
            return {"email": email, "failed_attempts": 0, "blocked_until": None}

        row = rows[0]
        return {
            "email": email,
            "failed_attempts": row.get("failed_attempts", 0),
            "blocked_until": row.get("blocked_until"),
        }
    except Exception:
        # Si algo falla, por seguridad, asumimos 0 intentos y sin bloqueo
        return {"email": email, "failed_attempts": 0, "blocked_until": None}


def save_block_info(email: str, attempts: int, blocked_until: datetime | None):
    """
    Guarda/actualiza el nº de intentos fallidos y la fecha de bloqueo.
    Usa upsert por email.
    """
    rest_url, headers, _ = get_rest_info()
    payload = [{
        "email": email,
        "failed_attempts": attempts,
        "blocked_until": blocked_until.isoformat() if blocked_until else None,
    }]

    local_headers = headers.copy()
    local_headers["Prefer"] = "resolution=merge-duplicates"

    requests.post(
        f"{rest_url}/login_security?on_conflict=email",
        headers=local_headers,
        json=payload,
        timeout=10,
    )


def reset_failed_attempts(email: str):
    """
    Resetea contador de intentos y bloqueo.
    Si no existe fila, la crea con 0 intentos y sin bloqueo.
    """
    rest_url, headers, _ = get_rest_info()
    try:
        # Intentamos parchear si existe
        resp = requests.patch(
            f"{rest_url}/login_security",
            headers=headers,
            params={"email": f"eq.{email}"},
            json={"failed_attempts": 0, "blocked_until": None},
            timeout=10,
        )
        # Si no ha modificado filas (por ejemplo, tabla vacía), hacemos upsert
        if resp.status_code >= 400:
            raise Exception("patch_error")
        # Si quieres ser ultra-estricto, podrías comprobar resp.text por nº filas afectadas
    except Exception:
        # Fallback: upsert directo
        save_block_info(email, 0, None)


# ---------------------------------------------
# LOGIN via SUPABASE AUTH (con límite de intentos)
# ---------------------------------------------
MAX_LOGIN_ATTEMPTS = 4
BLOCK_MINUTES = 15


def login(email, password, anon_key):
    """
    Login con límite de intentos y bloqueo temporal.
    - 4 intentos fallidos -> bloqueo 15 minutos.
    - Guarda el estado en st.session_state.login_security[email].
    """

    MAX_INTENTOS = 4
    BLOQUEO_MINUTOS = 15

    # Inicializar estructura de seguridad en sesión
    if "login_security" not in st.session_state:
        st.session_state.login_security = {}

    sec = st.session_state.login_security.get(
        email,
        {"attempts": 0, "blocked_until": None}
    )

    ahora_utc = datetime.utcnow()

    # -------------------------
    # 1) Comprobar si está bloqueado
    # -------------------------
    blocked_until = sec.get("blocked_until")

    # Por si en algún momento se guardó como string
    if isinstance(blocked_until, str):
        try:
            blocked_until = datetime.fromisoformat(blocked_until)
        except Exception:
            blocked_until = None

    if blocked_until is not None:
        if ahora_utc < blocked_until:
            # Ajuste horario simple: UTC+1 (Madrid invierno); si te cuadra, mantenlo
            hora_local = (blocked_until + timedelta(hours=1)).strftime("%H:%M")
            st.error(
                f"Usuario bloqueado por demasiados intentos fallidos. "
                f"Podrás volver a intentarlo a las {hora_local}."
            )
            return None
        else:
            # El bloqueo ya ha caducado → resetear contador
            sec = {"attempts": 0, "blocked_until": None}

    # -------------------------
    # 2) Intentar login contra Supabase
    # -------------------------
    url = st.secrets["SUPABASE_URL"].rstrip("/") + "/auth/v1/token?grant_type=password"
    payload = {"email": email, "password": password}

    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",   # <<< IMPORTANTE
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        st.error("No se ha podido conectar con el servidor de autenticación.")
        st.code(str(e))
        return None

    # ================================
    # DEBUG TEMPORAL: VER LA RESPUESTA REAL
    # ================================
    if resp.status_code != 200:
        st.warning(f"DEBUG auth: status={resp.status_code}")
        # mostramos solo los primeros 500 caracteres
        st.code(resp.text[:500])
    # ================================
    # 3) Login correcto
    # -------------------------
    if resp.status_code == 200:
        # Resetear seguridad para ese email
        st.session_state.login_security[email] = {
            "attempts": 0,
            "blocked_until": None,
        }
        return resp.json()  # tokens + user

    # -------------------------
    # 4) Login incorrecto
    # -------------------------
    sec["attempts"] = sec.get("attempts", 0) + 1

    if sec["attempts"] >= MAX_INTENTOS:
        # Bloqueamos
        blocked_until = ahora_utc + timedelta(minutes=BLOQUEO_MINUTOS)
        sec["blocked_until"] = blocked_until
        st.session_state.login_security[email] = sec

        hora_local = (blocked_until + timedelta(hours=1)).strftime("%H:%M")
        st.error(
            f"Has alcanzado el número máximo de intentos fallidos. "
            f"Tu usuario queda bloqueado durante {BLOQUEO_MINUTOS} minutos. "
            f"Podrás volver a intentarlo a las {hora_local}."
        )
    else:
        st.session_state.login_security[email] = sec
        restantes = MAX_INTENTOS - sec["attempts"]
        st.error(
            f"Email o contraseña incorrectos. "
            f"Intentos restantes antes de bloqueo: {restantes}."
        )

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
            params={"select": "fecha,franja,plaza_id,owner_usa,reservado_por,slot_bloqueado_para"},
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
            "slot_bloqueado_para": s.get("slot_bloqueado_para"),
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
    libres = [s for s in cedidos if s["reservado_por"] is None and (s.get("slot_bloqueado_para") is None)]

    st.markdown("### Semana visible")

    c1, c2, c3 = st.columns(3)
    c1.metric("Franjas cedidas", len(cedidos))
    c2.metric("Cedidas y reservadas", len(reservados))
    c3.metric("Cedidas libres", len(libres))

    # ---------------------------
    # 5) Tablero visual (solo semana visible)
    # ---------------------------
    st.markdown("### Ocupación por día (tablero 50 plazas)")

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

        # Evita KeyError para plazas que no tienen titular (no están en plazas_ids)
        if pid not in plazas_stats:
            continue

        bloqueado = (s.get("slot_bloqueado_para") is not None)

        if s["owner_usa"] is False and s["reservado_por"] is None and not bloqueado:
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
            libres_p = plazas_stats[pid]["libres"]

            color = "🟩" if libres_p == 2 else ("🟦" if libres_p == 1 else "🟥")

            html = f"""
            <div style='text-align:center;font-size:24px;'>
                {color}<br/>
                <span style='font-size:12px;'>P-{pid}</span>
            </div>
            """
            ccols[j].markdown(html, unsafe_allow_html=True)

    # ---------------------------
    # 6) Tabla detalle HISTÓRICA con Mes/Año
    # ---------------------------
    st.markdown("### Detalle de slots")

    filas = []
    for s in sorted(slots_all, key=lambda x: (x["fecha"], x["franja"], x["plaza_id"])):
        franja_txt = "09 - 15" if s["franja"] == "M" else "15 - 21"

        titular = plaza_to_titular.get(s["plaza_id"], "-")
        suplente = id_to_nombre.get(s["reservado_por"], "-") if s["reservado_por"] else "-"

        bloqueado = (s.get("slot_bloqueado_para") is not None)

        if s["owner_usa"] and not s["reservado_por"]:
            estado = "Titular usa"
        elif not s["owner_usa"] and bloqueado and s["reservado_por"] is None:
            estado = "Bloqueado EV (carga)"
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
    # B) 🔌 Tabla ADMIN: Carga EV
    # ---------------------------
    st.markdown("### 🔌 Carga EV (ADMIN)")

    # Rango: mismo que la semana visible (fecha_min .. fecha_max)
    try:
        # 1) Leer asignaciones EV del rango
        resp_ev_asig = requests.get(
            f"{rest_url}/ev_asignaciones",
            headers=headers,
            params={
                "select": "fecha,slot_label,plaza_id,usuario_id,created_at",
                "fecha": f"gte.{fecha_min.isoformat()}",
                "fecha": f"lte.{fecha_max.isoformat()}",
                "order": "fecha.asc,slot_label.asc",
            },
            timeout=10,
        )
        ev_asig_raw = resp_ev_asig.json() if resp_ev_asig.status_code == 200 else []
    except Exception:
        ev_asig_raw = []

    try:
        # 2) Leer solicitudes EV del rango (para ver pendientes/rechazadas)
        resp_ev_sol = requests.get(
            f"{rest_url}/ev_solicitudes",
            headers=headers,
            params={
                "select": "fecha,usuario_id,estado,pref_turno,assigned_slot_label,assigned_plaza_id,updated_at",
                "fecha": f"gte.{fecha_min.isoformat()}",
                "fecha": f"lte.{fecha_max.isoformat()}",
                "order": "fecha.asc,updated_at.desc",
            },
            timeout=10,
        )
        ev_sol_raw = resp_ev_sol.json() if resp_ev_sol.status_code == 200 else []
    except Exception:
        ev_sol_raw = []

    # 3) Mapear asignaciones por (fecha, slot_label)
    ev_asig_map = {}
    for r in ev_asig_raw:
        try:
            f = date.fromisoformat(str(r["fecha"])[:10])
            slot_label = r.get("slot_label")
            ev_asig_map[(f, slot_label)] = r
        except Exception:
            continue

    # 4) Mapear solicitudes por fecha (última por usuario ya viene por updated_at desc)
    #    Aquí hacemos un resumen por día: nº solicitudes, nº pendientes, nº rechazadas, etc.
    ev_sol_by_day = {}
    for r in ev_sol_raw:
        try:
            f = date.fromisoformat(str(r["fecha"])[:10])
        except Exception:
            continue
        ev_sol_by_day.setdefault(f, []).append(r)

    # 5) Determinar plaza EV "reservada" por día leyendo slots bloqueados (slot_bloqueado_para='EV_CHARGE')
    #    Usamos slots_semana (ya cargados arriba) porque incluyen slot_bloqueado_para
    ev_bloqueo_by_day = {}
    for s in slots_semana:
        if s.get("slot_bloqueado_para") != "EV_CHARGE":
            continue
        f = s["fecha"]
        pid = s["plaza_id"]
        fr = s["franja"]
        ev_bloqueo_by_day.setdefault(f, {"plaza_id": pid, "bloqueo": set()})
        # Si por lo que sea hay inconsistencia (otra plaza), lo ignoramos y nos quedamos con la primera
        if ev_bloqueo_by_day[f]["plaza_id"] != pid:
            continue
        ev_bloqueo_by_day[f]["bloqueo"].add(fr)

    def _bloqueo_txt(bset: set) -> str:
        if "M" in bset and "T" in bset:
            return "M+T"
        if "M" in bset:
            return "M"
        if "T" in bset:
            return "T"
        return "—"

    # 6) Construir tabla final por día/slot
    slot_order = ["9 - 12", "12 - 15", "15 - 18", "18 - 21"]

    filas_ev = []
    for d in dias_semana:
        # info bloqueo de plaza EV del día
        blk = ev_bloqueo_by_day.get(d)
        plaza_ev = f"P-{blk['plaza_id']}" if blk else "—"
        bloqueo = _bloqueo_txt(blk["bloqueo"]) if blk else "—"

        # resumen solicitudes del día
        sol_dia = ev_sol_by_day.get(d, [])
        n_sol = len(sol_dia)
        n_pend = sum(1 for x in sol_dia if x.get("estado") == "PENDIENTE")
        n_rech = sum(1 for x in sol_dia if x.get("estado") in ("RECHAZADO", "NO_DISPONIBLE"))
        n_asig = sum(1 for x in sol_dia if x.get("estado") == "ASIGNADO")

        for sl in slot_order:
            asign = ev_asig_map.get((d, sl))

            if asign:
                uid = asign.get("usuario_id")
                nombre = id_to_nombre.get(uid, uid) if uid else "—"
                filas_ev.append({
                    "Fecha": d.strftime("%d/%m/%Y"),
                    "Plaza EV": plaza_ev,
                    "Bloqueo": bloqueo,
                    "Slot": sl,
                    "Asignado a": nombre,
                    "Solicitudes (día)": n_sol,
                    "Pendientes": n_pend,
                    "Asignadas": n_asig,
                    "No disp./rech.": n_rech,
                })
            else:
                filas_ev.append({
                    "Fecha": d.strftime("%d/%m/%Y"),
                    "Plaza EV": plaza_ev,
                    "Bloqueo": bloqueo,
                    "Slot": sl,
                    "Asignado a": "—",
                    "Solicitudes (día)": n_sol,
                    "Pendientes": n_pend,
                    "Asignadas": n_asig,
                    "No disp./rech.": n_rech,
                })

    df_ev = pd.DataFrame(filas_ev)
    st.dataframe(df_ev, use_container_width=True)

    
    # ---------------------------
    # 7) 🏖️ Modo vacaciones (ADMIN)
    # ---------------------------
    st.markdown("### 🏖️ Modo vacaciones (forzar cesión de plaza)")

    with st.expander("Forzar cesión de plaza de un titular en un rango de fechas"):
        # Titulares con plaza asignada
        titulares_con_plaza = [
            u for u in usuarios
            if u.get("rol") == "TITULAR" and u.get("plaza_id") is not None
        ]

        if not titulares_con_plaza:
            st.info("No hay titulares con plaza asignada.")
        else:
            opciones_tit = {
                f"P-{u['plaza_id']} – {u['nombre']}": u
                for u in titulares_con_plaza
            }

            seleccion = st.selectbox(
                "Selecciona titular",
                options=list(opciones_tit.keys()),
                key="vac_titular",
            )

            fecha_inicio = st.date_input(
                "Fecha inicio",
                value=hoy,
                min_value=hoy,
                key="vac_fini",
            )

            fecha_fin = st.date_input(
                "Fecha fin",
                value=hoy + timedelta(days=5),
                min_value=fecha_inicio,
                key="vac_ffin",
            )

            solo_laborables = st.checkbox(
                "Solo lunes a viernes",
                value=True,
                key="vac_laborables",
            )

            if st.button("Aplicar modo vacaciones", key="btn_vacaciones"):
                if fecha_fin < fecha_inicio:
                    st.error("La fecha fin no puede ser anterior a la fecha inicio.")
                else:
                    titular_sel = opciones_tit[seleccion]
                    plaza_id_vac = titular_sel["plaza_id"]

                    try:
                        local_headers = headers.copy()
                        local_headers["Prefer"] = "resolution=merge-duplicates"

                        dia = fecha_inicio
                        total_franjas = 0

                        while dia <= fecha_fin:
                            # Saltar fines de semana si se ha marcado "solo laborables"
                            if solo_laborables and dia.weekday() > 4:
                                dia += timedelta(days=1)
                                continue

                            for fr in ["M", "T"]:
                                payload_slot = [{
                                    "fecha": dia.isoformat(),
                                    "plaza_id": plaza_id_vac,
                                    "franja": fr,
                                    "owner_usa": False,
                                    "estado": "CONFIRMADO",
                                }]

                                r_vac = requests.post(
                                    f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                                    headers=local_headers,
                                    json=payload_slot,
                                    timeout=10,
                                )
                                if r_vac.status_code >= 400:
                                    raise Exception(
                                        f"Error en {dia} {fr}: "
                                        f"{r_vac.status_code} – {r_vac.text}"
                                    )
                                total_franjas += 1

                            dia += timedelta(days=1)

                        st.success(
                            f"Modo vacaciones aplicado para {seleccion} "
                            f"del {fecha_inicio.strftime('%d/%m/%Y')} "
                            f"al {fecha_fin.strftime('%d/%m/%Y')}. "
                            f"{total_franjas} franjas marcadas como cedidas."
                        )
                    except Exception as e:
                        st.error("Error al aplicar el modo vacaciones.")
                        st.code(str(e))
    
    # ---------------------------
    # 7) Sorteo pre-reservas (ADMIN)
    # ---------------------------
    st.markdown("### Sorteo de plazas (ADMIN)")

    # Auditoría global (último sorteo ejecutado)
    last_log = get_last_sorteo_log()
    if last_log:
        st.info(
            f"**Último sorteo ejecutado**: fecha {last_log['fecha']} · "
            f"{format_ts_madrid(last_log.get('executed_at'))} (hora Madrid)"
        )
    else:
        st.warning("Aún no hay registros en sorteos_log.")

    fecha_por_defecto = hoy + timedelta(days=1)

    fecha_sorteo = st.date_input(
        "Fecha para ejecutar / reiniciar el sorteo",
        value=fecha_por_defecto,
        min_value=hoy,
        max_value=hoy + timedelta(days=30),
        key="fecha_sorteo_admin",
    )

    # Auditoría por fecha seleccionada
    log_fecha = get_sorteo_log_for_date(fecha_sorteo)
    if log_fecha:
        st.success(
            f"✅ Sorteo YA ejecutado para {fecha_sorteo.strftime('%d/%m/%Y')} · "
            f"{format_ts_madrid(log_fecha.get('executed_at'))} (hora Madrid)"
        )
    else:
        st.info(
            f"ℹ️ Aún no se ha ejecutado el sorteo para {fecha_sorteo.strftime('%d/%m/%Y')}."
        )

    col_sorteo, col_reset = st.columns(2)

    if col_sorteo.button("Ejecutar sorteo para esta fecha"):
        ejecutar_sorteo(fecha_sorteo)

    if col_reset.button("Reiniciar sorteos de esta fecha"):
        cancelar_sorteo(fecha_sorteo)



def view_titular(profile):
    st.subheader("Panel TITULAR")

    # ============================
    # 0) Validación fuerte de plaza del titular (mitiga VUL-001)
    # ============================
    auth = st.session_state.get("auth")
    if not auth:
        st.error("No se ha podido obtener información de usuario.")
        return

    user_id = auth["user"]["id"]

    # Plaza que dice el perfil (para mostrarla en UI si quieres)
    plaza_id_profile = profile.get("plaza_id")

    rest_url, headers, _ = get_rest_info()

    # Preguntamos a BD cuál es la plaza REAL asignada a este usuario y que sea TITULAR
    try:
        resp_verify = requests.get(
            f"{rest_url}/app_users",
            headers=headers,
            params={
                "select": "plaza_id,rol",
                "id": f"eq.{user_id}",
            },
            timeout=10,
        )
        resp_verify.raise_for_status()
        data = resp_verify.json()
    except Exception as e:
        st.error("Error al verificar permisos sobre la plaza.")
        st.code(str(e))
        return

    if not data:
        st.error("No se ha encontrado tu usuario en app_users.")
        return

    row = data[0]
    if row.get("rol") != "TITULAR":
        st.error("No tienes rol de TITULAR, no puedes gestionar plazas de titular.")
        return

    plaza_id_bd = row.get("plaza_id")
    if plaza_id_bd is None:
        st.error("No tienes ninguna plaza asignada en la base de datos.")
        return

    # A partir de aquí, SOLO usamos plaza_id_bd (validado en BD)
    # Opcional: sincronizar el profile en memoria
    if plaza_id_profile != plaza_id_bd:
        profile["plaza_id"] = plaza_id_bd

    plaza_id = plaza_id_bd  # ESTA es la única plaza válida para operar

    # ============================
    # Codigo post validación
    # ============================

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
    # LÓGICA DE SEMANAS
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
    header_cols[1].markdown("**09 - 15**")
    header_cols[2].markdown("**15 - 21**")
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
    # MODO VACACIONES (rango libre hasta 4 semanas)
    # ---------------------------
    with st.expander("Modo vacaciones (ceder plaza automáticamente por rango de fechas)"):
        rest_url, headers, _ = get_rest_info()

        hoy_vac = date.today()
        max_vac_date = hoy_vac + timedelta(days=28)  # ≈ 4 semanas vista

        st.caption(
            f"Selecciona un rango entre hoy y el {max_vac_date.strftime('%d/%m/%Y')}. "
            "Solo se ceden días laborables (lunes a viernes), ambas franjas."
        )

        col_v1, col_v2 = st.columns(2)

        # Fecha inicio
        vac_ini = col_v1.date_input(
            "Fecha inicio de vacaciones",
            value=hoy_vac,
            min_value=hoy_vac,
            max_value=max_vac_date,
            key="vac_ini_titular",
        )

        # Valor por defecto / previo de la fecha fin
        default_fin = vac_ini + timedelta(days=4)
        if "vac_fin_titular" in st.session_state:
            prev_fin = st.session_state["vac_fin_titular"]
            # Ajustamos por si se ha quedado fuera del rango general
            if prev_fin < hoy_vac:
                prev_fin = hoy_vac
            if prev_fin > max_vac_date:
                prev_fin = max_vac_date
            default_fin = prev_fin

        # Fecha fin (min_value fijo para evitar excepciones de Streamlit)
        vac_fin = col_v2.date_input(
            "Fecha fin de vacaciones",
            value=default_fin,
            min_value=hoy_vac,
            max_value=max_vac_date,
            key="vac_fin_titular",
        )

        if vac_fin < vac_ini:
            st.warning("La fecha fin es anterior al inicio. Ajusta el rango antes de aplicar.")

        # -----------------------------------
        # Botón: aplicar modo vacaciones
        # -----------------------------------
        if st.button("Aplicar modo vacaciones en este rango", key="btn_vacaciones_titular"):
            if vac_fin < vac_ini:
                st.error("Rango de fechas no válido: la fecha fin no puede ser anterior al inicio.")
            else:
                errores_vac = []
                franjas_afectadas = 0

                d = vac_ini
                while d <= vac_fin:
                    # Solo lunes-viernes
                    if d.weekday() < 5 and se_puede_modificar_cesion(d):
                        for fr in ("M", "T"):
                            payload = [{
                                "fecha": d.isoformat(),
                                "plaza_id": plaza_id,
                                "franja": fr,
                                "owner_usa": False,      # cedida
                                "estado": "CONFIRMADO",
                            }]

                            local_headers = headers.copy()
                            local_headers["Prefer"] = "resolution=merge-duplicates"

                            try:
                                r_vac = requests.post(
                                    f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                                    headers=local_headers,
                                    json=payload,
                                    timeout=10,
                                )
                                if r_vac.status_code >= 400:
                                    errores_vac.append(
                                        f"{d.strftime('%d/%m/%Y')} {fr}: "
                                        f"{r_vac.status_code} – {r_vac.text}"
                                    )
                                else:
                                    franjas_afectadas += 1
                            except Exception as e:
                                errores_vac.append(
                                    f"{d.strftime('%d/%m/%Y')} {fr}: excepción {e}"
                                )
                    d += timedelta(days=1)

                if errores_vac:
                    st.error("Se han producido errores al aplicar las vacaciones:")
                    for e in errores_vac:
                        st.code(e)
                else:
                    st.success(
                        f"Modo vacaciones aplicado correctamente. "
                        f"Franjas cedidas en el rango: {franjas_afectadas}."
                    )
                    st.rerun()

        # -----------------------------------
        # Botón: cancelar cesiones futuras
        # -----------------------------------
        st.markdown("---")
        st.caption(
            "Si te has equivocado, puedes resetear todas las franjas cedidas "
            "de tu plaza a partir de mañana, siempre que no tengan ya un suplente "
            "reservado."
        )

        if st.button("Cancelar cesiones futuras (reset desde mañana)", key="btn_cancel_vac_titular"):
            manana = hoy_vac + timedelta(days=1)

            try:
                resp_reset = requests.patch(
                    f"{rest_url}/slots",
                    headers=headers,
                    params={
                        "plaza_id": f"eq.{plaza_id}",
                        "fecha": f"gte.{manana.isoformat()}",
                        "owner_usa": "eq.false",
                        "reservado_por": "is.null",
                    },
                    json={"owner_usa": True},
                    timeout=10,
                )

                if resp_reset.status_code >= 400:
                    st.error("Error al cancelar las cesiones futuras.")
                    st.code(resp_reset.text)
                else:
                    st.success(
                        "Cesiones futuras sin suplente asignado canceladas correctamente. "
                        "A partir de mañana vuelves a aparecer como 'Titular usa' en esas franjas."
                    )
                    st.rerun()

            except Exception as e:
                st.error("Error inesperado al cancelar las cesiones futuras.")
                st.code(str(e))
  
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

    # user_id autenticado
    auth = st.session_state.get("auth")
    if not auth:
        st.error("No se ha podido obtener información de usuario.")
        return
    user_id = auth["user"]["id"]

    rest_url, headers, _ = get_rest_info()
    hoy = date.today()

    # Headers autenticados como el usuario (para RLS en pre_reservas)
    access_token = auth.get("access_token")
    user_headers = headers.copy()
    user_headers["Authorization"] = f"Bearer {access_token}"

    # ============================
    # 1) KPI uso mensual
    # ============================
    first_day = hoy.replace(day=1)
    next_month_first = (
        first_day.replace(
            month=(first_day.month % 12) + 1,
            year=first_day.year + (first_day.month // 12),
        )
        if first_day.month == 12
        else date(first_day.year, first_day.month + 1, 1)
    )

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
    except Exception:
        usadas_raw = []

    usadas_mes = []
    for x in usadas_raw:
        try:
            f = date.fromisoformat(x["fecha"][:10])
            if first_day <= f < next_month_first:
                usadas_mes.append(f)
        except Exception:
            pass

    st.write(f"Franjas utilizadas este mes: **{len(usadas_mes)}**")

    # ============================
    # 2) Próximas reservas / solicitudes (agenda completa futura)
    # ============================
    try:
        r = requests.get(
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
        slots_user_raw = r.json()
    except Exception:
        slots_user_raw = []

    slots_user = {}
    for s in slots_user_raw:
        try:
            f = date.fromisoformat(s["fecha"][:10])
            slots_user[(f, s["franja"])] = s["plaza_id"]
        except Exception:
            pass

    try:
        r = requests.get(
            f"{rest_url}/pre_reservas",
            headers=user_headers,   # <-- IMPORTANTE: leer con user_headers
            params={
                "select": "fecha,franja,estado",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc,franja.asc",
            },
            timeout=10,
        )
        pre_user_raw = r.json()
    except Exception:
        pre_user_raw = []

    # ============================
    # EV: asignaciones / solicitudes futuras
    # ============================
    try:
        r = requests.get(
            f"{rest_url}/ev_asignaciones",
            headers=headers,
            params={
                "select": "fecha,plaza_id,slot_label",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc",
            },
            timeout=10,
        )
        ev_asig_raw = r.json()
    except Exception:
        ev_asig_raw = []

    ev_asig = {}
    for row in ev_asig_raw:
        try:
            f = date.fromisoformat(row["fecha"][:10])
            ev_asig[f] = {
                "plaza_id": row.get("plaza_id"),
                "slot_label": row.get("slot_label"),
            }
        except Exception:
            pass

    try:
        r = requests.get(
            f"{rest_url}/ev_solicitudes",
            headers=headers,
            params={
                "select": "fecha,estado,pref_turno,assigned_plaza_id,assigned_slot_label",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"gte.{hoy.isoformat()}",
                "order": "fecha.asc",
            },
            timeout=10,
        )
        ev_sol_raw = r.json()
    except Exception:
        ev_sol_raw = []

    ev_sol = {}
    for row in ev_sol_raw:
        try:
            f = date.fromisoformat(row["fecha"][:10])
            if row.get("estado") != "CANCELADO":
                ev_sol[f] = row
        except Exception:
            pass
    
    pre_user = {}
    for rpr in pre_user_raw:
        try:
            if rpr["estado"] != "CANCELADO":
                f = date.fromisoformat(rpr["fecha"][:10])
                pre_user[(f, rpr["franja"])] = rpr["estado"]
        except Exception:
            pass

    claves = sorted(
        set(slots_user.keys()) | set(pre_user.keys()),
        key=lambda x: (x[0], x[1]),
    )

    st.markdown("### 🔜 Tus próximas reservas / solicitudes")
    if not claves:
        st.markdown("_No tienes reservas ni solicitudes futuras._")
    else:
        out_lines = []
        for (f, franja) in claves:
            fr_txt = "09 - 15" if franja == "M" else "15 - 21"
            fecha_txt = f.strftime("%a %d/%m")

            # 1) Si ya hay slot asignado, prioridad absoluta
            if (f, franja) in slots_user:
                plaza = slots_user[(f, franja)]
                out_lines.append(
                    f"- {fecha_txt} – {fr_txt} – Plaza **P-{plaza}** (asignada)"
                )
                continue

            # 2) Si no hay slot, miramos pre_reservas
            est = pre_user.get((f, franja))
            if est is None:
                est = "PENDIENTE"

            if est == "PENDIENTE":
                out_lines.append(
                    f"- {fecha_txt} – {fr_txt} – _Solicitud pendiente de plaza_"
                )
            elif est == "ASIGNADO":
                out_lines.append(
                    f"- {fecha_txt} – {fr_txt} – _Plaza asignada (pendiente)_"
                )
            elif est == "RECHAZADO":
                out_lines.append(
                    f"- {fecha_txt} – {fr_txt} – _Solicitud no aprobada_"
                )
            else:
                out_lines.append(
                    f"- {fecha_txt} – {fr_txt} – _Solicitud no aprobada_"
                )

        st.markdown("\n".join(out_lines))

    # Añadir resumen EV (por día)
    # Mostramos solo días donde haya solicitud o asignación EV
    dias_ev = sorted(set(ev_asig.keys()) | set(ev_sol.keys()))
    if dias_ev:
        out_ev = []
        out_ev.append("")
        out_ev.append("**🔌 Carga EV (slots de 3h)**")

        for f in dias_ev:
            fecha_txt = f.strftime("%a %d/%m")

            # Si ya hay asignación, prioridad
            if f in ev_asig:
                plaza = ev_asig[f].get("plaza_id")
                slot = ev_asig[f].get("slot_label")
                out_ev.append(
                    f"- {fecha_txt} – Carga EV asignada: **{slot}** (P-{plaza})"
                )
                continue

            sol = ev_sol.get(f)
            if not sol:
                continue

            est = sol.get("estado")
            pref = sol.get("pref_turno")
            pref_txt = {"M": "mañana", "T": "tarde", "ANY": "mañana/tarde"}.get(pref, "—")

            if est == "PENDIENTE":
                out_ev.append(
                    f"- {fecha_txt} – Solicitud carga EV pendiente ({pref_txt})"
                )
            elif est == "ASIGNADO":
                slot = sol.get("assigned_slot_label") or "—"
                plaza = sol.get("assigned_plaza_id") or "—"
                out_ev.append(
                    f"- {fecha_txt} – Carga EV asignada: **{slot}** (P-{plaza})"
                )
            elif est in ("RECHAZADO", "NO_DISPONIBLE"):
                out_ev.append(
                    f"- {fecha_txt} – Carga EV: no disponible"
                )
            else:
                out_ev.append(
                    f"- {fecha_txt} – Carga EV: {est}"
                )

        st.markdown("\n".join(out_ev))

    # ============================
    # 3) Semana inteligente
    # ============================
    weekday = hoy.weekday()

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
    # 4) Leer slots agregados (todas plazas de esos días)
    # ============================
    try:
        r = requests.get(
            f"{rest_url}/slots",
            headers=headers,
            params={
                "select": "fecha,franja,owner_usa,reservado_por,plaza_id",
            },
            timeout=10,
        )
        slots_raw = r.json()
    except Exception:
        slots_raw = []

    from collections import defaultdict

    libres = defaultdict(int)
    reservas_user_sem = {}

    for s in slots_raw:
        try:
            f = date.fromisoformat(s["fecha"][:10])
        except Exception:
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
            headers=user_headers,   # <-- IMPORTANTE: también aquí
            params={
                "select": "fecha,franja,estado,pack_id",
                "usuario_id": f"eq.{user_id}",
                "fecha": f"in.({','.join(d.isoformat() for d in dias_semana)})",
                "order": "fecha.asc,franja.asc",
            },
            timeout=10,
        )
        pre_sem_raw = r.json()
    except Exception:
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
        except Exception:
            pass

    # ============================
    # 6) UI tipo titular: checkboxes
    # ============================
    st.markdown("### Selecciona las franjas que deseas solicitar:")

    header = st.columns(6)
    header[0].markdown("**Día**")
    header[1].markdown("**09 - 15**")
    header[2].markdown("**15 - 21**")
    header[3].markdown("**Día completo**")
    header[4].markdown("**EV M**")
    header[5].markdown("**EV T**")

    cambios = {}

    for d in dias_semana:
        is_today = (d == hoy)

        with st.container():
            cols = st.columns(6)
            cols[0].write(d.strftime("%a %d/%m"))

            slot_M = reservas_user_sem.get((d, "M"))
            slot_T = reservas_user_sem.get((d, "T"))

            pre_M = pre_sem.get((d, "M"))
            pre_T = pre_sem.get((d, "T"))

            adjud_full = slot_M is not None and slot_T is not None

            editable = se_puede_modificar_slot(d, "reservar")

            # plazas libres por franja para este día
            disp_M = libres.get((d, "M"), 0)
            disp_T = libres.get((d, "T"), 0)

            # ---------------------------
            # EV (solo UI): precargar desde ev_sol (si existe)
            # ---------------------------
            ev_prev = ev_sol.get(d)
            ev_prev_m = bool(ev_prev) and ev_prev.get("pref_turno") in ("M", "ANY")
            ev_prev_t = bool(ev_prev) and ev_prev.get("pref_turno") in ("T", "ANY")

            # Si hoy no hay plazas (ya lo gestionas), EV también lo bloqueamos por coherencia
            if editable:
                cols[4].checkbox(
                    "",
                    value=ev_prev_m,
                    key=f"ev_m_{d.isoformat()}",
                    label_visibility="collapsed"
                )
                cols[5].checkbox(
                    "",
                    value=ev_prev_t,
                    key=f"ev_t_{d.isoformat()}",
                    label_visibility="collapsed"
                )
            else:
                cols[4].markdown("—")
                cols[5].markdown("—")

            # Día completo
            key_full = f"full_{d.isoformat()}"
            default_full = (
                pre_M is not None
                and pre_T is not None
                and pre_M != "RECHAZADO"
                and pre_T != "RECHAZADO"
                and slot_M is None
                and slot_T is None
            )

            if adjud_full:
                cols[3].markdown("🟩 Día completo adjudicado")
                cambios[(d, "FULL")] = "NOACCION"
                cols[1].markdown("—")
                cols[2].markdown("—")
            else:
                if editable:
                    full_checked = cols[3].checkbox(
                        "", value=default_full, key=key_full, label_visibility="collapsed"
                    )
                else:
                    full_checked = default_full
                    cols[3].markdown("—")

                if full_checked:
                    texto_M = "_Incluida en día completo_"
                    texto_T = "_Incluida en día completo_"

                    texto_M += (
                        f"<br/><span style='font-size:11px;color:#0a0;'>"
                        f"Plazas libres: {disp_M}</span>"
                    )
                    texto_T += (
                        f"<br/><span style='font-size:11px;color:#0a0;'>"
                        f"Plazas libres: {disp_T}</span>"
                    )

                    cols[1].markdown(texto_M, unsafe_allow_html=True)
                    cols[2].markdown(texto_T, unsafe_allow_html=True)

                    cambios[(d, "FULL")] = "SOLICITAR"
                else:
                    cambios[(d, "FULL")] = "NOFULL"

                    # Franjas individuales
                    for fr_idx, fr in enumerate(["M", "T"], start=1):
                        col = cols[fr_idx]
                        key = (d, fr)

                        # Si ya tiene plaza asignada
                        if (d, fr) in reservas_user_sem:
                            plaza = reservas_user_sem[(d, fr)]
                            col.markdown(f"🟩 Adjudicada P-{plaza}")
                            cambios[(d, fr)] = "NOACCION"
                            continue

                        # Plazas libres para esta franja
                        disp = libres.get((d, fr), 0)

                        # Hoy sin plazas → franja completa
                        if is_today and disp == 0:
                            col.markdown(
                                "<span style='font-size:11px;color:#a00;'>"
                                "Franja completa (0 plazas libres)</span>",
                                unsafe_allow_html=True,
                            )
                            cambios[(d, fr)] = "NOACCION"
                            continue

                        estado_pre = pre_sem.get(key)

                        # 🔴 Si la solicitud fue rechazada, solo mostramos mensaje
                        if estado_pre == "RECHAZADO":
                            col.markdown(
                                "<span style='font-size:13px;color:#a00;'>"
                                "Solicitud no aprobada</span>",
                                unsafe_allow_html=True,
                            )
                            cambios[(d, fr)] = "NOACCION"
                            continue

                        # Para PENDIENTE / ASIGNADO
                        marc = estado_pre in ("PENDIENTE", "ASIGNADO")

                        if editable:
                            marc = col.checkbox(
                                "Solicitar",
                                value=marc,
                                key=f"chk_{d.isoformat()}_{fr}",
                            )
                        else:
                            col.markdown("—")

                        if disp > 0:
                            col.markdown(
                                f"<span style='font-size:11px;color:#0a0;'>"
                                f"Plazas libres: {disp}</span>",
                                unsafe_allow_html=True,
                            )
                        else:
                            if not is_today:
                                col.markdown(
                                    "<span style='font-size:11px;color:#a00;'>"
                                    "Plazas libres: 0</span>",
                                    unsafe_allow_html=True,
                                )

                        cambios[(d, fr)] = "SOLICITAR" if marc else "NOACCION"

    # ============================
    # 7) Guardar cambios
    # ============================
    st.markdown("---")
    if st.button("💾 Guardar cambios"):
        errores = []
        try:
            for (d, fr) in cambios:
                accion = cambios[(d, fr)]

                # PACK día completo
                if fr == "FULL":
                    if accion == "SOLICITAR":
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
                        resp_full = requests.post(
                            f"{rest_url}/pre_reservas",
                            headers=user_headers,
                            json=payload,
                            timeout=10,
                        )
                        if resp_full.status_code >= 400:
                            errores.append(
                                f"Error creando pre-reservas de día completo para {d}: "
                                f"{resp_full.status_code} – {resp_full.text}"
                            )
                    continue

                # Franjas individuales
                f = fr
                esta_pre = (d, f) in pre_sem
                esta_slot = (d, f) in reservas_user_sem

                if accion == "SOLICITAR":
                    # Hoy → reserva inmediata
                    if d == hoy:
                        if not esta_slot:
                            resp_libre = requests.get(
                                f"{rest_url}/slots",
                                headers=headers,
                                params={
                                    "select": "plaza_id",
                                    "fecha": f"eq.{d.isoformat()}",
                                    "franja": f"eq.{f}",
                                    "owner_usa": "eq.false",
                                    "reservado_por": "is.null",
                                    "order": "plaza_id.asc",
                                    "limit": "1",
                                },
                                timeout=10,
                            )
                            if resp_libre.status_code == 200:
                                libres_hoy = resp_libre.json()
                                if libres_hoy:
                                    plaza_id = libres_hoy[0]["plaza_id"]
                                    payload_slot = [
                                        {
                                            "fecha": d.isoformat(),
                                            "plaza_id": plaza_id,
                                            "franja": f,
                                            "owner_usa": False,
                                            "reservado_por": user_id,
                                            "estado": "CONFIRMADO",
                                        }
                                    ]
                                    local_headers = headers.copy()
                                    local_headers["Prefer"] = (
                                        "resolution=merge-duplicates"
                                    )

                                    r_upd = requests.post(
                                        f"{rest_url}/slots?on_conflict=fecha,plaza_id,franja",
                                        headers=local_headers,
                                        json=payload_slot,
                                        timeout=10,
                                    )
                                    if r_upd.status_code >= 400:
                                        errores.append(
                                            f"Error reservando hoy {d} {f}: "
                                            f"{r_upd.status_code} – {r_upd.text}"
                                        )

                                    if esta_pre:
                                        try:
                                            r_patch = requests.patch(
                                                f"{rest_url}/pre_reservas",
                                                headers=user_headers,
                                                params={
                                                    "usuario_id": f"eq.{user_id}",
                                                    "fecha": f"eq.{d.isoformat()}",
                                                    "franja": f"eq.{f}",
                                                    "estado": "eq.PENDIENTE",
                                                },
                                                json={"estado": "ASIGNADO"},
                                                timeout=10,
                                            )
                                            if r_patch.status_code >= 400:
                                                errores.append(
                                                    f"Error actualizando pre-reserva hoy {d} {f}: "
                                                    f"{r_patch.status_code} – {r_patch.text}"
                                                )
                                        except Exception as e:
                                            errores.append(
                                                f"Excepción al actualizar pre-reserva hoy {d} {f}: {e}"
                                            )
                                else:
                                    st.warning(
                                        f"No queda hueco disponible en la franja "
                                        f"{'08-14' if f == 'M' else '14-20'}."
                                    )
                            else:
                                errores.append(
                                    f"Error buscando plaza libre hoy {d} {f}: "
                                    f"{resp_libre.status_code} – {resp_libre.text}"
                                )

                    # Futuro → solo pre-reserva
                    else:
                        if not esta_pre and not esta_slot:
                            payload = [
                                {
                                    "usuario_id": user_id,
                                    "fecha": d.isoformat(),
                                    "franja": f,
                                    "estado": "PENDIENTE",
                                }
                            ]
                            resp_pre = requests.post(
                                f"{rest_url}/pre_reservas",
                                headers=user_headers,
                                json=payload,
                                timeout=10,
                            )
                            if resp_pre.status_code >= 400:
                                errores.append(
                                    f"Error creando pre-reserva para {d} {f}: "
                                    f"{resp_pre.status_code} – {resp_pre.text}"
                                )

                elif accion == "NOACCION":
                    if esta_pre:
                        resp_cancel = requests.patch(
                            f"{rest_url}/pre_reservas",
                            headers=user_headers,
                            params={
                                "usuario_id": f"eq.{user_id}",
                                "fecha": f"eq.{d.isoformat()}",
                                "franja": f"eq.{f}",
                                "estado": "in.(PENDIENTE,ASIGNADO)",
                            },
                            json={"estado": "CANCELADO"},
                            timeout=10,
                        )
                        if resp_cancel.status_code >= 400:
                            errores.append(
                                f"Error cancelando pre-reserva para {d} {f}: "
                                f"{resp_cancel.status_code} – {resp_cancel.text}"
                            )

            # ============================
            # 7.B) Guardar solicitudes EV (1 fila por día / sin duplicados)
            # ============================
            for d in dias_semana:
                ev_m = st.session_state.get(f"ev_m_{d.isoformat()}", False)
                ev_t = st.session_state.get(f"ev_t_{d.isoformat()}", False)

                if ev_m and ev_t:
                    pref = "ANY"
                elif ev_m:
                    pref = "M"
                elif ev_t:
                    pref = "T"
                else:
                    pref = None

                if pref:
                    r_ev = ev_upsert_solicitud(
                        fecha_obj=d,
                        usuario_id=user_id,
                        pref_turno=pref,
                        estado="PENDIENTE",
                    )
                    if getattr(r_ev, "status_code", 500) >= 400:
                        errores.append(
                            f"Error guardando EV {d.strftime('%d/%m/%Y')}: "
                            f"{r_ev.status_code} – {r_ev.text}"
                        )
                else:
                    r_ev = ev_cancelar_solicitud(
                        fecha_obj=d,
                        usuario_id=user_id,
                    )
                    if getattr(r_ev, "status_code", 500) >= 400:
                        errores.append(
                            f"Error cancelando EV {d.strftime('%d/%m/%Y')}: "
                            f"{r_ev.status_code} – {r_ev.text}"
                        )

            if errores:
                st.error("Se han producido errores al guardar las solicitudes:")
                for e in errores:
                    st.code(e)
            else:
                st.success("Cambios guardados correctamente.")
                st.rerun()

        except Exception as e:
            st.error("Error inesperado al guardar cambios.")
            st.code(str(e))
            return

# ---------------------------------------------
# MAIN
# ---------------------------------------------
def main():
    st.image("Logo_KM0.png", width=200)
    st.title("Parking KM0")

    rest_url, headers, anon_key = get_rest_info()

    # Estado de sesión
    if "auth" not in st.session_state:
        st.session_state.auth = None
    if "profile" not in st.session_state:
        st.session_state.profile = None
    if "last_auto_draw_date" not in st.session_state:
        st.session_state.last_auto_draw_date = None   # ← controla que no se repita el sorteo

    # ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈
    # LOGIN
    # ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈
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
        return

    # ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈
    # VALIDAR TOKEN
    # ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈
    auth_data = st.session_state.auth
    access_token = auth_data.get("access_token")

    if not access_token or is_jwt_expired(access_token):
        st.warning("Tu sesión ha caducado. Por favor, vuelve a iniciar sesión.")
        st.session_state.auth = None
        st.session_state.profile = None
        st.rerun()
        return

    user = auth_data["user"]
    user_id = user["id"]
    email = user["email"]

    # Cargar perfil si aún no está
    if st.session_state.profile is None:
        profile = load_profile(user_id)
        if profile is None:
            st.error("No se ha encontrado un perfil en app_users para este usuario.")
            st.info("Da de alta este usuario en app_users y recarga.")
            if st.button("Cerrar sesión"):
                st.session_state.auth = None
                st.session_state.profile = None
                st.rerun()
            return
        st.session_state.profile = profile

    profile = st.session_state.profile

    # ----------------------------------------------------------
    # 🔥 EJECUCIÓN AUTOMÁTICA DEL SORTEO A PARTIR DE LAS 20:00
    # ----------------------------------------------------------

    hoy = date.today()
    ahora = datetime.now().time()
    limite = time(20, 0)
    fecha_sorteo = hoy + timedelta(days=1)

    # ¿Ya se ejecutó hoy automáticamente?
    ya_ejecutado_hoy = (st.session_state.last_auto_draw_date == hoy)

    if ahora >= limite and not ya_ejecutado_hoy:
        st.info("⏳ Ejecutando sorteo automático…")
        ejecutar_sorteo(fecha_sorteo)
        st.session_state.last_auto_draw_date = hoy
        st.rerun()
        return

    # ----------------------------------------------------------
    # Cabecera común
    # ----------------------------------------------------------
    st.success(f"Sesión iniciada como: {email}")
    st.write(f"Rol: **{profile['rol']}**")

    if st.button("Cerrar sesión"):
        st.session_state.auth = None
        st.session_state.profile = None
        st.rerun()

    password_change_panel()

    st.markdown("---")

    # ----------------------------------------------------------
    # VISTA SEGÚN ROL
    # ----------------------------------------------------------
    rol = profile["rol"]
    if rol == "ADMIN":
        view_admin(profile)
    elif rol == "TITULAR":
        view_titular(profile)
    elif rol == "SUPLENTE":
        view_suplente(profile)
    else:
        st.error(f"Rol desconocido: {rol}")


# ---------------------------------------------
# EJECUCIÓN REAL DEL PROGRAMA
# ---------------------------------------------
if __name__ == "__main__":
    main()
