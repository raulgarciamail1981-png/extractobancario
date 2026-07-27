import io
import json
import os
import re
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
from flask import Flask, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from werkzeug.security import check_password_hash, generate_password_hash

import db
from conciliador import (BANK_ACCOUNTS_FILE, EMPRESAS_FILE, LOGO_FILE, MASTER_FILE, create_master_file,
                          gather_statements, load_bank_accounts, load_companies, account_matches,
                          normalize_cuit)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / 'uploads'
USERS_FILE = BASE_DIR / 'users.json'
# DATABASE_URL (ej. postgresql+psycopg2://user:pass@host:5432/db) tiene
# prioridad para producción; sin ella, cae al archivo SQLite de siempre
# (dev/tests, cero configuración).
DB_PATH = os.environ.get('DATABASE_URL') or os.environ.get('CONCILIADOR_DB_PATH', str(BASE_DIR / 'conciliador.db'))
EMPRESAS_PATH = BASE_DIR / EMPRESAS_FILE.name
USER_ROLES = ['admin', 'uploader', 'viewer', 'cajera', 'finanzas']

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

with USERS_FILE.open('r', encoding='utf-8') as f:
    users_data = json.load(f)['users']

users_by_name = {user['username']: user for user in users_data}


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'username' not in session:
                return redirect(url_for('login'))
            if role and session.get('role') not in role:
                return render_template('no_access.html', role=session.get('role')), 403
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
    user = users_by_name.get(username)
    if not user or not check_password_hash(user['password'], password):
        db.log_action(username or '(desconocido)', 'login_failed', db_path=DB_PATH)
        return render_template('login.html', error='Usuario o contraseña incorrectos')
    session['username'] = username
    session['role'] = user['role']
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
    global users_data, users_by_name
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
        if session.get('role') not in ['admin', 'uploader', 'finanzas']:
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


def get_unregistered_account_rows(df: pd.DataFrame, bank_accounts: list[dict[str, str]]) -> pd.DataFrame:
    if df.empty or not bank_accounts:
        return pd.DataFrame()
    rows = []
    for _, row in df.iterrows():
        cuenta = str(row.get('Cuenta', '') or '').strip()
        if not cuenta:
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


def load_users() -> list[dict[str, object]]:
    try:
        with USERS_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('users', [])
    except Exception:
        return []


def save_users(users: list[dict[str, object]]) -> None:
    with USERS_FILE.open('w', encoding='utf-8') as f:
        json.dump({'users': users}, f, indent=2, ensure_ascii=False)


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
        missing_rows = get_missing_company_rows(statements)
        unregistered_rows = get_unregistered_account_rows(statements, bank_accounts)
        if not missing_rows.empty or not unregistered_rows.empty:
            company_options = bank_company_options if bank_company_options else build_company_options(company_map)
            rows = pd.concat([missing_rows, unregistered_rows]).to_dict(orient='records')
            return render_template('resolve_missing.html', rows=rows, company_options=company_options, bank_options=bank_options, role=session.get('role'), error='Debe completar la empresa, CUIT y banco para todas las filas antes de continuar.')
    else:
        missing_rows = get_missing_company_rows(statements)
        unregistered_rows = get_unregistered_account_rows(statements, bank_accounts)
        if not missing_rows.empty or not unregistered_rows.empty:
            company_options = bank_company_options if bank_company_options else build_company_options(company_map)
            if not company_options:
                return render_template('result.html', error='No se encontró el archivo Empresas.xlsx o ningún dato válido en Datos Bancarios.', role=session.get('role'))
            rows = pd.concat([missing_rows, unregistered_rows]).to_dict(orient='records')
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
    empresa_options = sorted({value for value in empresa_scope['Empresa'].astype(str) if value.strip()})
    banco_options = sorted({value for value in banco_scope['Banco'].astype(str) if value.strip()})
    return empresa_options, banco_options


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
            show_saldo=(session.get('role') != 'cajera'),
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
        if session.get('role') not in ['admin', 'cajera']:
            error = 'No tiene permiso para modificar CI.'
        else:
            changes = []
            for record_hash in request.form.getlist('record_hash'):
                ci_value = request.form.get(f'ci_{record_hash}', '').strip()
                if ci_value:
                    changes.append((record_hash, ci_value))
            if changes:
                old_ci_map = df_full.set_index('RecordHash')['CI'].to_dict()
                for record_hash, ci_value in changes:
                    db.update_ci(record_hash, ci_value, db_path=DB_PATH)
                    db.log_action(
                        session.get('username'), 'update_ci',
                        detail={'antes': old_ci_map.get(record_hash, ''), 'despues': ci_value},
                        record_hash=record_hash, db_path=DB_PATH,
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
        show_saldo=(session.get('role') != 'cajera'),
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
        return render_template('records.html', error='No hay movimientos para descargar con los filtros aplicados.', rows=[], role=session.get('role'), display_name=get_display_name(), filters={}, empresa_options=[], banco_options=[], latest_unify_ts=get_latest_unify_ts(), show_saldo=(session.get('role') != 'cajera'), show_ci=False, total_summary={'name': 'TOTAL', 'sin_ci': 0, 'sin_ci_href': '/records', 'vencido': 0, 'vencido_href': '/records'}, company_summaries=[], ci_alert_days=CI_ALERT_DAYS, filters_query='')
    show_saldo = session.get('role') != 'cajera'
    export_df = df.copy()
    export_df['Monto'] = export_df['Monto_raw']
    export_df['Saldo'] = export_df['Saldo_raw']
    export_path = create_master_file(
        export_df, UPLOAD_DIR / MASTER_FILE.name,
        fields=get_export_columns(show_saldo), header_note=describe_filters(filters),
    )
    return send_file(export_path, as_attachment=True, download_name=MASTER_FILE.name)


@app.route('/download/pdf')
@login_required(role=['admin', 'uploader', 'viewer', 'cajera', 'finanzas'])
def download_pdf():
    filters = get_filters_from_args()
    df = load_filtered_export_dataframe(filters)
    if df.empty:
        error = 'No hay movimientos para generar el PDF con los filtros aplicados.'
        return render_template('records.html', error=error, rows=[], role=session.get('role'), display_name=get_display_name(), filters={}, empresa_options=[], banco_options=[], latest_unify_ts=get_latest_unify_ts(), show_saldo=(session.get('role') != 'cajera'), show_ci=False, total_summary={'name': 'TOTAL', 'sin_ci': 0, 'sin_ci_href': '/records', 'vencido': 0, 'vencido_href': '/records'}, company_summaries=[], ci_alert_days=CI_ALERT_DAYS, filters_query='')
    show_saldo = session.get('role') != 'cajera'
    columns = get_export_columns(show_saldo)
    rows = df.to_dict(orient='records')
    pdf_bytes = build_pdf_export(rows, columns, describe_filters(filters))
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True, download_name='extractos_unificados.pdf', mimetype='application/pdf')


@app.route('/admin', methods=['GET', 'POST'])
@login_required(role=['admin'])
def admin():
    global users_data, users_by_name
    users = load_users()
    message = None
    error = None
    if request.method == 'POST':
        action = request.form.get('action', '')
        username = request.form.get('username', '').strip()
        role = request.form.get('role', '').strip()
        password = request.form.get('password', '').strip()
        nombre = request.form.get('nombre', '').strip()
        if action == 'create':
            if not username or not password or not role:
                error = 'Debes completar usuario, contraseña y rol para crear un usuario.'
            elif any(u['username'] == username for u in users):
                error = 'El usuario ya existe.'
            elif role not in get_user_options():
                error = 'Rol inválido.'
            elif not is_valid_password(password):
                error = 'La contraseña debe ser alfanumérica y tener al menos 8 caracteres.'
            else:
                users.append({'username': username, 'password': generate_password_hash(password), 'role': role, 'nombre': nombre})
                save_users(users)
                db.log_action(session.get('username'), 'user_create', detail={'usuario': username, 'rol': role, 'nombre': nombre}, db_path=DB_PATH)
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
                        user['role'] = role
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
                    db.log_action(session.get('username'), 'user_update', detail={'usuario': username, 'rol': role, 'nombre': nombre}, db_path=DB_PATH)
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
    app.run(host='127.0.0.1', port=5000, debug=True)
