"""Borrar un movimiento suelto desde la grilla. Solo admin.

Es la operación más destructiva que tiene la app a mano de un clic, así que
lo que se cuida acá es: que nadie más que admin pueda hacerlo, que borre uno
y solo uno, que quede registrado con el detalle de lo que se borró, y —sobre
todo— que la grilla siga guardando CI como antes.
"""
import json
import re

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

import db
import web_app


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / 'test.db'
    users = [
        {'username': 'admin', 'password': generate_password_hash('clave12345'), 'roles': ['admin']},
        {'username': 'cajera', 'password': generate_password_hash('clave12345'), 'roles': ['cajera']},
        {'username': 'finanzas', 'password': generate_password_hash('clave12345'), 'roles': ['finanzas']},
        {'username': 'comercial', 'password': generate_password_hash('clave12345'), 'roles': ['comercial']},
    ]
    (tmp_path / 'users.json').write_text(json.dumps({'users': users}), encoding='utf-8')

    monkeypatch.setattr(web_app, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(web_app, 'DB_PATH', db_path)
    monkeypatch.setattr(web_app, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(web_app, 'USERS_FILE', tmp_path / 'users.json')
    monkeypatch.setattr(web_app, 'EMPRESAS_PATH', tmp_path / 'Empresas.xlsx')
    monkeypatch.setattr(web_app, 'users_by_name', {u['username']: u for u in users})
    monkeypatch.setattr(web_app, '_login_failures', {})
    web_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return {'db_path': db_path}


@pytest.fixture
def client(app_env):
    with web_app.app.test_client() as test_client:
        yield test_client


def _movimiento(db_path, record_hash, monto=100.0, ci='', empresa='EMPRESA TEST SA'):
    db.upsert_movements(pd.DataFrame([{
        'RecordHash': record_hash, 'Fecha': '15/07/2026', 'Empresa': empresa,
        'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
        'Descripcion': 'Movimiento', 'Debito': None, 'Credito': monto, 'Monto': monto,
        'Saldo': 1000.0, 'CI': ci, 'SourceFile': 'archivo1.xlsx', 'SourceRow': 1,
    }]), db_path=db_path)


def _login(client, username):
    client.post('/login', data={'username': username, 'password': 'clave12345'})


def _hashes(db_path):
    return sorted(db.load_movements(db_path=db_path)['RecordHash'])


# ------------------------------- permisos ----------------------------------

@pytest.mark.parametrize('rol', ['cajera', 'finanzas', 'comercial'])
def test_solo_admin_puede_borrar_un_movimiento(client, app_env, rol):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, rol)

    respuesta = client.post('/records/borrar', data={'record_hash': 'h1'})

    assert respuesta.status_code == 403
    assert _hashes(app_env['db_path']) == ['h1']


@pytest.mark.parametrize('rol', ['cajera', 'finanzas', 'comercial'])
def test_el_boton_no_aparece_para_los_demas_roles(client, app_env, rol):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, rol)

    body = client.get('/records').get_data(as_text=True)

    assert 'borrarMovimientoForm' not in body


def test_el_boton_aparece_para_admin(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    body = client.get('/records').get_data(as_text=True)

    assert 'borrarMovimientoForm' in body
    assert 'form="borrarMovimientoForm"' in body


def test_sin_sesion_no_se_puede_borrar(client, app_env):
    _movimiento(app_env['db_path'], 'h1')

    client.post('/records/borrar', data={'record_hash': 'h1'})

    assert _hashes(app_env['db_path']) == ['h1']


# -------------------------------- borrado -----------------------------------

def test_borra_uno_y_solo_uno(client, app_env):
    for h in ('h1', 'h2', 'h3'):
        _movimiento(app_env['db_path'], h, monto=float(len(h) + ord(h[-1])))
    _login(client, 'admin')

    client.post('/records/borrar', data={'record_hash': 'h2'})

    assert _hashes(app_env['db_path']) == ['h1', 'h3']


def test_borrar_deja_el_detalle_en_la_auditoria(client, app_env):
    _movimiento(app_env['db_path'], 'h1', monto=1500.5, ci='CI-9', empresa='ALCO ROSARIO SA')
    _login(client, 'admin')

    client.post('/records/borrar', data={'record_hash': 'h1'})

    entrada = db.get_last_action_entry('delete_movement', db_path=app_env['db_path'])
    assert entrada['username'] == 'admin'
    assert entrada['record_hash'] == 'h1'
    # El detalle tiene que alcanzar para saber qué se borró: no hay vuelta atrás.
    assert entrada['detail']['Empresa'] == 'ALCO ROSARIO SA'
    assert entrada['detail']['Monto'] == 1500.5
    assert entrada['detail']['CI'] == 'CI-9'


@pytest.mark.parametrize('record_hash', ['', 'no_existe'])
def test_un_hash_invalido_no_borra_nada(client, app_env, record_hash):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    client.post('/records/borrar', data={'record_hash': record_hash})

    assert _hashes(app_env['db_path']) == ['h1']
    assert db.get_last_action_entry('delete_movement', db_path=app_env['db_path']) is None


def test_avisa_en_pantalla_lo_que_borro(client, app_env):
    _movimiento(app_env['db_path'], 'h1', empresa='ALCO ROSARIO SA')
    _login(client, 'admin')

    respuesta = client.post('/records/borrar', data={'record_hash': 'h1'}, follow_redirects=True)

    assert 'Se borró el movimiento' in respuesta.get_data(as_text=True)


def test_no_redirige_fuera_del_sitio(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    respuesta = client.post('/records/borrar', data={
        'record_hash': 'h1', 'volver': 'https://otro-sitio.com',
    })

    assert respuesta.headers['Location'].endswith('/records')


def test_vuelve_al_resumen_con_los_filtros_puestos(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    respuesta = client.post('/records/borrar', data={
        'record_hash': 'h1', 'volver': '/records?empresa=ALCO&ci_filter=sin',
    })

    assert respuesta.headers['Location'].endswith('/records?empresa=ALCO&ci_filter=sin')


# ------------- que la reforma no rompa lo que ya funcionaba ----------------

def test_la_grilla_sigue_guardando_ci_para_admin(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    client.post('/records', data={'record_hash': 'h1', 'ci_h1': 'CI-123'})

    fila = db.load_movements(db_path=app_env['db_path']).iloc[0]
    assert fila['CI'] == 'CI-123'


def test_la_grilla_sigue_guardando_ci_para_la_cajera(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'cajera')

    client.post('/records', data={'record_hash': 'h1', 'ci_h1': 'CI-456'})

    fila = db.load_movements(db_path=app_env['db_path']).iloc[0]
    assert fila['CI'] == 'CI-456'


def test_el_formulario_de_borrar_no_queda_anidado_en_el_de_ci(client, app_env):
    # Un <form> dentro de otro es HTML invalido: el navegador descarta el de
    # adentro y se rompe el guardado de CI. El de borrar tiene que abrirse y
    # cerrarse ANTES de que empiece el de CI.
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    body = client.get('/records').get_data(as_text=True)

    cierre_borrar = body.index('</form>', body.index('id="borrarMovimientoForm"'))
    apertura_ci = body.index('<form method="post" action="/records">')
    assert cierre_borrar < apertura_ci


def test_la_cantidad_de_columnas_coincide_entre_encabezado_y_filas(client, app_env):
    _movimiento(app_env['db_path'], 'h1')
    _login(client, 'admin')

    body = client.get('/records').get_data(as_text=True)

    encabezado = body[body.index('<thead>'):body.index('</thead>')]
    primera_fila = body[body.index('<tbody>'):body.index('</tr>', body.index('<tbody>'))]
    # El patrón pide un espacio o el cierre después del nombre, si no "<thead>"
    # se cuenta como una celda más.
    columnas_encabezado = len(re.findall(r'<th[\s>]', encabezado))
    columnas_fila = len(re.findall(r'<td[\s>]', primera_fila))
    assert columnas_encabezado == columnas_fila
    assert columnas_encabezado > 0
