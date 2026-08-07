"""Avisos persistentes al entrar a la app.

La idea es que sirvan de seguimiento entre sesiones: la cajera que estuvo toda
la mañana en otra cosa tiene que enterarse de que se cargaron extractos, y el
asistente comercial de que le asignaron CI en sus empresas. Por eso no
dependen de tener la pantalla abierta: se ven al volver y quedan hasta que la
persona los marca como vistos.
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
        {'username': 'cajera', 'password': generate_password_hash('clave12345'),
         'roles': ['cajera'], 'nombre': 'Cajera Test',
         'empresas_primarias': ['ALCO'], 'empresas_secundarias': []},
        {'username': 'comercial', 'password': generate_password_hash('clave12345'),
         'roles': ['comercial'], 'nombre': 'Comercial Test',
         'empresas_primarias': ['ALCO'], 'empresas_secundarias': ['HIKARI']},
        {'username': 'finanzas', 'password': generate_password_hash('clave12345'),
         'roles': ['finanzas'], 'nombre': 'Finanzas Test',
         'empresas_primarias': ['ALCO'], 'empresas_secundarias': []},
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


def _login(client, username):
    return client.post('/login', data={'username': username, 'password': 'clave12345'})


def _movimiento(db_path, record_hash, empresa, ci=''):
    db.upsert_movements(pd.DataFrame([{
        'RecordHash': record_hash, 'Fecha': '15/07/2026', 'Empresa': empresa,
        'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
        'Descripcion': 'Movimiento', 'Debito': None, 'Credito': 100.0, 'Monto': 100.0,
        'Saldo': 1000.0, 'CI': ci, 'SourceFile': 'archivo1.xlsx', 'SourceRow': 1,
    }]), db_path=db_path)


def _body(client, ruta='/records'):
    return client.get(ruta).get_data(as_text=True)


# Fecha vieja y fija para el "visto hasta acá". Dejar que la ponga el reloj
# hacía que el test fallara de a ratos: en la corrida completa, la primera
# visita y el CI que se carga después caen en el mismo milisegundo del reloj de
# Windows, y ahí "posterior a" es falso. Lo que se quiere probar es la lógica
# del aviso, no la resolución del reloj.
VISTO_VIEJO = '2026-01-01T00:00:00+00:00'


# --------------------------- extractos (cajeras) ---------------------------

def test_la_primera_visita_no_avisa_de_lo_que_ya_estaba(client, app_env):
    # Si no, la primera vez que entra le aparece un aviso por una carga vieja
    # que ya vio mil veces.
    db.log_action('finanzas', 'unify', detail={'nuevas': 5}, db_path=app_env['db_path'])
    _login(client, 'cajera')

    assert 'Hay nuevos extractos cargados' not in _body(client)


def test_la_cajera_ve_el_aviso_de_extractos_nuevos(client, app_env):
    db.log_action('finanzas', 'unify', detail={'nuevas': 5}, db_path=app_env['db_path'])
    _login(client, 'cajera')
    _body(client)  # deja la marca de "visto hasta acá"

    db.log_action('finanzas', 'unify', detail={'nuevas': 12}, db_path=app_env['db_path'])

    body = _body(client)
    assert 'Hay nuevos extractos cargados' in body
    assert '12 movimiento(s) nuevo(s)' in body


def test_el_aviso_de_extractos_sigue_hasta_que_lo_marca_visto(client, app_env):
    db.log_action('finanzas', 'unify', db_path=app_env['db_path'])
    _login(client, 'cajera')
    _body(client)
    db.log_action('finanzas', 'unify', detail={'nuevas': 3}, db_path=app_env['db_path'])

    # Entrar de nuevo no lo apaga: es un seguimiento, no un cartel de una vez.
    assert 'Hay nuevos extractos cargados' in _body(client)
    assert 'Hay nuevos extractos cargados' in _body(client)

    ts = db.get_last_action_entry('unify', db_path=app_env['db_path'])['ts']
    client.post('/avisos/visto', data={'tipo': 'extractos', 'ts': ts})

    assert 'Hay nuevos extractos cargados' not in _body(client)


def test_el_aviso_de_extractos_es_solo_para_cajeras(client, app_env):
    db.log_action('otra', 'unify', db_path=app_env['db_path'])
    _login(client, 'finanzas')
    _body(client)
    db.log_action('otra', 'unify', detail={'nuevas': 9}, db_path=app_env['db_path'])

    assert 'Hay nuevos extractos cargados' not in _body(client)


# ----------------------- CI asignado (asistente comercial) -----------------

def test_el_comercial_ve_los_ci_asignados_en_sus_empresas(client, app_env):
    _movimiento(app_env['db_path'], 'hash1', 'ALCO ROSARIO SA')
    _login(client, 'comercial')
    _body(client)
    db.marcar_notificacion_vista('comercial', 'ci', VISTO_VIEJO, db_path=app_env['db_path'])

    _movimiento(app_env['db_path'], 'hash1', 'ALCO ROSARIO SA', ci='CI-100')
    db.log_action('cajera', 'update_ci', detail={'antes': '', 'despues': 'CI-100'},
                  record_hash='hash1', db_path=app_env['db_path'])

    body = _body(client)
    assert '1 movimiento(s) con CI asignado' in body
    assert 'ALCO ROSARIO SA' in body


def test_el_comercial_no_ve_ci_de_empresas_que_no_son_suyas(client, app_env):
    _movimiento(app_env['db_path'], 'hash2', 'HIKARI SA')
    _login(client, 'comercial')
    _body(client)

    _movimiento(app_env['db_path'], 'hash2', 'HIKARI SA', ci='CI-200')
    db.log_action('cajera', 'update_ci', record_hash='hash2', db_path=app_env['db_path'])

    # HIKARI es secundaria para esta persona: no es su seguimiento.
    assert 'con CI asignado' not in _body(client)


def test_un_ci_que_se_borro_no_queda_avisando(client, app_env):
    _movimiento(app_env['db_path'], 'hash3', 'ALCO ROSARIO SA')
    _login(client, 'comercial')
    _body(client)

    db.log_action('cajera', 'update_ci', record_hash='hash3', db_path=app_env['db_path'])
    _movimiento(app_env['db_path'], 'hash3', 'ALCO ROSARIO SA', ci='')

    assert 'con CI asignado' not in _body(client)


def test_el_aviso_de_ci_se_apaga_al_marcarlo_visto(client, app_env):
    _movimiento(app_env['db_path'], 'hash4', 'ALCO ROSARIO SA')
    _login(client, 'comercial')
    _body(client)
    db.marcar_notificacion_vista('comercial', 'ci', VISTO_VIEJO, db_path=app_env['db_path'])

    _movimiento(app_env['db_path'], 'hash4', 'ALCO ROSARIO SA', ci='CI-300')
    db.log_action('cajera', 'update_ci', record_hash='hash4', db_path=app_env['db_path'])
    assert 'con CI asignado' in _body(client)

    ultimo = db.ci_asignados_desde('', db_path=app_env['db_path'])[0]['ultimo']
    client.post('/avisos/visto', data={'tipo': 'ci', 'ts': ultimo})

    assert 'con CI asignado' not in _body(client)


def test_marcar_visto_no_redirige_fuera_del_sitio(client, app_env):
    _login(client, 'cajera')

    respuesta = client.post('/avisos/visto', data={
        'tipo': 'extractos', 'ts': '2026-01-01T00:00:00+00:00', 'volver': 'https://otro-sitio.com',
    })

    assert respuesta.headers['Location'].endswith('/records')


def test_el_aviso_no_se_le_muestra_a_quien_no_inicio_sesion(client, app_env):
    db.log_action('finanzas', 'unify', db_path=app_env['db_path'])

    body = client.get('/login').get_data(as_text=True)

    assert 'Hay nuevos extractos cargados' not in body


def test_no_se_avisa_de_la_unificacion_que_hizo_uno_mismo(client, app_env):
    db.log_action('otra', 'unify', db_path=app_env['db_path'])
    _login(client, 'cajera')
    _body(client)

    db.log_action('cajera', 'unify', detail={'nuevas': 7}, db_path=app_env['db_path'])

    assert 'Hay nuevos extractos cargados' not in _body(client)
