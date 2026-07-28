import io
import json
import os
import re
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash

import db
from conciliador import (BANK_ACCOUNTS_FILE, EMPRESAS_FILE, LOGO_FILE, MASTER_FILE, build_master_bytes,
                          gather_statements, hash_record, load_bank_accounts, load_companies,
                          account_matches, normalize_cuit, normalize_text)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / 'uploads'
USERS_FILE = BASE_DIR / 'users.json'
USERS_EXAMPLE_FILE = BASE_DIR / 'users.example.json'
# DATABASE_URL (ej. postgresql+psycopg2://user:pass@host:5432/db) tiene
# prioridad para producción; sin ella, cae al archivo SQLite de siempre
# (dev/tests, cero configuración).
DB_PATH = os.environ.get('DATABASE_URL') or os.environ.get('CONCILIADOR_DB_PATH', str(BASE_DIR / 'conciliador.db'))
EMPRESAS_PATH = BASE_DIR / EMPRESAS_FILE.name
USER_ROLES = ['admin', 'uploader', 'viewer', 'cajera', 'finanzas']

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'si', 'sí')


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # 'Lax' no manda la cookie en pedidos cross-site que no sean navegación de
    # nivel superior: es defensa en profundidad detrás del CSRF de abajo.
    SESSION_COOKIE_SAMESITE='Lax',
    # El deploy real está detrás de HTTPS (Caddy), así que la cookie va marcada
    # Secure por defecto. Para desarrollo local por HTTP hay que apagarlo, o el
    # navegador directamente no la manda (ver el bloque __main__).
    SESSION_COOKIE_SECURE=not _env_flag('CONCILIADOR_INSECURE_COOKIES'),
)

# Todos los POST del sitio llevan token CSRF; sin él, cualquier página externa
# podía hacer que el navegador de un usuario logueado subiera archivos, tocara
# CI o borrara movimientos con su sesión.
csrf = CSRFProtect(app)

# Detrás de Caddy, request.remote_addr es siempre la IP del proxy: sin esto, el
# límite de intentos de login trataría a todos los usuarios como uno solo. Se
# activa por variable de entorno porque confiar en X-Forwarded-For sin un proxy
# adelante permitiría falsear la IP de origen.
if _env_flag('TRUST_PROXY_HEADERS'):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

NO_USERS_ERROR = ('No hay usuarios configurados: falta el archivo users.json. '
                  'Copiá users.example.json a users.json y cargá los usuarios reales.')

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
LOGIN_THROTTLED_ERROR = (f'Demasiados intentos fallidos. Esperá {LOGIN_WINDOW_SECONDS // 60} minutos '
                         'antes de volver a intentar.')
# Fallos recientes por (IP, usuario). Vive en memoria: se pierde al reiniciar y
# no se comparte entre procesos, suficiente para frenar fuerza bruta contra un
# solo waitress. Si algún día se corre con varios workers, esto tiene que pasar
# a la base o a un Redis.
_login_failures: dict[str, list[float]] = {}


def _login_throttle_key(username: str) -> str:
    # Por IP + usuario, no solo por usuario: si no, cualquiera desde afuera
    # podría dejar afuera a un compañero fallando cinco veces con su nombre.
    return f'{request.remote_addr or "?"}|{username.strip().lower()}'


def _recent_login_failures(key: str) -> list[float]:
    now = time.monotonic()
    failures = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if failures:
        _login_failures[key] = failures
    else:
        _login_failures.pop(key, None)
    return failures


def login_is_throttled(username: str) -> bool:
    return len(_recent_login_failures(_login_throttle_key(username))) >= LOGIN_MAX_ATTEMPTS


def register_login_failure(username: str) -> bool:
    """Anota un intento fallido. Devuelve True si con este se llegó al tope."""
    if len(_login_failures) > 1000:
        for stale_key in [k for k in list(_login_failures) if not _recent_login_failures(k)]:
            _login_failures.pop(stale_key, None)
    key = _login_throttle_key(username)
    failures = _recent_login_failures(key)
    failures.append(time.monotonic())
    _login_failures[key] = failures
    return len(failures) == LOGIN_MAX_ATTEMPTS


def clear_login_failures(username: str) -> None:
    _login_failures.pop(_login_throttle_key(username), None)


def load_users() -> list[dict[str, object]]:
    # users.json trae credenciales reales, así que no está en el repo (ver
    # users.example.json). Importar la app no puede depender de él: sin el
    # archivo la app arranca igual y el login explica qué falta, en vez de
    # tirar FileNotFoundError en tiempo de import y dejar todo — tests
    # incluidos — sin poder ni cargarse.
    try:
        with USERS_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('users', [])
    except Exception:
        return []


def save_users(users: list[dict[str, object]]) -> None:
    with USERS_FILE.open('w', encoding='utf-8') as f:
        json.dump({'users': users}, f, indent=2, ensure_ascii=False)


users_by_name = {user['username']: user for user in load_users()}


def get_user_roles(user: dict) -> list[str]:
    # Un usuario puede tener más de un rol (ej. finanzas Y cajera: sube
    # extractos, ve Saldo y además carga CI). Se acepta el campo 'roles' con
    # una lista y, por compatibilidad con los usuarios viejos, el 'role' suelto.
    roles = user.get('roles')
    if isinstance(roles, list):
        return [str(r).strip().lower() for r in roles if str(r).strip()]
    role = user.get('role')
    return [str(role).strip().lower()] if role else []


def session_roles() -> list[str]:
    return session.get('roles') or ([session['role']] if session.get('role') else [])


def has_role(*names: str) -> bool:
    return bool(set(session_roles()) & set(names))


# Disponibles en los templates para no repetir la lógica de roles en el HTML.
app.jinja_env.globals['has_role'] = has_role
app.jinja_env.globals['user_roles'] = get_user_roles


def puede_ver_saldo() -> bool:
    # El Saldo se le oculta a las cajeras, pero quien además es finanzas (o
    # admin) sí tiene que verlo: la restricción es del rol cajera "puro".
    return not has_role('cajera') or has_role('admin', 'finanzas')


def current_user() -> dict:
    return users_by_name.get(session.get('username', ''), {})


def empresa_matches(empresa: object, clave: str) -> bool:
    # users.json guarda la empresa con su nombre corto (ALCO, DASEOS...) y los
    # movimientos traen la razón social completa de Empresas.xlsx ("ALCO
    # ROSARIO S.A."). Se compara por contenido normalizado para que un cambio
    # de redacción en el maestro no rompa las asignaciones.
    clave_norm = normalize_text(clave)
    return bool(clave_norm) and clave_norm in normalize_text(str(empresa or ''))


def empresas_primarias_de(user: dict) -> list[str]:
    valores = user.get('empresas_primarias') or []
    return [str(v).strip() for v in valores if str(v).strip()]


def es_empresa_primaria(empresa: object, user: dict) -> bool:
    primarias = empresas_primarias_de(user)
    # Sin empresas asignadas (admin, usuarios viejos) no hay nada que avisar:
    # todo cuenta como propio.
    if not primarias:
        return True
    return any(empresa_matches(empresa, clave) for clave in primarias)


def guardar_ci(changes: list[tuple[str, str]], old_ci_map: dict) -> None:
    for record_hash, ci_value in changes:
        db.update_ci(record_hash, ci_value, db_path=DB_PATH)
        db.log_action(
            session.get('username'), 'update_ci',
            detail={'antes': old_ci_map.get(record_hash, ''), 'despues': ci_value},
            record_hash=record_hash, db_path=DB_PATH,
        )


def build_pending_ci_rows(pendientes: list[tuple[str, str]], df_full: pd.DataFrame) -> list[dict]:
    # Datos suficientes para que se reconozca cada movimiento en la pantalla de
    # confirmación, sin tener que volver a la grilla.
    por_hash = df_full.set_index('RecordHash')
    filas = []
    for record_hash, ci_value in pendientes:
        if record_hash not in por_hash.index:
            continue
        fila = por_hash.loc[record_hash]
        filas.append({
            'RecordHash': record_hash,
            'ci_nuevo': ci_value,
            'Empresa': fila['Empresa'],
            'Fecha_display': fila['Fecha_display'],
            'Banco': fila['Banco'],
            'Descripcion': fila['Descripcion'],
            'Monto': fila['Monto'],
            'CI': fila['CI'],
        })
    return filas


def split_ci_changes(changes: list[tuple[str, str]], empresa_por_hash: dict,
                      user: dict) -> tuple[list, list]:
    """Separa los CI a guardar entre empresas propias y del resto.

    Los de empresas primarias se guardan directo; los demás se le muestran a
    la usuaria para que confirme o corrija antes de escribirlos.
    """
    propias, ajenas = [], []
    for record_hash, ci_value in changes:
        empresa = empresa_por_hash.get(record_hash, '')
        destino = propias if es_empresa_primaria(empresa, user) else ajenas
        destino.append((record_hash, ci_value))
    return propias, ajenas


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'username' not in session:
                return redirect(url_for('login'))
            if role and not has_role(*role):
                return render_template('no_access.html', role=', '.join(session_roles())), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route('/', methods=['GET'])
def login():
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def do_login():
    username = request.form.get('username', '')
    password = request.form.get('password', '')
    if not users_by_name:
        return render_template('login.html', error=NO_USERS_ERROR)
    if login_is_throttled(username):
        # Sin auditar: durante un ataque, cada intento bloqueado sería una fila
        # más en audit_log. El bloqueo ya quedó registrado una vez, abajo.
        return render_template('login.html', error=LOGIN_THROTTLED_ERROR), 429
    user = users_by_name.get(username)
    if not user or not check_password_hash(user['password'], password):
        db.log_action(username or '(desconocido)', 'login_failed', db_path=DB_PATH)
        if register_login_failure(username):
            db.log_action(username or '(desconocido)', 'login_blocked', db_path=DB_PATH)
            return render_template('login.html', error=LOGIN_THROTTLED_ERROR), 429
        return render_template('login.html', error='Usuario o contraseña incorrectos')
    clear_login_failures(username)
    roles = get_user_roles(user)
    session['username'] = username
    session['roles'] = roles
    # 'role' se mantiene para lo que solo muestra un rol (chip del header).
    session['role'] = roles[0] if roles else ''
    session['nombre'] = user.get('nombre', '')
    db.log_action(username, 'login_success', db_path=DB_PATH)
    return redirect(url_for('records'))


@app.route('/logout')
def logout():
    if 'username' in session:
        db.log_action(session['username'], 'logout', db_path=DB_PATH)
    session.clear()
    return redirect(url_for('login'))


def get_display_name() -> str:
    return session.get('nombre') or session.get('username') or ''


@app.route('/change-password', methods=['GET', 'POST'])
@login_required()
def change_password():
    global users_by_name
    message = None
    error = None
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        username = session.get('username')
        users = load_users()
        user = next((u for u in users if u['username'] == username), None)
        if not user or not check_password_hash(user['password'], current_password):
            error = 'La contraseña actual no es correcta.'
        elif not is_valid_password(new_password):
            error = 'La nueva contraseña debe ser alfanumérica y tener al menos 8 caracteres.'
        elif new_password != confirm_password:
            error = 'Las contraseñas nuevas no coinciden.'
        else:
            user['password'] = generate_password_hash(new_password)
            save_users(users)
            users_by_name = {u['username']: u for u in users}
            db.log_action(username, 'password_change', db_path=DB_PATH)
            message = 'Contraseña actualizada correctamente.'
    return render_template(
        'change_password.html', role=session.get('role'), display_name=get_display_name(),
        message=message, error=error,
    )


@app.route('/upload', methods=['GET', 'POST'])
@login_required(role=['admin', 'uploader', 'finanzas'])
def upload():
    message = None
    error = None
    if request.method == 'POST':
        if not has_role('admin', 'uploader', 'finanzas'):
            error = 'No tiene permiso para subir archivos.'
        else:
            statements = request.files.getlist('statement')
            if not statements or all(not statement or statement.filename == '' for statement in statements):
                error = 'Debe seleccionar al menos un archivo.'
            else:
                saved_files = []
                for statement in statements:
                    if not statement or statement.filename == '':
                        continue
                    filename = Path(statement.filename).name
                    target = UPLOAD_DIR / filename
                    statement.save(target)
                    saved_files.append(filename)
                if saved_files:
                    message = f'Archivos subidos: {", ".join(saved_files)}'
                    db.log_action(session.get('username'), 'upload', detail={'archivos': saved_files}, db_path=DB_PATH)
                else:
                    error = 'No se pudo guardar ningún archivo.'
    return render_template('upload.html', role=session.get('role'), display_name=get_display_name(), message=message, error=error)


def get_missing_company_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    empresa = df.get('Empresa', pd.Series([''] * len(df))).astype(str).fillna('')
    cuit = df.get('CUIT', pd.Series([''] * len(df))).astype(str).fillna('')
    missing = df[(empresa.str.strip() == '') & (cuit.str.strip() == '')].copy()
    return missing


def is_row_identified(row: dict) -> bool:
    # Una fila está identificada cuando ya sabemos de quién es y de qué banco.
    # 'Desconocido' es el valor que pone build_record cuando no pudo resolver
    # el banco, así que no cuenta como identificado.
    empresa = str(row.get('Empresa', '') or '').strip()
    cuit = str(row.get('CUIT', '') or '').strip()
    banco = str(row.get('Banco', '') or '').strip()
    return bool(empresa and cuit and banco and banco.lower() != 'desconocido')


def get_unregistered_account_rows(df: pd.DataFrame, bank_accounts: list[dict[str, str]]) -> pd.DataFrame:
    if df.empty or not bank_accounts:
        return pd.DataFrame()
    rows = []
    for _, row in df.iterrows():
        cuenta = str(row.get('Cuenta', '') or '').strip()
        if not cuenta:
            continue
        # El cruce por cuenta es el medio para averiguar Empresa/CUIT/Banco, no
        # un fin: si la fila ya está identificada (porque el extracto lo traía
        # o porque un usuario la asignó a mano en /unify), no hay nada que
        # resolver. Sin esta salida, una cuenta que no figura en DATOS
        # BANCARIOS no se puede unificar nunca: la pantalla de resolución no
        # cambia el número de cuenta, así que el re-chequeo volvía a marcarla
        # y devolvía al usuario a la misma pantalla indefinidamente.
        if is_row_identified(row):
            continue
        matched = False
        for entry in bank_accounts:
            if account_matches(cuenta, entry.get('account', '')):
                matched = True
                break
        if not matched:
            rows.append(row.to_dict())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def recompute_record_hashes(df: pd.DataFrame, record_hashes: list[str]) -> pd.DataFrame:
    # El RecordHash identifica al movimiento por su contenido, y ese contenido
    # incluye Banco y CUIT. Después de una asignación manual el hash viejo ya
    # no describe la fila: si no se recalcula, el día que la cuenta se dé de
    # alta en DATOS BANCARIOS y el banco pase a detectarse solo, el mismo
    # movimiento entraría de nuevo como si fuera nuevo.
    if df.empty or not record_hashes or 'RecordHash' not in df.columns:
        return df
    mask = df['RecordHash'].isin(set(record_hashes))
    if not mask.any():
        return df
    df.loc[mask, 'RecordHash'] = df.loc[mask].apply(lambda row: hash_record(row.to_dict()), axis=1)
    return df


def build_resolve_rows(missing_rows: pd.DataFrame, unregistered_rows: pd.DataFrame) -> list[dict]:
    # Una misma fila puede caer en los dos grupos a la vez (sin Empresa/CUIT y
    # además con una cuenta que no figura en DATOS BANCARIOS). Se muestra una
    # sola vez: si no, el usuario tiene que completar el mismo movimiento dos
    # veces en la grilla.
    combined = pd.concat([missing_rows, unregistered_rows])
    if 'RecordHash' in combined.columns:
        combined = combined.drop_duplicates(subset=['RecordHash'])
    return combined.to_dict(orient='records')


def get_user_options() -> list[str]:
    return USER_ROLES


PASSWORD_PATTERN = re.compile(r'^[A-Za-z0-9]+$')


def is_valid_password(password: str) -> bool:
    return len(password) >= 8 and bool(PASSWORD_PATTERN.match(password))


def build_company_options(company_map: dict) -> list[dict]:
    return [
        {'cuit': cuit, 'empresa': empresa}
        for cuit, empresa in sorted(company_map.items(), key=lambda item: item[1])
        if cuit and empresa
    ]


def merge_company_lookup(company_map: dict, bank_company_options: list[dict[str, str]]) -> dict[str, str]:
    lookup = {normalize_cuit(cuit): empresa for cuit, empresa in company_map.items() if cuit and empresa}
    for entry in bank_company_options:
        cuit = normalize_cuit(entry.get('cuit', ''))
        if cuit and entry.get('empresa'):
            lookup[cuit] = entry['empresa']
    return lookup


@app.route('/unify', methods=['POST'])
@login_required(role=['admin', 'uploader', 'finanzas'])
def unify():
    company_map, company_name_map = load_companies(EMPRESAS_PATH)
    if not company_map and EMPRESAS_PATH.exists():
        return render_template('result.html', error='No se pudo abrir Empresas.xlsx. Cerrá el archivo si está abierto y volvé a intentarlo.', role=session.get('role'))
    bank_accounts, bank_company_options, bank_options = load_bank_accounts(BASE_DIR / BANK_ACCOUNTS_FILE.name)
    statements = gather_statements(UPLOAD_DIR, company_map, company_name_map, bank_accounts)
    if request.form.get('resolve_missing') == '1':
        combined_company_map = merge_company_lookup(company_map, bank_company_options)
        assigned_hashes = []
        for index in range(int(request.form.get('missing_count', '0') or '0')):
            record_hash = request.form.get(f'record_hash_{index}', '')
            selected_cuit = request.form.get(f'mapping_{index}', '')
            selected_bank = request.form.get(f'bank_{index}', '')
            if record_hash:
                if selected_cuit:
                    statements.loc[statements['RecordHash'] == record_hash, 'CUIT'] = selected_cuit
                    selected_company = combined_company_map.get(normalize_cuit(selected_cuit), '')
                    if selected_company:
                        statements.loc[statements['RecordHash'] == record_hash, 'Empresa'] = selected_company
                if selected_bank:
                    statements.loc[statements['RecordHash'] == record_hash, 'Banco'] = selected_bank
                if selected_cuit or selected_bank:
                    assigned_hashes.append(record_hash)
        statements = recompute_record_hashes(statements, assigned_hashes)
        missing_rows = get_missing_company_rows(statements)
        unregistered_rows = get_unregistered_account_rows(statements, bank_accounts)
        if not missing_rows.empty or not unregistered_rows.empty:
            company_options = bank_company_options if bank_company_options else build_company_options(company_map)
            rows = build_resolve_rows(missing_rows, unregistered_rows)
            return render_template('resolve_missing.html', rows=rows, company_options=company_options, bank_options=bank_options, role=session.get('role'), error='Debe completar la empresa, CUIT y banco para todas las filas antes de continuar.')
    else:
        missing_rows = get_missing_company_rows(statements)
        unregistered_rows = get_unregistered_account_rows(statements, bank_accounts)
        if not missing_rows.empty or not unregistered_rows.empty:
            company_options = bank_company_options if bank_company_options else build_company_options(company_map)
            if not company_options:
                return render_template('result.html', error='No se encontró el archivo Empresas.xlsx o ningún dato válido en Datos Bancarios.', role=session.get('role'))
            rows = build_resolve_rows(missing_rows, unregistered_rows)
            return render_template('resolve_missing.html', rows=rows, company_options=company_options, bank_options=bank_options, role=session.get('role'))
    if statements.empty:
        return render_template('result.html', error='No se encontraron registros nuevos en los extractos subidos.', role=session.get('role'))
    file_counts = statements['SourceFile'].value_counts().to_dict()

    # Detalle por archivo: cuántas filas trajo cada uno y cuántas eran nuevas,
    # en orden de procesamiento (si dos archivos del mismo lote se solapan,
    # el primero se lleva las "nuevas" y el segundo las cuenta como repetidas).
    seen_hashes = db.get_existing_hashes(statements['RecordHash'].tolist(), db_path=DB_PATH)
    per_file_stats = []
    for source_file in statements['SourceFile'].drop_duplicates().tolist():
        file_hashes = statements.loc[statements['SourceFile'] == source_file, 'RecordHash'].tolist()
        total = len(file_hashes)
        new = sum(1 for h in file_hashes if h not in seen_hashes)
        per_file_stats.append((source_file, total, new, total - new))
        seen_hashes.update(file_hashes)

    total_processed, new_count = db.upsert_movements(statements, db_path=DB_PATH)
    existing_count = total_processed - new_count
    db.log_action(
        session.get('username'), 'unify',
        detail={'filas_procesadas': total_processed, 'nuevas': new_count, 'archivos': file_counts},
        db_path=DB_PATH,
    )
    # Los archivos ya unificados se sacan de /uploads: quedan guardados en la
    # base y, si se dejaran ahí, la próxima unificación los volvería a
    # procesar junto con los nuevos que se suban.
    for filename in file_counts:
        try:
            (UPLOAD_DIR / filename).unlink()
        except (FileNotFoundError, PermissionError):
            pass
    lines = ['Archivos procesados']
    for source_file, total, new, existing in per_file_stats:
        detail = f'{source_file}: {total} filas leídas, {new} nuevas'
        if existing:
            detail += f' ({existing} ya existían)'
        lines.append(detail + '.')
    lines.append(f'Total de filas leídas: {total_processed}.')
    nuevas_line = f'Nuevas agregadas: {new_count}.'
    if existing_count:
        nuevas_line = f'Nuevas agregadas: {new_count} ({existing_count} ya existían y no se duplicaron).'
    lines.append(nuevas_line)
    lines.append('Los archivos procesados se quitaron de la carpeta de subidas.')
    message = '\n'.join(lines)
    return render_template('result.html', message=message, role=session.get('role'))


def normalize_currency_value(value: object) -> str:
    if pd.isna(value) or str(value).strip() == '' or str(value).strip().lower() == 'nan':
        return '$'
    text = str(value).strip()
    normalized = text.lower()
    if 'usd' in normalized or 'dolar' in normalized or 'dólar' in normalized:
        return 'USD'
    return text


def format_amount(value: object) -> str:
    if pd.isna(value) or value == '':
        return ''
    try:
        number = float(value)
    except Exception:
        return str(value)
    formatted = f'{number:,.2f}'
    # Use dot as thousand separator and comma as decimal separator for Spanish locale
    formatted = formatted.replace(',', 'X').replace('.', ',').replace('X', '.')
    return formatted


def parse_date_filter(value: str):
    if not value:
        return None
    try:
        # Los <input type="date"> del HTML siempre mandan la fecha en formato
        # ISO (AAAA-MM-DD), nunca ambigua: "dayfirst" no aplica y de hecho
        # invierte mes y día cuando ambos son <=12 (ej. "2026-07-08" se leía
        # como 7 de agosto en vez de 8 de julio).
        parsed = pd.to_datetime(value, format='%Y-%m-%d', errors='coerce')
        return parsed
    except Exception:
        return None


def apply_general_search(df: pd.DataFrame, term: str) -> pd.DataFrame:
    term = (term or '').strip()
    if not term:
        return df
    # regex=False: el término se busca como texto literal, no como expresión
    # regular (una descripción con paréntesis u otros caracteres especiales
    # no debe romper la búsqueda ni dar resultados inesperados).
    description_mask = df['Descripcion'].astype(str).str.contains(term, case=False, regex=False, na=False)
    normalized = term.replace('.', '').replace(',', '.')
    try:
        float(normalized)
        is_amount = True
    except ValueError:
        is_amount = False
    if not is_amount:
        return df[description_mask]
    search_term = term.lstrip('-').strip()
    # Comparamos contra el monto sin separador de miles (solo coma decimal)
    # para que "1500,50" encuentre "1.500,50" sin que el punto de miles
    # rompa la búsqueda por substring.
    plain_abs = df['Monto_raw'].abs().apply(lambda v: '' if pd.isna(v) else f'{v:.2f}'.replace('.', ','))
    amount_mask = plain_abs.str.contains(search_term, regex=False, na=False)
    # Un término numérico puede ser tanto un monto como un código/referencia
    # dentro de la descripción (ej. "0718", "000105255"); se buscan ambos.
    return df[description_mask | amount_mask]


def apply_record_filters(df: pd.DataFrame, empresa: str, banco: str, fecha: str, fecha_desde: str, fecha_hasta: str,
                          monto_filter: str, ci_filter: str = 'all', search: str = '', moneda: str = '',
                          vencido: bool = False) -> pd.DataFrame:
    df = df.copy()
    # 'Fecha' siempre viene en formato ISO (AAAA-MM-DD) desde la base; ver
    # nota en parse_date_filter sobre por qué "dayfirst" no corresponde acá.
    df['Fecha_dt'] = pd.to_datetime(df['Fecha'], format='%Y-%m-%d', errors='coerce')
    if empresa:
        df = df[df['Empresa'].astype(str) == empresa]
    if banco:
        df = df[df['Banco'].astype(str) == banco]
    if moneda:
        df = df[df['Moneda'].astype(str) == moneda]
    exact_date = parse_date_filter(fecha)
    if exact_date is not None:
        df = df[df['Fecha_dt'] == exact_date]
    start_date = parse_date_filter(fecha_desde)
    if start_date is not None:
        df = df[df['Fecha_dt'] >= start_date]
    end_date = parse_date_filter(fecha_hasta)
    if end_date is not None:
        df = df[df['Fecha_dt'] <= end_date]
    if monto_filter == 'positivos':
        df = df[df['Monto_raw'] > 0]
    elif monto_filter == 'negativos':
        df = df[df['Monto_raw'] < 0]
    if ci_filter == 'con_ci':
        df = df[df['CI'].astype(str).str.strip() != '']
    elif ci_filter == 'vacios':
        df = df[df['CI'].astype(str).str.strip() == '']
    if vencido:
        df = df[df['ci_vencido']]
    df = apply_general_search(df, search)
    return df


CI_ALERT_DAYS = 10


def prepare_display_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if 'Moneda' not in df.columns:
        df['Moneda'] = ''
    df['Moneda'] = df['Moneda'].apply(normalize_currency_value)
    if 'CI' not in df.columns:
        df['CI'] = ''
    df['CI'] = df['CI'].astype(str)
    if 'CUIT' not in df.columns:
        df['CUIT'] = ''
    df['CUIT'] = df['CUIT'].astype(str).replace('nan', '')
    if 'Monto' not in df.columns:
        df['Monto'] = ''
    df['Monto_raw'] = pd.to_numeric(df['Monto'], errors='coerce')
    if 'Saldo' not in df.columns:
        df['Saldo'] = ''
    df['Saldo_raw'] = pd.to_numeric(df['Saldo'], errors='coerce')
    # Acreditación sin CI cargado hace más de CI_ALERT_DAYS días: se marca
    # para resaltar el recuadro de CI en rojo y para el resumen de pendientes.
    fecha_dt = pd.to_datetime(df['Fecha'], format='%Y-%m-%d', errors='coerce')
    dias_transcurridos = (pd.Timestamp.now().normalize() - fecha_dt).dt.days
    df['sin_ci_acreditacion'] = (df['Monto_raw'] > 0) & (df['CI'].str.strip() == '')
    df['ci_vencido'] = df['sin_ci_acreditacion'] & (dias_transcurridos > CI_ALERT_DAYS)
    df['Monto'] = df['Monto_raw'].apply(format_amount)
    df['Saldo'] = df['Saldo_raw'].apply(format_amount)
    # 'Fecha' se mantiene en formato ISO (lo que esperan los filtros y la
    # exportación); 'Fecha_display' es solo para mostrar dd/mm/aaaa en pantalla.
    df['Fecha_display'] = fecha_dt.dt.strftime('%d/%m/%Y')
    df['Fecha_display'] = df['Fecha_display'].where(fecha_dt.notna(), df['Fecha'])
    return df


def get_filter_options(df_full: pd.DataFrame, filters: dict) -> tuple[list[str], list[str]]:
    # Los desplegables son "en cascada": las opciones de Empresa reflejan el
    # resto de los filtros activos (banco, moneda, fechas, etc.) menos el
    # propio filtro de empresa, y viceversa para Banco. Así, si ya filtraste
    # por Moneda=USD, el desplegable de Empresa solo muestra las empresas que
    # realmente tienen movimientos en USD, no todas las que existen en la base.
    empresa_scope = apply_record_filters(
        df_full, '', filters.get('banco', ''), filters.get('fecha', ''), filters.get('fecha_desde', ''),
        filters.get('fecha_hasta', ''), filters.get('monto_filter', 'all'), filters.get('ci_filter', 'all'),
        filters.get('search', ''), filters.get('moneda', ''), filters.get('vencido') == '1',
    )
    banco_scope = apply_record_filters(
        df_full, filters.get('empresa', ''), '', filters.get('fecha', ''), filters.get('fecha_desde', ''),
        filters.get('fecha_hasta', ''), filters.get('monto_filter', 'all'), filters.get('ci_filter', 'all'),
        filters.get('search', ''), filters.get('moneda', ''), filters.get('vencido') == '1',
    )
    empresas = {value for value in empresa_scope['Empresa'].astype(str) if value.strip()}
    # Todas las empresas de Empresas.xlsx aparecen siempre, tengan o no
    # movimientos: una empresa recién dada de alta tiene que poder elegirse
    # para confirmar que todavía no cargó nada (la grilla queda vacía), en vez
    # de no figurar y dejar la duda de si está mal cargada.
    company_map, _ = load_companies(EMPRESAS_PATH)
    empresas.update(name for name in company_map.values() if str(name).strip())
    banco_options = sorted({value for value in banco_scope['Banco'].astype(str) if value.strip()})
    return sorted(empresas), banco_options


def build_ci_summary(df_full: pd.DataFrame, filters: dict) -> tuple[dict, list[dict]]:
    # Base: respeta los filtros que no son de empresa/CI/movimiento (banco,
    # fechas, moneda, búsqueda), para que el desglose por empresa siga
    # reaccionando a esos filtros sin que el propio filtro de empresa/CI
    # oculte los recuadros.
    base_df = apply_record_filters(
        df_full, '', filters.get('banco', ''), filters.get('fecha', ''), filters.get('fecha_desde', ''),
        filters.get('fecha_hasta', ''), 'all', 'all', filters.get('search', ''), filters.get('moneda', ''), False,
    )
    carry_params = {
        key: filters[key] for key in ('banco', 'fecha', 'fecha_desde', 'fecha_hasta', 'moneda', 'search')
        if filters.get(key)
    }

    def make_href(empresa_value: str, vencido: bool) -> str:
        params = dict(carry_params)
        params['ci_filter'] = 'vacios'
        params['monto_filter'] = 'positivos'
        if empresa_value:
            params['empresa'] = empresa_value
        if vencido:
            params['vencido'] = '1'
        return '/records?' + urlencode(params)

    def counts_for(sub_df: pd.DataFrame) -> tuple[int, int]:
        return int(sub_df['sin_ci_acreditacion'].sum()), int(sub_df['ci_vencido'].sum())

    total_sin_ci, total_vencido = counts_for(base_df)
    total_summary = {
        'name': 'TOTAL', 'sin_ci': total_sin_ci, 'sin_ci_href': make_href('', False),
        'vencido': total_vencido, 'vencido_href': make_href('', True),
    }

    # Los recuadros son fijos (todas las empresas de Empresas.xlsx), aunque
    # una empresa todavía no tenga movimientos o no tenga pendientes de CI.
    company_map, _ = load_companies(EMPRESAS_PATH)
    known_companies = {name for name in company_map.values() if name.strip()}
    if not known_companies:
        known_companies = {value for value in df_full['Empresa'].astype(str) if value.strip()}

    company_summaries = []
    for empresa in sorted(known_companies):
        sub = base_df[base_df['Empresa'].astype(str) == empresa]
        sin_ci, vencido = counts_for(sub)
        company_summaries.append({
            'name': empresa, 'sin_ci': sin_ci, 'sin_ci_href': make_href(empresa, False),
            'vencido': vencido, 'vencido_href': make_href(empresa, True),
        })
    return total_summary, company_summaries


def get_latest_unify_ts() -> str:
    entry = db.get_last_action_entry('unify', db_path=DB_PATH)
    return entry['ts'] if entry else ''


@app.route('/api/latest-unify')
@login_required(role=['admin', 'uploader', 'viewer', 'cajera', 'finanzas'])
def api_latest_unify():
    entry = db.get_last_action_entry('unify', db_path=DB_PATH)
    if not entry:
        return {'ts': None}
    return {'ts': entry['ts'], 'username': entry['username'], 'detail': entry.get('detail')}


@app.route('/records', methods=['GET', 'POST'])
@login_required(role=['admin', 'uploader', 'viewer', 'cajera', 'finanzas'])
def records():
    error = None
    message = None
    df_full = db.load_movements(db_path=DB_PATH)
    if df_full.empty:
        error = 'No existen movimientos unificados. Primero subí y unificá extractos.'
        return render_template(
            'records.html',
            error=error,
            message=message,
            rows=[],
            role=session.get('role'),
            display_name=get_display_name(),
            filters={},
            empresa_options=[],
            banco_options=[],
            latest_unify_ts=get_latest_unify_ts(),
            show_saldo=puede_ver_saldo(),
            show_ci=False,
            total_summary={'name': 'TOTAL', 'sin_ci': 0, 'sin_ci_href': '/records', 'vencido': 0, 'vencido_href': '/records'},
            company_summaries=[],
            ci_alert_days=CI_ALERT_DAYS,
            filters_query='',
        )
    df_full = prepare_display_dataframe(df_full)
    filters = {
        'empresa': request.args.get('empresa') or request.form.get('empresa', ''),
        'banco': request.args.get('banco') or request.form.get('banco', ''),
        'fecha': request.args.get('fecha') or request.form.get('fecha', ''),
        'fecha_desde': request.args.get('fecha_desde') or request.form.get('fecha_desde', ''),
        'fecha_hasta': request.args.get('fecha_hasta') or request.form.get('fecha_hasta', ''),
        'monto_filter': request.args.get('monto_filter') or request.form.get('monto_filter', 'all'),
        'ci_filter': request.args.get('ci_filter') or request.form.get('ci_filter', 'all'),
        'search': request.args.get('search') or request.form.get('search', ''),
        'moneda': request.args.get('moneda') or request.form.get('moneda', ''),
        'vencido': request.args.get('vencido') or request.form.get('vencido', ''),
    }
    empresa_options, banco_options = get_filter_options(df_full, filters)
    df_filtered = apply_record_filters(
        df_full,
        filters['empresa'],
        filters['banco'],
        filters['fecha'],
        filters['fecha_desde'],
        filters['fecha_hasta'],
        filters['monto_filter'],
        filters['ci_filter'],
        filters['search'],
        filters['moneda'],
        filters['vencido'] == '1',
    )

    if request.method == 'POST':
        if not has_role('admin', 'cajera'):
            error = 'No tiene permiso para modificar CI.'
        else:
            changes = []
            for record_hash in request.form.getlist('record_hash'):
                ci_value = request.form.get(f'ci_{record_hash}', '').strip()
                if ci_value:
                    changes.append((record_hash, ci_value))
            if changes:
                old_ci_map = df_full.set_index('RecordHash')['CI'].to_dict()
                empresa_por_hash = df_full.set_index('RecordHash')['Empresa'].to_dict()
                user = current_user()
                # Ya viene de la pantalla de confirmación: se guarda todo.
                if request.form.get('confirmar_ajenas') == '1':
                    propias, ajenas = changes, []
                else:
                    propias, ajenas = split_ci_changes(changes, empresa_por_hash, user)

                guardar_ci(propias, old_ci_map)

                if ajenas:
                    # Los de otras empresas no se escriben todavía: se listan
                    # para que la usuaria confirme o vuelva a corregirlos.
                    return render_template(
                        'confirm_ci.html',
                        role=session.get('role'),
                        display_name=get_display_name(),
                        guardados=len(propias),
                        pendientes=build_pending_ci_rows(ajenas, df_full),
                        empresas_primarias=empresas_primarias_de(user),
                        filters_query=urlencode({k: v for k, v in filters.items() if v}),
                    )
                message = 'CI actualizados correctamente.'
                df_full = prepare_display_dataframe(db.load_movements(db_path=DB_PATH))
                empresa_options, banco_options = get_filter_options(df_full, filters)
                df_filtered = apply_record_filters(
                    df_full,
                    filters['empresa'],
                    filters['banco'],
                    filters['fecha'],
                    filters['fecha_desde'],
                    filters['fecha_hasta'],
                    filters['monto_filter'],
                    filters['ci_filter'],
                    filters['search'],
                    filters['moneda'],
                    filters['vencido'] == '1',
                )
            else:
                message = 'No se detectaron cambios en CI.'

    rows = df_filtered.to_dict(orient='records')
    total_summary, company_summaries = build_ci_summary(df_full, filters)
    return render_template(
        'records.html',
        error=error,
        message=message,
        rows=rows,
        role=session.get('role'),
        display_name=get_display_name(),
        filters=filters,
        empresa_options=empresa_options,
        banco_options=banco_options,
        latest_unify_ts=get_latest_unify_ts(),
        show_saldo=puede_ver_saldo(),
        show_ci=True,
        total_summary=total_summary,
        company_summaries=company_summaries,
        ci_alert_days=CI_ALERT_DAYS,
        filters_query=urlencode({k: v for k, v in filters.items() if v}),
    )


def get_filters_from_args() -> dict:
    return {
        'empresa': request.args.get('empresa', ''),
        'banco': request.args.get('banco', ''),
        'fecha': request.args.get('fecha', ''),
        'fecha_desde': request.args.get('fecha_desde', ''),
        'fecha_hasta': request.args.get('fecha_hasta', ''),
        'monto_filter': request.args.get('monto_filter', 'all'),
        'ci_filter': request.args.get('ci_filter', 'all'),
        'search': request.args.get('search', ''),
        'moneda': request.args.get('moneda', ''),
        'vencido': request.args.get('vencido', ''),
    }


def describe_filters(filters: dict) -> str:
    movimiento_labels = {'positivos': 'Acreditaciones', 'negativos': 'Débitos'}
    ci_labels = {'con_ci': 'Con CI', 'vacios': 'Vacíos'}
    plain_labels = {
        'empresa': 'Empresa', 'banco': 'Banco', 'fecha': 'Fecha exacta',
        'fecha_desde': 'Desde', 'fecha_hasta': 'Hasta', 'moneda': 'Moneda', 'search': 'Búsqueda',
    }
    parts = []
    for key, label in plain_labels.items():
        value = filters.get(key)
        if value:
            parts.append(f'{label}: {value}')
    monto_filter = filters.get('monto_filter')
    if monto_filter and monto_filter != 'all':
        parts.append(f'Movimiento: {movimiento_labels.get(monto_filter, monto_filter)}')
    ci_filter = filters.get('ci_filter')
    if ci_filter and ci_filter != 'all':
        parts.append(f'CI: {ci_labels.get(ci_filter, ci_filter)}')
    if filters.get('vencido') == '1':
        parts.append('Solo vencidas (más de 10 días sin CI)')
    if not parts:
        return 'Filtros aplicados: ninguno (todos los registros).'
    return 'Filtros aplicados — ' + ' | '.join(parts)


def get_export_columns(show_saldo: bool) -> list[str]:
    columns = ['Fecha', 'Empresa', 'CUIT', 'Cuenta', 'Moneda', 'Banco', 'Descripcion', 'Monto']
    if show_saldo:
        columns.append('Saldo')
    columns.append('CI')
    return columns


def load_filtered_export_dataframe(filters: dict) -> pd.DataFrame:
    df = db.load_movements(db_path=DB_PATH)
    if df.empty:
        return df
    df = prepare_display_dataframe(df)
    return apply_record_filters(
        df, filters['empresa'], filters['banco'], filters['fecha'], filters['fecha_desde'],
        filters['fecha_hasta'], filters['monto_filter'], filters['ci_filter'], filters['search'],
        filters['moneda'], filters.get('vencido') == '1',
    )


PDF_COLUMN_LABELS = {
    'Fecha': 'Fecha', 'Empresa': 'Empresa', 'CUIT': 'CUIT', 'Cuenta': 'Cuenta',
    'Moneda': 'Moneda', 'Banco': 'Banco', 'Descripcion': 'Descripción',
    'Monto': 'Monto', 'Saldo': 'Saldo', 'CI': 'CI',
}
PDF_COLUMN_WEIGHTS = {
    'Fecha': 0.07, 'Empresa': 0.13, 'CUIT': 0.09, 'Cuenta': 0.10, 'Moneda': 0.05,
    'Banco': 0.09, 'Descripcion': 0.27, 'Monto': 0.09, 'Saldo': 0.08, 'CI': 0.08,
}


def build_pdf_export(rows: list[dict], columns: list[str], filter_summary: str) -> bytes:
    buffer = io.BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=page_size, topMargin=12 * mm, bottomMargin=12 * mm,
                             leftMargin=10 * mm, rightMargin=10 * mm)
    styles = getSampleStyleSheet()
    cell_style = styles['Normal'].clone('cell')
    cell_style.fontSize = 7
    cell_style.leading = 8

    story = []
    if LOGO_FILE.exists():
        story.append(RLImage(str(LOGO_FILE), width=45 * mm, height=45 * mm * 432 / 1550))
        story.append(Spacer(1, 6))
    story.append(Paragraph('Registros unificados', styles['Title']))
    story.append(Paragraph(filter_summary, styles['Normal']))
    story.append(Spacer(1, 10))

    header = [PDF_COLUMN_LABELS.get(col, col) for col in columns]
    table_data = [header]
    for row in rows:
        table_data.append([Paragraph(str(row.get(col, '') or ''), cell_style) for col in columns])

    usable_width = page_size[0] - 20 * mm
    total_weight = sum(PDF_COLUMN_WEIGHTS.get(col, 0.1) for col in columns)
    col_widths = [usable_width * PDF_COLUMN_WEIGHTS.get(col, 0.1) / total_weight for col in columns]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4338ca')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    doc.build(story)
    return buffer.getvalue()


@app.route('/download/excel')
@login_required(role=['admin', 'uploader', 'viewer', 'cajera', 'finanzas'])
def download_excel():
    filters = get_filters_from_args()
    df = load_filtered_export_dataframe(filters)
    if df.empty:
        return render_template('records.html', error='No hay movimientos para descargar con los filtros aplicados.', rows=[], role=session.get('role'), display_name=get_display_name(), filters={}, empresa_options=[], banco_options=[], latest_unify_ts=get_latest_unify_ts(), show_saldo=puede_ver_saldo(), show_ci=False, total_summary={'name': 'TOTAL', 'sin_ci': 0, 'sin_ci_href': '/records', 'vencido': 0, 'vencido_href': '/records'}, company_summaries=[], ci_alert_days=CI_ALERT_DAYS, filters_query='')
    show_saldo = puede_ver_saldo()
    export_df = df.copy()
    export_df['Monto'] = export_df['Monto_raw']
    export_df['Saldo'] = export_df['Saldo_raw']
    excel_bytes = build_master_bytes(
        export_df, fields=get_export_columns(show_saldo), header_note=describe_filters(filters),
    )
    return send_file(
        io.BytesIO(excel_bytes), as_attachment=True, download_name=MASTER_FILE.name,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/download/pdf')
@login_required(role=['admin', 'uploader', 'viewer', 'cajera', 'finanzas'])
def download_pdf():
    filters = get_filters_from_args()
    df = load_filtered_export_dataframe(filters)
    if df.empty:
        error = 'No hay movimientos para generar el PDF con los filtros aplicados.'
        return render_template('records.html', error=error, rows=[], role=session.get('role'), display_name=get_display_name(), filters={}, empresa_options=[], banco_options=[], latest_unify_ts=get_latest_unify_ts(), show_saldo=puede_ver_saldo(), show_ci=False, total_summary={'name': 'TOTAL', 'sin_ci': 0, 'sin_ci_href': '/records', 'vencido': 0, 'vencido_href': '/records'}, company_summaries=[], ci_alert_days=CI_ALERT_DAYS, filters_query='')
    show_saldo = puede_ver_saldo()
    columns = get_export_columns(show_saldo)
    rows = df.to_dict(orient='records')
    pdf_bytes = build_pdf_export(rows, columns, describe_filters(filters))
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True, download_name='extractos_unificados.pdf', mimetype='application/pdf')


@app.route('/admin', methods=['GET', 'POST'])
@login_required(role=['admin'])
def admin():
    global users_by_name
    users = load_users()
    message = None
    error = None
    if request.method == 'POST':
        action = request.form.get('action', '')
        username = request.form.get('username', '').strip()
        # 'roles' (multi-selección) es la forma actual; 'role' se sigue
        # aceptando para no romper formularios o scripts viejos.
        roles = [r.strip().lower() for r in request.form.getlist('roles') if r.strip()]
        if not roles and request.form.get('role', '').strip():
            roles = [request.form['role'].strip().lower()]
        role = roles[0] if roles else ''
        password = request.form.get('password', '').strip()
        nombre = request.form.get('nombre', '').strip()
        if action == 'create':
            if not username or not password or not role:
                error = 'Debes completar usuario, contraseña y rol para crear un usuario.'
            elif any(u['username'] == username for u in users):
                error = 'El usuario ya existe.'
            elif any(r not in get_user_options() for r in roles):
                error = 'Rol inválido.'
            elif not is_valid_password(password):
                error = 'La contraseña debe ser alfanumérica y tener al menos 8 caracteres.'
            else:
                users.append({'username': username, 'password': generate_password_hash(password),
                              'roles': roles, 'nombre': nombre,
                              'empresas_primarias': [], 'empresas_secundarias': []})
                save_users(users)
                db.log_action(session.get('username'), 'user_create', detail={'usuario': username, 'roles': roles, 'nombre': nombre}, db_path=DB_PATH)
                message = 'Usuario creado correctamente.'
        elif action == 'update':
            if not username or not role:
                error = 'Debes completar usuario y rol para actualizar.'
            elif password and not is_valid_password(password):
                error = 'La contraseña debe ser alfanumérica y tener al menos 8 caracteres.'
            else:
                updated = False
                for user in users:
                    if user['username'] == username:
                        user['roles'] = roles
                        user.pop('role', None)
                        if password:
                            user['password'] = generate_password_hash(password)
                        if nombre:
                            user['nombre'] = nombre
                        updated = True
                        break
                if not updated:
                    error = 'Usuario no encontrado.'
                else:
                    save_users(users)
                    db.log_action(session.get('username'), 'user_update', detail={'usuario': username, 'roles': roles, 'nombre': nombre}, db_path=DB_PATH)
                    message = 'Usuario actualizado correctamente.'
        elif action == 'delete':
            if username:
                users = [u for u in users if u['username'] != username]
                save_users(users)
                db.log_action(session.get('username'), 'user_delete', detail={'usuario': username}, db_path=DB_PATH)
                message = 'Usuario eliminado correctamente.'
            else:
                error = 'Usuario inválido para eliminar.'
        else:
            error = 'Acción desconocida.'
        users = load_users()
        users_by_name = {user['username']: user for user in users}
    return render_template('admin.html', role=session.get('role'), users=users, user_options=get_user_options(), message=message, error=error)


@app.route('/admin/clear', methods=['POST'])
@login_required(role=['admin'])
def admin_clear():
    message = None
    error = None
    try:
        db.log_action(session.get('username'), 'clear_test_data', db_path=DB_PATH)
        deleted = db.clear_movements(db_path=DB_PATH)
        message = f'Se borraron {deleted} movimientos unificados (uso de prueba). Usuarios y archivos subidos no se modificaron.'
    except Exception as exc:
        error = f'Error al borrar movimientos: {exc}'
    users = load_users()
    return render_template('admin.html', role=session.get('role'), users=users, user_options=get_user_options(), message=message, error=error)


@app.route('/admin/audit')
@login_required(role=['admin'])
def admin_audit():
    entries = db.load_audit_log(limit=200, db_path=DB_PATH)
    return render_template('audit.html', role=session.get('role'), entries=entries)


if __name__ == '__main__':
    UPLOAD_DIR.mkdir(exist_ok=True)
    db.migrate_excel_if_needed(BASE_DIR / MASTER_FILE.name, db_path=DB_PATH)
    # Este entrypoint es solo desarrollo local por HTTP: con la cookie marcada
    # Secure el navegador no la mandaría y no se podría ni iniciar sesión.
    # El deploy real entra por wsgi.py, que no toca esta configuración.
    app.config['SESSION_COOKIE_SECURE'] = False
    app.run(host='127.0.0.1', port=5000, debug=True)
