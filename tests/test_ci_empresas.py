"""Aviso al cargar CI sobre empresas que la usuaria no tiene asignadas.

Cada cajera tiene empresas primarias (las suyas) y secundarias. El CI de las
primarias se guarda directo; el de las demás se le muestra para que confirme o
corrija antes de escribirlo.
"""
import json

import pandas as pd
import pytest
from werkzeug.security import generate_password_hash

import db
import web_app

MOVIMIENTOS = [
    ('h_alco', 'Alco Rosario SA'),
    ('h_daseos', 'Daseos SA'),
    ('h_hikari', 'Hikari SA'),
    ('h_xinoxia', 'Xinoxia SA'),
]

USUARIOS = [
    # Cajera con parte de las empresas: ALCO/DASEOS/NEOSTAR propias,
    # XINOXIA/HIKARI ajenas.
    {'username': 'cajera_parcial', 'nombre': 'Cajera Empresas Parciales', 'roles': ['cajera'],
     'empresas_primarias': ['ALCO', 'DASEOS', 'NEOSTAR'],
     'empresas_secundarias': ['XINOXIA', 'HIKARI']},
    # Finanzas Y cajera a la vez, con todas las empresas propias.
    {'username': 'finanzas_cajera', 'nombre': 'Finanzas Y Cajera', 'roles': ['finanzas', 'cajera'],
     'empresas_primarias': ['ALCO', 'DASEOS', 'NEOSTAR', 'XINOXIA', 'HIKARI'],
     'empresas_secundarias': []},
    # Cajera "pura" sin empresas asignadas.
    {'username': 'cajera', 'nombre': 'Cajera Sin Asignar', 'roles': ['cajera'],
     'empresas_primarias': [], 'empresas_secundarias': []},
    {'username': 'admin', 'nombre': 'Admin', 'roles': ['admin'],
     'empresas_primarias': [], 'empresas_secundarias': []},
]


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / 'test.db'
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir()
    users_file = tmp_path / 'users.json'
    users = [dict(u, password=generate_password_hash('clave12345')) for u in USUARIOS]
    users_file.write_text(json.dumps({'users': users}, ensure_ascii=False), encoding='utf-8')

    monkeypatch.setattr(web_app, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(web_app, 'DB_PATH', db_path)
    monkeypatch.setattr(web_app, 'UPLOAD_DIR', upload_dir)
    monkeypatch.setattr(web_app, 'USERS_FILE', users_file)
    monkeypatch.setattr(web_app, 'EMPRESAS_PATH', tmp_path / 'Empresas.xlsx')
    monkeypatch.setattr(web_app, 'users_by_name', {u['username']: u for u in users})
    monkeypatch.setattr(web_app, '_login_failures', {})
    web_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    filas = []
    for i, (record_hash, empresa) in enumerate(MOVIMIENTOS, start=1):
        filas.append({
            'RecordHash': record_hash, 'Fecha': '15/07/2026', 'Empresa': empresa,
            'CUIT': f'3061250235{i}', 'Cuenta': f'111222333{i}', 'Moneda': '$',
            'Banco': 'Santander RIO', 'Descripcion': f'Movimiento de {empresa}',
            'Debito': None, 'Credito': 100.0 * i, 'Monto': 100.0 * i, 'Saldo': 5000.0,
            'CI': '', 'SourceFile': 'extracto.xlsx', 'SourceRow': i,
        })
    db.upsert_movements(pd.DataFrame(filas), db_path=db_path)
    return {'db_path': db_path}


@pytest.fixture
def client(app_env):
    with web_app.app.test_client() as c:
        yield c


def login(client, username):
    return client.post('/login', data={'username': username, 'password': 'clave12345'},
                        follow_redirects=True)


def ci_guardados(db_path) -> dict:
    df = db.load_movements(db_path=db_path)
    return {r['RecordHash']: r['CI'] for _, r in df.iterrows()}


def test_ci_de_empresas_propias_se_guarda_sin_preguntar(client, app_env):
    login(client, 'cajera_parcial')

    resp = client.post('/records', data={
        'record_hash': ['h_alco', 'h_daseos'],
        'ci_h_alco': 'CI-100', 'ci_h_daseos': 'CI-200',
    })

    body = resp.get_data(as_text=True)
    assert 'registros de otras empresas' not in body
    assert 'CI actualizados correctamente' in body
    guardados = ci_guardados(app_env['db_path'])
    assert guardados['h_alco'] == 'CI-100'
    assert guardados['h_daseos'] == 'CI-200'


def test_ci_de_otra_empresa_pide_confirmacion_y_no_se_guarda_todavia(client, app_env):
    # El caso reportado: carga varios CI de sus empresas y uno de XINOXIA
    # y otro de HIKARI, que no le corresponden.
    login(client, 'cajera_parcial')

    resp = client.post('/records', data={
        'record_hash': ['h_alco', 'h_daseos', 'h_xinoxia', 'h_hikari'],
        'ci_h_alco': 'CI-100', 'ci_h_daseos': 'CI-200',
        'ci_h_xinoxia': 'CI-300', 'ci_h_hikari': 'CI-400',
    })

    body = resp.get_data(as_text=True)
    assert 'registros de otras empresas' in body
    # Se listan los dos movimientos ajenos, con su empresa y el CI a guardar.
    assert 'Xinoxia SA' in body and 'Hikari SA' in body
    assert 'CI-300' in body and 'CI-400' in body
    assert 'Confirmar guardado de CI' in body
    assert 'Corregir' in body

    guardados = ci_guardados(app_env['db_path'])
    # Las propias ya se guardaron; las ajenas todavía no.
    assert guardados['h_alco'] == 'CI-100'
    assert guardados['h_daseos'] == 'CI-200'
    assert guardados['h_xinoxia'] == ''
    assert guardados['h_hikari'] == ''


def test_confirmar_guarda_los_de_las_otras_empresas(client, app_env):
    login(client, 'cajera_parcial')
    client.post('/records', data={
        'record_hash': ['h_alco', 'h_xinoxia'],
        'ci_h_alco': 'CI-100', 'ci_h_xinoxia': 'CI-300',
    })

    resp = client.post('/records', data={
        'confirmar_ajenas': '1',
        'record_hash': ['h_xinoxia'], 'ci_h_xinoxia': 'CI-300',
    })

    assert 'CI actualizados correctamente' in resp.get_data(as_text=True)
    guardados = ci_guardados(app_env['db_path'])
    assert guardados['h_xinoxia'] == 'CI-300'
    assert guardados['h_alco'] == 'CI-100'


def test_corregir_deja_los_ajenos_sin_guardar(client, app_env):
    # "Corregir" es volver a la grilla: alcanza con no confirmar.
    login(client, 'cajera_parcial')
    client.post('/records', data={
        'record_hash': ['h_hikari'], 'ci_h_hikari': 'CI-400',
    })

    client.get('/records')

    assert ci_guardados(app_env['db_path'])['h_hikari'] == ''


def test_el_aviso_lista_solo_los_movimientos_ajenos(client, app_env):
    login(client, 'cajera_parcial')

    body = client.post('/records', data={
        'record_hash': ['h_alco', 'h_hikari'],
        'ci_h_alco': 'CI-100', 'ci_h_hikari': 'CI-400',
    }).get_data(as_text=True)

    assert body.count('name="record_hash"') == 1
    assert 'h_hikari' in body
    assert 'Movimiento de Alco Rosario SA' not in body


def test_usuaria_con_todas_las_empresas_propias_nunca_ve_el_aviso(client, app_env):
    login(client, 'finanzas_cajera')

    body = client.post('/records', data={
        'record_hash': ['h_alco', 'h_hikari', 'h_xinoxia'],
        'ci_h_alco': 'CI-1', 'ci_h_hikari': 'CI-2', 'ci_h_xinoxia': 'CI-3',
    }).get_data(as_text=True)

    assert 'registros de otras empresas' not in body
    guardados = ci_guardados(app_env['db_path'])
    assert guardados['h_hikari'] == 'CI-2'


def test_usuario_sin_empresas_asignadas_no_recibe_avisos(client, app_env):
    # Admin y usuarios viejos no tienen empresas: todo cuenta como propio.
    login(client, 'admin')

    body = client.post('/records', data={
        'record_hash': ['h_hikari'], 'ci_h_hikari': 'CI-999',
    }).get_data(as_text=True)

    assert 'registros de otras empresas' not in body
    assert ci_guardados(app_env['db_path'])['h_hikari'] == 'CI-999'


# --- roles múltiples -----------------------------------------------------

def test_finanzas_y_cajera_puede_subir_extractos_y_cargar_ci(client, app_env):
    login(client, 'finanzas_cajera')

    assert client.get('/upload').status_code == 200
    resp = client.post('/records', data={'record_hash': ['h_alco'], 'ci_h_alco': 'CI-77'})
    assert ci_guardados(app_env['db_path'])['h_alco'] == 'CI-77'
    assert resp.status_code == 200


def test_finanzas_y_cajera_ve_la_columna_saldo(client, app_env):
    # A la cajera "pura" se le oculta el Saldo; quien además es finanzas lo ve.
    login(client, 'finanzas_cajera')
    con_finanzas = client.get('/records').get_data(as_text=True)
    client.get('/logout')
    login(client, 'cajera')
    solo_cajera = client.get('/records').get_data(as_text=True)

    assert '<th>Saldo</th>' in con_finanzas
    assert '<th>Saldo</th>' not in solo_cajera


def test_cajera_pura_no_puede_subir_extractos(client):
    login(client, 'cajera')

    assert client.get('/upload').status_code == 403


def test_el_header_muestra_los_dos_roles(client, app_env):
    login(client, 'finanzas_cajera')

    body = client.get('/records').get_data(as_text=True)

    assert 'finanzas' in body and 'cajera' in body
