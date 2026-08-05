"""Panel de posibles duplicados en Admin.

Sirve para detectar CON DATOS si un banco necesita reconciliación, en vez de
activarla a ciegas: hay bancos (Macro, Banco Municipal) donde el comprobante
es de la operación entera y usarlo como identificador borraría movimientos
buenos.

La huella que busca es: mismo banco, cuenta, fecha, CUIT e importe, pero
distinto Saldo o distinta Descripción.
"""
import json

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


def _mov(db_path, record_hash, saldo=1000.0, monto=3000000.0, descripcion='CRED VS | 636478',
          banco='Santa Fe', cuenta='000005252000', fecha='05/08/2026', ci='', archivo='a.xls'):
    db.upsert_movements(pd.DataFrame([{
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': 'ALCO ROSARIO SA',
        'CUIT': '30612502354', 'Cuenta': cuenta, 'Moneda': '$', 'Banco': banco,
        'Descripcion': descripcion, 'Debito': None, 'Credito': monto, 'Monto': monto,
        'Saldo': saldo, 'CI': ci, 'SourceFile': archivo, 'SourceRow': 1,
    }]), db_path=db_path)


def _login(client, username='admin'):
    client.post('/login', data={'username': username, 'password': 'clave12345'})


# ------------------------------ la detección --------------------------------

def test_detecta_el_mismo_movimiento_con_distinto_saldo(app_env):
    _mov(app_env['db_path'], 'h1', saldo=36867133.87)
    _mov(app_env['db_path'], 'h2', saldo=55867133.87)

    grupos = db.posibles_duplicados(db_path=app_env['db_path'])

    assert len(grupos) == 1
    assert len(grupos[0]['movimientos']) == 2
    assert grupos[0]['tiene_repetidos'] is True
    assert all(m['descripcion_repetida'] for m in grupos[0]['movimientos'])
    # Ordenados por saldo: primero el de la descarga vieja, que es el que sobra.
    assert [m['Saldo'] for m in grupos[0]['movimientos']] == [36867133.87, 55867133.87]


def test_detecta_la_misma_fecha_e_importe_con_descripcion_distinta(app_env):
    _mov(app_env['db_path'], 'h1', descripcion='Transferencia pendiente')
    _mov(app_env['db_path'], 'h2', descripcion='Transferencia confirmada')

    grupos = db.posibles_duplicados(db_path=app_env['db_path'])

    assert len(grupos) == 1
    # Se marca distinto porque puede ser una reclasificación o dos movimientos
    # distintos: hay que mirarlo, no borrarlo a ciegas.
    assert grupos[0]['tiene_repetidos'] is False


def test_no_marca_movimientos_que_no_se_parecen(app_env):
    _mov(app_env['db_path'], 'h1', monto=100.0)
    _mov(app_env['db_path'], 'h2', monto=200.0)
    _mov(app_env['db_path'], 'h3', fecha='04/08/2026')
    _mov(app_env['db_path'], 'h4', cuenta='000051173507')
    _mov(app_env['db_path'], 'h5', banco='Macro')

    assert db.posibles_duplicados(db_path=app_env['db_path']) == []


def test_una_base_vacia_no_rompe(app_env):
    assert db.posibles_duplicados(db_path=app_env['db_path']) == []


def test_agrupa_de_a_tres_o_mas(app_env):
    for i, saldo in enumerate([100.0, 200.0, 300.0]):
        _mov(app_env['db_path'], f'h{i}', saldo=saldo)

    grupos = db.posibles_duplicados(db_path=app_env['db_path'])

    assert len(grupos) == 1
    assert len(grupos[0]['movimientos']) == 3


def test_respeta_el_limite(app_env):
    for i in range(6):
        _mov(app_env['db_path'], f'a{i}', fecha=f'0{i + 1}/08/2026', saldo=1.0)
        _mov(app_env['db_path'], f'b{i}', fecha=f'0{i + 1}/08/2026', saldo=2.0)

    assert len(db.posibles_duplicados(db_path=app_env['db_path'], limite=2)) == 2


# ------------------------------- la pantalla --------------------------------

def test_el_panel_muestra_el_grupo(client, app_env):
    _mov(app_env['db_path'], 'h1', saldo=36867133.87, archivo='descarga_1237.xls')
    _mov(app_env['db_path'], 'h2', saldo=55867133.87, archivo='descarga_1421.xls')
    _login(client)

    body = client.get('/admin').get_data(as_text=True)

    assert 'Posibles duplicados' in body
    assert '1 grupo(s) para revisar' in body
    assert 'descarga_1237.xls' in body
    assert 'descarga_1421.xls' in body
    assert 'hay descripción repetida' in body


def test_sin_duplicados_lo_dice(client, app_env):
    _mov(app_env['db_path'], 'h1')
    _login(client)

    assert 'No se detectaron movimientos repetidos' in client.get('/admin').get_data(as_text=True)


def test_una_cajera_no_ve_el_panel(client, app_env):
    _mov(app_env['db_path'], 'h1', saldo=1.0)
    _mov(app_env['db_path'], 'h2', saldo=2.0)
    _login(client, 'cajera')

    assert client.get('/admin').status_code == 403


def test_se_puede_borrar_desde_el_panel_y_vuelve_a_admin(client, app_env):
    _mov(app_env['db_path'], 'h1', saldo=36867133.87)
    _mov(app_env['db_path'], 'h2', saldo=55867133.87)
    _login(client)

    respuesta = client.post('/records/borrar', data={'record_hash': 'h1', 'volver': '/admin'})

    assert respuesta.headers['Location'].endswith('/admin')
    assert sorted(db.load_movements(db_path=app_env['db_path'])['RecordHash']) == ['h2']


def test_el_aviso_de_lo_borrado_se_ve_al_volver_a_admin(client, app_env):
    _mov(app_env['db_path'], 'h1', saldo=1.0)
    _mov(app_env['db_path'], 'h2', saldo=2.0)
    _login(client)

    respuesta = client.post('/records/borrar', data={'record_hash': 'h1', 'volver': '/admin'},
                            follow_redirects=True)

    assert 'Se borró el movimiento' in respuesta.get_data(as_text=True)


def test_el_panel_se_vacia_cuando_se_resuelve(client, app_env):
    _mov(app_env['db_path'], 'h1', saldo=1.0)
    _mov(app_env['db_path'], 'h2', saldo=2.0)
    _login(client)

    client.post('/records/borrar', data={'record_hash': 'h1', 'volver': '/admin'})

    assert 'No se detectaron movimientos repetidos' in client.get('/admin').get_data(as_text=True)


def test_marca_solo_el_repetido_y_no_al_que_coincide_de_casualidad(app_env):
    # Caso real de Santa Fe: el "CRED VS" duplicado convivia con un "DEP EFEC"
    # de 3.000.000 que era un deposito distinto. Ese no hay que borrarlo.
    _mov(app_env['db_path'], 'h1', saldo=36867133.87, descripcion='CRED VS | 636478')
    _mov(app_env['db_path'], 'h2', saldo=55867133.87, descripcion='CRED VS | 636478')
    _mov(app_env['db_path'], 'h3', saldo=36867133.87, descripcion='DEP EFEC | 0000023955')

    grupo = db.posibles_duplicados(db_path=app_env['db_path'])[0]

    assert len(grupo['movimientos']) == 3
    assert grupo['tiene_repetidos'] is True
    marcados = {m['Descripcion'] for m in grupo['movimientos'] if m['descripcion_repetida']}
    assert marcados == {'CRED VS | 636478'}
