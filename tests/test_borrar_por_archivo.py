"""Sacar un extracto mal subido sin vaciar toda la base.

Antes, lo único que había era "Borrar registros importados", que borra todo.
Cuando alguien sube el archivo equivocado no hace falta perder el resto: cada
movimiento sabe de qué archivo vino.
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
        {'username': 'finanzas', 'password': generate_password_hash('clave12345'), 'roles': ['finanzas']},
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


def _movimiento(db_path, record_hash, archivo, empresa='EMPRESA TEST SA', fecha='15/07/2026', ci=''):
    db.upsert_movements(pd.DataFrame([{
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': empresa,
        'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
        'Descripcion': 'Movimiento', 'Debito': None, 'Credito': 100.0, 'Monto': 100.0,
        'Saldo': 1000.0, 'CI': ci, 'SourceFile': archivo, 'SourceRow': 1,
    }]), db_path=db_path)


def _login(client, username):
    client.post('/login', data={'username': username, 'password': 'clave12345'})


def test_el_resumen_agrupa_por_archivo(app_env):
    _movimiento(app_env['db_path'], 'h1', 'bueno.xls', fecha='01/07/2026')
    _movimiento(app_env['db_path'], 'h2', 'bueno.xls', fecha='20/07/2026', ci='CI-1')
    _movimiento(app_env['db_path'], 'h3', 'malo.xls', empresa='OTRA SA')

    resumen = {item['archivo']: item for item in db.resumen_por_archivo(db_path=app_env['db_path'])}

    assert resumen['bueno.xls']['filas'] == 2
    assert resumen['bueno.xls']['con_ci'] == 1
    assert resumen['bueno.xls']['desde'] == '2026-07-01'
    assert resumen['bueno.xls']['hasta'] == '2026-07-20'
    assert resumen['malo.xls']['empresas'] == ['OTRA SA']


def test_borrar_un_archivo_deja_el_resto_intacto(client, app_env):
    _movimiento(app_env['db_path'], 'h1', 'bueno.xls')
    _movimiento(app_env['db_path'], 'h2', 'bueno.xls')
    _movimiento(app_env['db_path'], 'h3', 'malo.xls')
    _login(client, 'admin')

    respuesta = client.post('/admin/borrar-archivo', data={'archivo': 'malo.xls'})

    assert 'Se borraron 1 movimiento(s) de «malo.xls»' in respuesta.get_data(as_text=True)
    quedan = db.load_movements(db_path=app_env['db_path'])
    assert sorted(quedan['RecordHash']) == ['h1', 'h2']


def test_borrar_un_archivo_queda_registrado_en_la_auditoria(client, app_env):
    _movimiento(app_env['db_path'], 'h1', 'malo.xls')
    _login(client, 'admin')

    client.post('/admin/borrar-archivo', data={'archivo': 'malo.xls'})

    entrada = db.get_last_action_entry('delete_source_file', db_path=app_env['db_path'])
    assert entrada['username'] == 'admin'
    assert entrada['detail'] == {'archivo': 'malo.xls', 'movimientos': 1}


@pytest.mark.parametrize('rol', ['finanzas', 'cajera'])
def test_solo_admin_puede_borrar_un_archivo(client, app_env, rol):
    _movimiento(app_env['db_path'], 'h1', 'malo.xls')
    _login(client, rol)

    respuesta = client.post('/admin/borrar-archivo', data={'archivo': 'malo.xls'})

    assert respuesta.status_code == 403
    assert len(db.load_movements(db_path=app_env['db_path'])) == 1


@pytest.mark.parametrize('archivo', ['', 'no_existe.xls'])
def test_no_se_borra_nada_con_un_archivo_que_no_esta(client, app_env, archivo):
    _movimiento(app_env['db_path'], 'h1', 'bueno.xls')
    _login(client, 'admin')

    respuesta = client.post('/admin/borrar-archivo', data={'archivo': archivo})

    assert len(db.load_movements(db_path=app_env['db_path'])) == 1
    assert 'Se borraron' not in respuesta.get_data(as_text=True)


def test_la_pantalla_de_admin_lista_los_archivos_importados(client, app_env):
    _movimiento(app_env['db_path'], 'h1', 'extracto_julio.xls', fecha='15/07/2026')
    _login(client, 'admin')

    body = client.get('/admin').get_data(as_text=True)

    assert 'extracto_julio.xls' in body
    assert '15/07/2026' in body
