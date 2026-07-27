import io
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook
from pypdf import PdfReader
from werkzeug.security import generate_password_hash

import db
import web_app


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / 'test.db'
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir()
    users_file = tmp_path / 'users.json'
    users = [
        {'username': 'admin', 'password': generate_password_hash('adminpass'), 'role': 'admin'},
        {'username': 'cajera', 'password': generate_password_hash('cajerapass'), 'role': 'cajera'},
    ]
    users_file.write_text(json.dumps({'users': users}), encoding='utf-8')

    # Por defecto no existe Empresas.xlsx en el entorno de test: el resumen
    # de CI cae al listado de empresas derivado de los datos cargados (mismo
    # comportamiento que antes de tener un listado fijo).
    empresas_path = tmp_path / 'Empresas.xlsx'

    monkeypatch.setattr(web_app, 'DB_PATH', db_path)
    monkeypatch.setattr(web_app, 'UPLOAD_DIR', upload_dir)
    monkeypatch.setattr(web_app, 'USERS_FILE', users_file)
    monkeypatch.setattr(web_app, 'EMPRESAS_PATH', empresas_path)
    monkeypatch.setattr(web_app, 'users_by_name', {u['username']: u for u in users})

    web_app.app.config.update(TESTING=True)
    return {'db_path': db_path, 'upload_dir': upload_dir, 'users_file': users_file, 'empresas_path': empresas_path}


@pytest.fixture
def client(app_env):
    with web_app.app.test_client() as test_client:
        yield test_client


def _seed_movement(db_path, record_hash='hash1', ci=''):
    record = {
        'RecordHash': record_hash, 'Fecha': '15/07/2026', 'Empresa': 'Empresa Test',
        'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
        'Descripcion': 'Movimiento', 'Debito': None, 'Credito': 100.0, 'Monto': 100.0,
        'Saldo': 1000.0, 'CI': ci, 'SourceFile': 'archivo1.xlsx', 'SourceRow': 1,
    }
    db.upsert_movements(pd.DataFrame([record]), db_path=db_path)


def login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password}, follow_redirects=True)


def test_records_requires_login(client):
    response = client.get('/records')
    assert response.status_code == 302
    assert response.headers['Location'].rstrip('/') in ('', 'http://localhost')


def test_invalid_login_logs_failure(client, app_env):
    response = client.post('/login', data={'username': 'admin', 'password': 'wrong'})
    assert 'incorrectos'.encode('utf-8') in response.data
    entries = db.load_audit_log(db_path=app_env['db_path'])
    assert entries[0]['action'] == 'login_failed'


def test_valid_login_redirects_and_logs_success(client, app_env):
    response = login(client, 'admin', 'adminpass')
    assert response.status_code == 200
    entries = db.load_audit_log(db_path=app_env['db_path'])
    assert any(e['action'] == 'login_success' for e in entries)


def test_role_without_permission_shows_no_access_without_logging_out(client, app_env):
    login(client, 'cajera', 'cajerapass')

    response = client.get('/upload')

    assert response.status_code == 403
    assert 'No tenés permiso' in response.get_data(as_text=True)
    # La sesión sigue activa: una ruta permitida para su rol debe seguir andando.
    still_logged_in = client.get('/records')
    assert still_logged_in.status_code == 200


def test_records_hides_carga_link_for_roles_without_upload_access(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'cajera', 'cajerapass')

    body = client.get('/records').get_data(as_text=True)

    assert 'href="/upload"' not in body


def test_records_shows_carga_link_for_roles_with_upload_access(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    assert 'href="/upload"' in body


def test_ci_update_preserves_and_audits_change(client, app_env):
    _seed_movement(app_env['db_path'], record_hash='hash1', ci='')
    login(client, 'cajera', 'cajerapass')

    response = client.post('/records', data={'record_hash': 'hash1', 'ci_hash1': 'CI-500'})

    assert 'CI-500' in response.get_data(as_text=True)
    loaded = db.load_movements(db_path=app_env['db_path'])
    assert loaded.iloc[0]['CI'] == 'CI-500'
    entries = db.load_audit_log(db_path=app_env['db_path'])
    assert entries[0]['action'] == 'update_ci'
    assert entries[0]['detail']['despues'] == 'CI-500'


def _seed_movements(db_path, records):
    rows = []
    for i, overrides in enumerate(records, start=1):
        record = {
            'RecordHash': f'hash{i}', 'Fecha': '15/07/2026', 'Empresa': 'Empresa Test',
            'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
            'Descripcion': 'Movimiento', 'Debito': None, 'Credito': 100.0, 'Monto': 100.0,
            'Saldo': 1000.0, 'CI': '', 'SourceFile': 'archivo1.xlsx', 'SourceRow': i,
        }
        record.update(overrides)
        rows.append(record)
    db.upsert_movements(pd.DataFrame(rows), db_path=db_path)


def test_records_filter_dropdowns_list_known_companies_and_banks(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Hikari SA', 'Banco': 'Galicia'},
        {'Empresa': 'Alco Rosario SA', 'Banco': 'Santander'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    assert '<option value="Hikari SA"' in body
    assert '<option value="Alco Rosario SA"' in body
    assert '<option value="Galicia"' in body
    assert '<option value="Santander"' in body


def test_records_empresa_and_banco_dropdowns_cascade_with_other_filters(client, app_env):
    # Bug real reportado: al filtrar por Moneda=USD, los desplegables de
    # Empresa y Banco seguían mostrando TODAS las empresas/bancos de la base,
    # no solo los que realmente tienen movimientos en esa moneda.
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Hikari SA', 'Banco': 'Santa Fe', 'Moneda': 'USD'},
        {'Empresa': 'Alco Rosario SA', 'Banco': 'Santander', 'Moneda': 'ARS'},
        {'Empresa': 'Daseos SA', 'Banco': 'Macro', 'Moneda': 'ARS'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?moneda=USD').get_data(as_text=True)

    assert '<option value="Hikari SA"' in body
    assert '<option value="Alco Rosario SA"' not in body
    assert '<option value="Daseos SA"' not in body
    assert '<option value="Santa Fe"' in body
    assert '<option value="Santander"' not in body
    assert '<option value="Macro"' not in body


def test_records_empresa_dropdown_excludes_only_its_own_filter(client, app_env):
    # El desplegable de Empresa debe reflejar el filtro de Banco activo, pero
    # no auto-restringirse por el propio filtro de Empresa ya elegido (si no,
    # nunca se podría cambiar de una empresa a otra dentro del mismo banco).
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Hikari SA', 'Banco': 'Santa Fe'},
        {'Empresa': 'Alco Rosario SA', 'Banco': 'Santa Fe'},
        {'Empresa': 'Daseos SA', 'Banco': 'Macro'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?banco=Santa+Fe&empresa=Hikari+SA').get_data(as_text=True)

    assert '<option value="Hikari SA"' in body
    assert '<option value="Alco Rosario SA"' in body
    assert '<option value="Daseos SA"' not in body


def test_records_ci_filter_shows_only_empty_or_only_filled(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'CI': 'CI-1'},
        {'CI': ''},
    ])
    login(client, 'admin', 'adminpass')

    only_empty = client.get('/records?ci_filter=vacios').get_data(as_text=True)
    only_filled = client.get('/records?ci_filter=con_ci').get_data(as_text=True)

    assert 'CI-1' not in only_empty
    assert 'CI-1' in only_filled


def test_records_general_search_matches_amount_regardless_of_sign(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Descripcion': 'Pago proveedor', 'Monto': -1500.50},
        {'Descripcion': 'Cobro cliente', 'Monto': 999.00},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?search=1500,50').get_data(as_text=True)

    assert 'Pago proveedor' in body
    assert 'Cobro cliente' not in body


def test_records_general_search_matches_description_text(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Descripcion': 'Transferencia recibida de Juan'},
        {'Descripcion': 'Comision bancaria'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?search=transferencia').get_data(as_text=True)

    assert 'Transferencia recibida de Juan' in body
    assert 'Comision bancaria' not in body


def test_records_general_search_handles_special_characters_literally(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Descripcion': 'Pago a proveed acred santander rio - &&(0001933)'},
        {'Descripcion': 'Cobro cliente'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?search=%26%26(0001933)').get_data(as_text=True)

    assert 'santander rio' in body
    assert 'Cobro cliente' not in body


def test_records_general_search_numeric_term_also_matches_description_code(client, app_env):
    # Con los nuevos formatos de descripción (Macro, Santander, etc.) las
    # descripciones incluyen códigos numéricos (sucursal, referencia). Un
    # término numérico debe poder encontrar esos códigos, no solo montos.
    _seed_movements(app_env['db_path'], [
        {'Descripcion': '0718 | Rosario Centro | 4633 | 000105255 | Impuesto ley', 'Monto': -508594.75},
        {'Descripcion': 'Comision bancaria', 'Monto': 12.00},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?search=000105255').get_data(as_text=True)

    assert 'Rosario Centro' in body
    assert 'Comision bancaria' not in body


def test_records_moneda_filter_shows_only_selected_currency(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Descripcion': 'Movimiento en pesos', 'Moneda': ''},
        {'Descripcion': 'Movimiento en dolares', 'Moneda': 'USD'},
    ])
    login(client, 'admin', 'adminpass')

    only_usd = client.get('/records?moneda=USD').get_data(as_text=True)
    only_pesos = client.get('/records?moneda=%24').get_data(as_text=True)

    assert 'Movimiento en dolares' in only_usd
    assert 'Movimiento en pesos' not in only_usd
    assert 'Movimiento en pesos' in only_pesos
    assert 'Movimiento en dolares' not in only_pesos


def test_records_date_range_filter_correct_for_ambiguous_day_and_month(client, app_env):
    # "2026-07-08" (día 8, mes 7) es el caso exacto que dayfirst=True
    # interpretaba mal como 7 de agosto. Filtrando desde/hasta esa misma
    # fecha, el movimiento tiene que aparecer.
    _seed_movements(app_env['db_path'], [
        {'Fecha': '08/07/2026', 'Descripcion': 'Movimiento del 8 de julio'},
        {'Fecha': '20/08/2026', 'Descripcion': 'Movimiento de agosto'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records?fecha_desde=2026-07-08&fecha_hasta=2026-07-08').get_data(as_text=True)

    assert 'Movimiento del 8 de julio' in body
    assert 'Movimiento de agosto' not in body


def test_api_latest_unify_reflects_last_unify_action(client, app_env):
    login(client, 'admin', 'adminpass')
    db.log_action('admin', 'unify', detail={'filas_procesadas': 5, 'nuevas': 3}, db_path=app_env['db_path'])

    response = client.get('/api/latest-unify')
    data = response.get_json()

    assert data['username'] == 'admin'
    assert data['detail']['nuevas'] == 3


def test_unify_removes_processed_files_from_uploads(client, app_env):
    # Si el archivo ya unificado se queda en /uploads, la próxima
    # unificación lo vuelve a procesar junto con lo nuevo que se suba.
    wb = Workbook()
    ws = wb.active
    ws.append(['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Empresa', 'CUIT'])
    ws.append(['15/07/2026', 'Movimiento de prueba', '100,00', '900,00', 'Empresa Test', '20111111116'])
    target = app_env['upload_dir'] / 'archivo_test_unify.xlsx'
    wb.save(target)

    login(client, 'admin', 'adminpass')
    response = client.post('/unify', data={}, follow_redirects=True)

    assert response.status_code == 200
    assert not target.exists()
    body = response.get_data(as_text=True)
    assert 'se quitaron de la carpeta de subidas' in body
    assert 'Archivos procesados' in body
    assert 'archivo_test_unify.xlsx: 1 filas leídas, 1 nuevas.' in body
    assert 'Total de filas leídas: 1.' in body
    assert 'Nuevas agregadas: 1.' in body
    assert len(db.load_movements(db_path=app_env['db_path'])) == 1


def test_unify_reports_per_file_new_vs_repeated_rows(client, app_env):
    # Dos archivos con fechas superpuestas (como dos exports de ICBC en
    # distintos momentos): el segundo debe mostrar cuántas de sus filas ya
    # habían entrado con el primero.
    wb1 = Workbook()
    ws1 = wb1.active
    ws1.append(['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Empresa', 'CUIT'])
    ws1.append(['01/07/2026', 'Movimiento A', '100,00', '900,00', 'Empresa Test', '20111111116'])
    ws1.append(['02/07/2026', 'Movimiento B', '200,00', '700,00', 'Empresa Test', '20111111116'])
    wb1.save(app_env['upload_dir'] / 'archivo_1_primero.xlsx')

    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Empresa', 'CUIT'])
    ws2.append(['02/07/2026', 'Movimiento B', '200,00', '700,00', 'Empresa Test', '20111111116'])
    ws2.append(['03/07/2026', 'Movimiento C', '300,00', '400,00', 'Empresa Test', '20111111116'])
    wb2.save(app_env['upload_dir'] / 'archivo_2_segundo.xlsx')

    login(client, 'admin', 'adminpass')
    response = client.post('/unify', data={}, follow_redirects=True)

    body = response.get_data(as_text=True)
    assert 'archivo_1_primero.xlsx: 2 filas leídas, 2 nuevas.' in body
    assert 'archivo_2_segundo.xlsx: 2 filas leídas, 1 nuevas (1 ya existían).' in body
    assert 'Total de filas leídas: 4.' in body
    assert 'Nuevas agregadas: 3 (1 ya existían y no se duplicaron).' in body
    assert len(db.load_movements(db_path=app_env['db_path'])) == 3


def test_admin_create_user_stores_nombre(client, app_env):
    login(client, 'admin', 'adminpass')

    response = client.post('/admin', data={
        'action': 'create', 'username': 'nueva', 'password': 'pass12345',
        'role': 'viewer', 'nombre': 'Juana Perez',
    })

    assert response.status_code == 200
    users_after = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    nueva = next(u for u in users_after['users'] if u['username'] == 'nueva')
    assert nueva['nombre'] == 'Juana Perez'


def test_admin_update_user_preserves_nombre_when_left_blank(client, app_env):
    login(client, 'admin', 'adminpass')
    client.post('/admin', data={
        'action': 'create', 'username': 'nueva', 'password': 'pass12345',
        'role': 'viewer', 'nombre': 'Juana Perez',
    })

    client.post('/admin', data={'action': 'update', 'username': 'nueva', 'role': 'cajera'})

    users_after = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    nueva = next(u for u in users_after['users'] if u['username'] == 'nueva')
    assert nueva['nombre'] == 'Juana Perez'
    assert nueva['role'] == 'cajera'


def test_is_valid_password_requires_length_and_alphanumeric():
    assert web_app.is_valid_password('abcd1234') is True
    assert web_app.is_valid_password('short1') is False
    assert web_app.is_valid_password('abcdefgh') is True
    assert web_app.is_valid_password('abcd123!') is False
    assert web_app.is_valid_password('abcd 1234') is False


def test_admin_create_user_rejects_weak_password(client, app_env):
    login(client, 'admin', 'adminpass')

    response = client.post('/admin', data={
        'action': 'create', 'username': 'nueva', 'password': 'short1',
        'role': 'viewer', 'nombre': 'Juana Perez',
    })

    assert 'alfanumérica' in response.get_data(as_text=True)
    users_after = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    assert all(u['username'] != 'nueva' for u in users_after['users'])


def test_admin_create_user_rejects_non_alphanumeric_password(client, app_env):
    login(client, 'admin', 'adminpass')

    response = client.post('/admin', data={
        'action': 'create', 'username': 'nueva', 'password': 'pass1234!',
        'role': 'viewer',
    })

    assert 'alfanumérica' in response.get_data(as_text=True)
    users_after = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    assert all(u['username'] != 'nueva' for u in users_after['users'])


def test_admin_update_user_rejects_weak_password(client, app_env):
    login(client, 'admin', 'adminpass')
    client.post('/admin', data={
        'action': 'create', 'username': 'nueva', 'password': 'pass12345', 'role': 'viewer',
    })

    response = client.post('/admin', data={'action': 'update', 'username': 'nueva', 'role': 'viewer', 'password': 'short1'})

    assert 'alfanumérica' in response.get_data(as_text=True)


def test_admin_page_shows_form_before_users_table(client, app_env):
    login(client, 'admin', 'adminpass')

    body = client.get('/admin').get_data(as_text=True)

    assert body.index('id="userForm"') < body.index('id="userTable"')


def test_admin_page_has_search_input_and_edit_buttons(client, app_env):
    login(client, 'admin', 'adminpass')

    body = client.get('/admin').get_data(as_text=True)

    assert 'id="userSearch"' in body
    assert 'edit-user-button' in body
    assert 'data-username="admin"' in body


def test_change_password_requires_login(client):
    response = client.get('/change-password')
    assert response.status_code == 302


def test_change_password_success(client, app_env):
    login(client, 'cajera', 'cajerapass')

    response = client.post('/change-password', data={
        'current_password': 'cajerapass', 'new_password': 'nuevapass123', 'confirm_password': 'nuevapass123',
    })

    assert 'actualizada correctamente' in response.get_data(as_text=True)
    # La nueva contraseña ya sirve para loguearse.
    client.get('/logout')
    login_response = login(client, 'cajera', 'nuevapass123')
    assert login_response.status_code == 200


def test_change_password_rejects_wrong_current_password(client, app_env):
    login(client, 'cajera', 'cajerapass')

    response = client.post('/change-password', data={
        'current_password': 'incorrecta', 'new_password': 'nuevapass123', 'confirm_password': 'nuevapass123',
    })

    assert 'no es correcta' in response.get_data(as_text=True)


def test_change_password_rejects_mismatched_confirmation(client, app_env):
    login(client, 'cajera', 'cajerapass')

    response = client.post('/change-password', data={
        'current_password': 'cajerapass', 'new_password': 'nuevapass123', 'confirm_password': 'otradistinta',
    })

    assert 'no coinciden' in response.get_data(as_text=True)


def test_change_password_rejects_weak_new_password(client, app_env):
    login(client, 'cajera', 'cajerapass')

    response = client.post('/change-password', data={
        'current_password': 'cajerapass', 'new_password': 'short1', 'confirm_password': 'short1',
    })

    assert 'alfanumérica' in response.get_data(as_text=True)


def test_records_shows_date_as_dd_mm_yyyy(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    assert '15/07/2026' in body
    assert '2026-07-15' not in body


def test_records_save_ci_button_is_floating_for_cajera(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'cajera', 'cajerapass')

    body = client.get('/records').get_data(as_text=True)

    assert 'save-ci-bar' in body
    assert 'has-save-ci-bar' in body


def test_login_shows_display_name_instead_of_just_role(client, app_env):
    users_file_content = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    users_file_content['users'][0]['nombre'] = 'Ana Admin'
    app_env['users_file'].write_text(json.dumps(users_file_content), encoding='utf-8')
    import web_app
    web_app.users_by_name['admin']['nombre'] = 'Ana Admin'

    login(client, 'admin', 'adminpass')
    body = client.get('/records').get_data(as_text=True)

    assert 'Ana Admin' in body


def test_admin_clear_wipes_only_movements(client, app_env):
    _seed_movement(app_env['db_path'], record_hash='hash1')
    dummy_upload = app_env['upload_dir'] / 'dummy.xlsx'
    dummy_upload.write_bytes(b'not a real xlsx, just checking it survives')
    login(client, 'admin', 'adminpass')

    response = client.post('/admin/clear', follow_redirects=True)

    assert response.status_code == 200
    assert db.load_movements(db_path=app_env['db_path']).empty
    assert dummy_upload.exists()
    users_after = json.loads(app_env['users_file'].read_text(encoding='utf-8'))
    assert len(users_after['users']) == 2


def test_records_uses_movimiento_label_and_has_no_apply_button(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    assert 'name="monto_filter"' in body
    assert 'Tipo de monto' not in body
    assert 'Aplicar filtros' not in body
    assert 'last30Button' in body
    assert "filtersForm.addEventListener('change'" in body


def test_ci_alert_flag_only_for_overdue_credit_without_ci(client, app_env):
    overdue_date = (datetime.now() - timedelta(days=15)).strftime('%d/%m/%Y')
    recent_date = (datetime.now() - timedelta(days=2)).strftime('%d/%m/%Y')
    _seed_movements(app_env['db_path'], [
        {'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': '', 'Descripcion': 'Acreditacion vieja sin CI'},
        {'Fecha': recent_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': '', 'Descripcion': 'Acreditacion reciente sin CI'},
        {'Fecha': overdue_date, 'Monto': -500.0, 'Debito': 500.0, 'CI': '', 'Descripcion': 'Debito viejo sin CI'},
        {'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': 'CI-1', 'Descripcion': 'Acreditacion vieja con CI'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    # "ci-alert" también aparece una vez en el <style> (la regla CSS); lo que
    # importa es que solo una fila tenga la clase realmente aplicada al input.
    assert body.count('ci-cell-field ci-alert') == 1


def test_records_ci_summary_shows_total_and_per_company_boxes(client, app_env):
    overdue_date = (datetime.now() - timedelta(days=15)).strftime('%d/%m/%Y')
    recent_date = (datetime.now() - timedelta(days=2)).strftime('%d/%m/%Y')
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Empresa A', 'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': ''},
        {'Empresa': 'Empresa B', 'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': ''},
        {'Empresa': 'Empresa B', 'Fecha': recent_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': ''},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    # Recuadro TOTAL: 3 acreditaciones sin CI, 2 con más de 10 días.
    assert 'ci-summary-total' in body
    assert '>TOTAL<' in body
    assert '<strong>3</strong> acreditaciones sin CI' in body
    assert '<strong>2</strong> de esas con más de 10 días sin CI' in body

    # Recuadro Empresa A: 1 sin CI, 1 vencida.
    assert '>Empresa A<' in body
    assert '<strong>1</strong> acreditaciones sin CI' in body

    # Recuadro Empresa B: 2 sin CI, 1 vencida.
    assert '>Empresa B<' in body
    assert '<strong>2</strong> acreditaciones sin CI' in body
    assert '<strong>1</strong> de esas con más de 10 días sin CI' in body

    # Los links de cada recuadro llevan los filtros correctos para esa empresa.
    assert 'empresa=Empresa+A' in body
    assert 'ci_filter=vacios' in body
    assert 'monto_filter=positivos' in body
    assert 'vencido=1' in body


def test_records_ci_summary_box_click_filters_to_that_company(client, app_env):
    overdue_date = (datetime.now() - timedelta(days=15)).strftime('%d/%m/%Y')
    recent_date = (datetime.now() - timedelta(days=2)).strftime('%d/%m/%Y')
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Empresa A', 'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': '', 'Descripcion': 'Mov A vencido'},
        {'Empresa': 'Empresa B', 'Fecha': overdue_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': '', 'Descripcion': 'Mov B vencido'},
        {'Empresa': 'Empresa B', 'Fecha': recent_date, 'Monto': 500.0, 'Credito': 500.0, 'CI': '', 'Descripcion': 'Mov B reciente'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get(
        '/records?empresa=Empresa+B&ci_filter=vacios&monto_filter=positivos&vencido=1'
    ).get_data(as_text=True)

    assert 'Mov B vencido' in body
    assert 'Mov A vencido' not in body
    assert 'Mov B reciente' not in body


def test_records_table_rows_have_zebra_striping(client, app_env):
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    assert 'nth-child(even)' in body


def _write_empresas_file(path, companies):
    wb = Workbook()
    ws = wb.active
    ws.append(['CUIT', 'Empresa'])
    for cuit, empresa in companies:
        ws.append([cuit, empresa])
    wb.save(path)


def test_records_ci_summary_boxes_are_fixed_even_without_pending_ci(client, app_env):
    # El listado de empresas es el fijo de Empresas.xlsx: debe aparecer un
    # recuadro por cada una aunque no tenga movimientos ni pendientes de CI.
    _write_empresas_file(app_env['empresas_path'], [
        ('20111111116', 'Empresa A'),
        ('20222222223', 'Empresa B'),
        ('20333333330', 'Empresa Sin Movimientos'),
    ])
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Empresa A', 'CI': 'CI-1'},
    ])
    login(client, 'admin', 'adminpass')

    body = client.get('/records').get_data(as_text=True)

    names = set()
    for line in body.splitlines():
        if 'ci-summary-name' in line:
            names.add(line.strip())
    assert any('Empresa A' in n for n in names)
    assert any('Empresa B' in n for n in names)
    assert any('Empresa Sin Movimientos' in n for n in names)
    # Empresa A no tiene pendientes de CI (el único movimiento ya tiene CI
    # cargado): el recuadro debe seguir mostrándose, en 0.
    assert '>Empresa A<' in body


def test_download_excel_hides_saldo_for_cajera_and_applies_filters(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Empresa A', 'Monto': 100.0, 'Credito': 100.0, 'Saldo': 900.0},
        {'Empresa': 'Empresa B', 'Monto': 200.0, 'Credito': 200.0, 'Saldo': 800.0},
    ])
    login(client, 'cajera', 'cajerapass')

    resp = client.get('/download/excel?empresa=Empresa+A')

    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    all_values = [cell for row in ws.iter_rows(values_only=True) for cell in row if cell is not None]
    assert 'Saldo' not in all_values
    assert any('Empresa A' == v for v in all_values)
    assert not any('Empresa B' == v for v in all_values)
    assert any(isinstance(v, str) and 'Filtros aplicados' in v for v in all_values)


def test_download_excel_shows_saldo_for_admin(client, app_env):
    _seed_movement(app_env['db_path'])
    login(client, 'admin', 'adminpass')

    resp = client.get('/download/excel')

    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    all_values = [cell for row in ws.iter_rows(values_only=True) for cell in row if cell is not None]
    assert 'Saldo' in all_values


def test_download_pdf_is_real_pdf_and_respects_role_and_filters(client, app_env):
    _seed_movements(app_env['db_path'], [
        {'Empresa': 'Empresa A', 'Descripcion': 'Movimiento de Empresa A', 'Monto': 100.0, 'Credito': 100.0},
        {'Empresa': 'Empresa B', 'Descripcion': 'Movimiento de Empresa B', 'Monto': 200.0, 'Credito': 200.0},
    ])
    login(client, 'cajera', 'cajerapass')

    resp = client.get('/download/pdf?empresa=Empresa+A')

    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data[:4] == b'%PDF'
    reader = PdfReader(io.BytesIO(resp.data))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    assert 'Filtros aplicados' in text
    assert 'Empresa A' in text or 'Movimiento de Empresa A' in text
    assert 'Empresa B' not in text
    assert 'Saldo' not in text

