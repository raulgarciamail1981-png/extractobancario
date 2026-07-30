"""Los recuadros de CI tienen que cerrar contra el TOTAL.

Si el total dice 166 y los recuadros suman 124, la pantalla es imposible de
usar para controlar: no hay forma de saber dónde están los 42 que faltan.
"""
import json

import pandas as pd
import pytest
from openpyxl import Workbook
from werkzeug.security import generate_password_hash

import db
import web_app


def _empresas_xlsx(path, empresas):
    wb = Workbook()
    ws = wb.active
    ws.append(['CUIT', 'Empresa'])
    for cuit, nombre in empresas:
        ws.append([cuit, nombre])
    wb.save(path)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / 'test.db'
    (tmp_path / 'uploads').mkdir()
    users = [{'username': 'admin', 'password': generate_password_hash('clave12345'), 'roles': ['admin']}]
    (tmp_path / 'users.json').write_text(json.dumps({'users': users}), encoding='utf-8')

    monkeypatch.setattr(web_app, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(web_app, 'DB_PATH', db_path)
    monkeypatch.setattr(web_app, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(web_app, 'USERS_FILE', tmp_path / 'users.json')
    monkeypatch.setattr(web_app, 'EMPRESAS_PATH', tmp_path / 'Empresas.xlsx')
    monkeypatch.setattr(web_app, 'users_by_name', {u['username']: u for u in users})
    monkeypatch.setattr(web_app, '_login_failures', {})
    web_app.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return {'db_path': db_path, 'empresas_path': tmp_path / 'Empresas.xlsx'}


@pytest.fixture
def client(app_env):
    with web_app.app.test_client() as c:
        c.post('/login', data={'username': 'admin', 'password': 'clave12345'})
        yield c


def _acreditaciones_sin_ci(db_path, empresas):
    """Carga una acreditación sin CI por cada nombre de empresa recibido."""
    filas = []
    for i, empresa in enumerate(empresas, start=1):
        filas.append({
            'RecordHash': f'h{i}', 'Fecha': '15/07/2026', 'Empresa': empresa,
            'CUIT': '30612502354', 'Cuenta': '1', 'Moneda': '$', 'Banco': 'Santander RIO',
            'Descripcion': f'Acreditacion {i}', 'Debito': None, 'Credito': 100.0,
            'Monto': 100.0, 'Saldo': 1.0, 'CI': '', 'SourceFile': 'f', 'SourceRow': i,
        })
    db.upsert_movements(pd.DataFrame(filas), db_path=db_path)


def _resumen(client):
    """Devuelve (total, {empresa: acreditaciones sin CI}) leído de la pantalla."""
    import re
    body = client.get('/records').get_data(as_text=True)
    pares = re.findall(
        r'ci-summary-name">([^<]*)</div>\s*<a[^>]*>\s*<strong>(\d+)</strong>\s*acreditaciones',
        body,
    )
    assert pares, 'no se pudo leer ningún recuadro del resumen'
    total, empresas = None, {}
    for nombre, cantidad in pares:
        nombre = nombre.strip()
        if nombre == 'TOTAL':
            total = int(cantidad)
        else:
            empresas[nombre] = int(cantidad)
    return total, empresas


def test_el_total_cierra_con_la_suma_de_los_recuadros(client, app_env):
    # Caso real del servidor: el maestro escribe "ALCO ROSARIO SA" y los
    # movimientos llegan como "ALCO ROSARIO S.A." (con puntos), porque el
    # nombre vino del cruce por cuenta y no del maestro.
    _empresas_xlsx(app_env['empresas_path'], [
        ('30612502354', 'ALCO ROSARIO SA'),
        ('30708853557', 'XINOXIA SA'),
    ])
    _acreditaciones_sin_ci(app_env['db_path'], [
        'ALCO ROSARIO S.A.',   # misma empresa, otra escritura
        'ALCO ROSARIO SA',     # coincide exacto
        'XINOXIA SA',
    ])

    total, empresas = _resumen(client)

    assert total == 3
    assert sum(empresas.values()) == total, (
        f'el total ({total}) no cierra con la suma de los recuadros '
        f'({sum(empresas.values())}): {empresas}'
    )
    assert empresas['ALCO ROSARIO SA'] == 2


def test_los_movimientos_sin_empresa_no_desaparecen_del_resumen(client, app_env):
    # Una acreditación cuya empresa no se pudo resolver igual tiene que
    # contarse en algún recuadro, o el total deja de cerrar.
    _empresas_xlsx(app_env['empresas_path'], [('30612502354', 'ALCO ROSARIO SA')])
    _acreditaciones_sin_ci(app_env['db_path'], ['ALCO ROSARIO SA', '', 'EMPRESA DESCONOCIDA SRL'])

    total, empresas = _resumen(client)

    assert total == 3
    assert sum(empresas.values()) == total, (
        f'faltan {total - sum(empresas.values())} acreditaciones sin recuadro: {empresas}'
    )


def test_las_cinco_empresas_reales_agrupan_pese_a_la_diferencia_de_escritura(client, app_env):
    # Empresas.xlsx las escribe sin puntos ("ALCO ROSARIO SA") y los
    # movimientos que salen del cruce por cuenta las traen con puntos
    # ("ALCO ROSARIO S.A."). Las cinco tienen que caer en su recuadro.
    maestro = [
        ('30612502354', 'ALCO ROSARIO SA'), ('30716202956', 'DASEOS SA'),
        ('30719146194', 'HIKARI SA'), ('34684733496', 'NEOSTAR SA'),
        ('30708853557', 'XINOXIA SA'),
    ]
    _empresas_xlsx(app_env['empresas_path'], maestro)
    _acreditaciones_sin_ci(app_env['db_path'], [
        'ALCO ROSARIO S.A.', 'DASEOS S.A.', 'HIKARI S.A.', 'NEOSTAR S.A.', 'XINOXIA S.A.',
    ])

    total, empresas = _resumen(client)

    assert total == 5
    assert sum(empresas.values()) == 5
    for _, nombre in maestro:
        assert empresas[nombre] == 1, f'{nombre} no agrupó su movimiento'
    assert 'SIN EMPRESA ASIGNADA' not in empresas


@pytest.mark.parametrize('variante', [
    'ALCO ROSARIO S.A.', 'Alco Rosario S.A.', 'alco rosario sa',
    'ALCO  ROSARIO  SA', 'ALCO ROSARIO S.A', 'Alco Rosario SA.',
])
def test_variantes_de_escritura_caen_en_el_mismo_recuadro(client, app_env, variante):
    _empresas_xlsx(app_env['empresas_path'], [('30612502354', 'ALCO ROSARIO SA')])
    _acreditaciones_sin_ci(app_env['db_path'], [variante])

    total, empresas = _resumen(client)

    assert empresas['ALCO ROSARIO SA'] == 1, f'"{variante}" quedó fuera del recuadro'
    assert sum(empresas.values()) == total


def _filas_de(client, url):
    import re
    body = client.get(url).get_data(as_text=True)
    return len(re.findall(r'data-label="CUIT"', body))


def test_hacer_clic_en_un_recuadro_muestra_exactamente_lo_que_dice(client, app_env):
    # El recuadro contaba con nombre normalizado y el filtro comparaba texto
    # exacto: un recuadro que decía 2 podía abrir la grilla vacía.
    _empresas_xlsx(app_env['empresas_path'], [('30612502354', 'ALCO ROSARIO SA')])
    _acreditaciones_sin_ci(app_env['db_path'], ['ALCO ROSARIO S.A.', 'ALCO ROSARIO SA'])

    _, empresas = _resumen(client)
    filas = _filas_de(
        client,
        '/records?empresa=ALCO+ROSARIO+SA&ci_filter=vacios&monto_filter=positivos',
    )

    assert empresas['ALCO ROSARIO SA'] == 2
    assert filas == 2, f'el recuadro dice 2 pero la grilla muestra {filas}'


def test_el_recuadro_sin_empresa_abre_justo_esos_movimientos(client, app_env):
    _empresas_xlsx(app_env['empresas_path'], [('30612502354', 'ALCO ROSARIO SA')])
    _acreditaciones_sin_ci(app_env['db_path'], ['ALCO ROSARIO SA', '', 'OTRA COSA SRL'])

    _, empresas = _resumen(client)
    filas = _filas_de(
        client,
        f'/records?empresa={web_app.SIN_EMPRESA_FILTRO}&ci_filter=vacios&monto_filter=positivos',
    )

    assert empresas['SIN EMPRESA ASIGNADA'] == 2
    assert filas == 2, f'el recuadro dice 2 pero la grilla muestra {filas}'


def test_filtrar_por_empresa_alcanza_todas_sus_variantes_de_escritura(client, app_env):
    _empresas_xlsx(app_env['empresas_path'], [('30612502354', 'ALCO ROSARIO SA')])
    _acreditaciones_sin_ci(app_env['db_path'], [
        'ALCO ROSARIO S.A.', 'Alco Rosario SA', 'ALCO ROSARIO SA', 'XINOXIA SA',
    ])

    filas = _filas_de(client, '/records?empresa=ALCO+ROSARIO+SA')

    assert filas == 3


def test_las_empresas_del_maestro_siguen_apareciendo_con_cero(client, app_env):
    _empresas_xlsx(app_env['empresas_path'], [
        ('30612502354', 'ALCO ROSARIO SA'),
        ('34684733496', 'NEOSTAR SA'),
    ])
    _acreditaciones_sin_ci(app_env['db_path'], ['ALCO ROSARIO SA'])

    total, empresas = _resumen(client)

    assert empresas['NEOSTAR SA'] == 0
    assert sum(empresas.values()) == total
