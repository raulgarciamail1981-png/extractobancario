"""En el Resumen queda solo el total compensado, no los detalles.

Interbanking agrupa los detalles de un día con el movimiento que el banco emite
al día siguiente por la suma de todos. Si se muestran los dos, el importe se
cuenta dos veces: en el caso real de ALCO eran 19.795.652,15 duplicados.

Cuál es el consolidado no queda guardado (todas las filas del grupo llevan la
misma etiqueta), pero se deduce: la pantalla de Interbanking exige al aplicar
que la suma de los detalles dé igual al importe a compensar, así que el
consolidado es el de mayor importe absoluto del grupo.
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
    users = [{'username': 'admin', 'password': generate_password_hash('clave12345'),
              'roles': ['admin'], 'empresas_primarias': [], 'empresas_secundarias': []}]
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


def _btob(db_path, record_hash, referencia, monto, cuenta='376109405439550'):
    db.upsert_movements(pd.DataFrame([{
        'RecordHash': record_hash, 'Fecha': '05/08/2026', 'Empresa': 'ALCO ROSARIO SA',
        'CUIT': '30612502354', 'Cuenta': cuenta, 'Moneda': '$', 'Banco': 'Macro',
        'Descripcion': f'TEF DATANET BTOB | {referencia} | 2026', 'Debito': abs(monto),
        'Credito': None, 'Monto': monto, 'Saldo': 1000.0, 'CI': '',
        'SourceFile': 'macro.xls', 'SourceRow': 1, 'RAW_nro de referencia': referencia,
    }]), db_path=db_path)


def _grupo_real(db_path):
    """El caso que se probó en local: un consolidado y tres detalles."""
    _btob(db_path, 'consolidado', '120378921', -1520000.0)
    _btob(db_path, 'detalle1', '120378926', -120000.0)
    _btob(db_path, 'detalle2', '120378928', -1000000.0)
    _btob(db_path, 'detalle3', '120378938', -400000.0)
    db.apply_interbanking_group(
        ['consolidado', 'detalle1', 'detalle2', 'detalle3'], '120378921', db_path=db_path)


def _login(client):
    client.post('/login', data={'username': 'admin', 'password': 'clave12345'})


def test_en_el_resumen_queda_solo_el_total_compensado(client, app_env):
    _grupo_real(app_env['db_path'])
    _login(client)

    body = client.get('/records').get_data(as_text=True)

    assert '1 movimiento(s) con los filtros aplicados' in body
    assert '120378921' in body
    for referencia in ('120378926', '120378928', '120378938'):
        assert referencia not in body


def test_los_detalles_se_pueden_ver_a_proposito(client, app_env):
    # Hace falta para revisar una unificación equivocada: los movimientos no se
    # borran, solo dejan de mostrarse.
    _grupo_real(app_env['db_path'])
    _login(client)

    body = client.get('/records?ver_compensados=1').get_data(as_text=True)

    assert '4 movimiento(s) con los filtros aplicados' in body
    for referencia in ('120378926', '120378928', '120378938'):
        assert referencia in body


def test_lo_que_no_esta_unificado_no_se_toca(client, app_env):
    _btob(app_env['db_path'], 'suelto1', '111', -500.0)
    _btob(app_env['db_path'], 'suelto2', '222', -700.0)
    _login(client)

    body = client.get('/records').get_data(as_text=True)

    assert '2 movimiento(s) con los filtros aplicados' in body


def test_no_se_mezclan_grupos_de_cuentas_distintas(app_env):
    # El número de referencia lo pone el banco y podría repetirse en otra
    # cuenta sin tener nada que ver.
    _btob(app_env['db_path'], 'a1', '999', -300.0, cuenta='111')
    _btob(app_env['db_path'], 'a2', '998', -300.0, cuenta='111')
    _btob(app_env['db_path'], 'b1', '999', -50.0, cuenta='222')
    db.apply_interbanking_group(['a1', 'a2'], '999', db_path=app_env['db_path'])
    db.apply_interbanking_group(['b1'], '999', db_path=app_env['db_path'])

    df = web_app.prepare_display_dataframe(db.load_movements(db_path=app_env['db_path']))

    # De la cuenta 111 queda uno de los dos; el de la cuenta 222 no se toca.
    assert sorted(df['RecordHash']) == ['a1', 'b1'] or sorted(df['RecordHash']) == ['a2', 'b1']
    assert 'b1' in list(df['RecordHash'])


def test_la_exportacion_muestra_lo_mismo_que_la_pantalla(client, app_env):
    _grupo_real(app_env['db_path'])
    _login(client)

    assert client.get('/download/excel').status_code == 200
    sin_filtros = {'empresa': '', 'banco': '', 'fecha': '', 'fecha_desde': '', 'fecha_hasta': '',
                   'monto_filter': 'all', 'ci_filter': 'all', 'search': '', 'moneda': '',
                   'vencido': '', 'ver_compensados': ''}
    with web_app.app.test_request_context('/'):
        df = web_app.load_filtered_export_dataframe(sin_filtros)

    assert len(df) == 1
    assert df.iloc[0]['RecordHash'] == 'consolidado'


def test_un_grupo_de_dos_por_el_mismo_importe_deja_uno(app_env):
    # Caso real: el banco consolidó un solo detalle, asi que las dos filas
    # tienen el mismo importe. Da igual cuál quede, pero tiene que quedar una.
    _btob(app_env['db_path'], 'x1', '120358916', -55705270.66)
    _btob(app_env['db_path'], 'x2', '120358916', -55705270.66)
    db.apply_interbanking_group(['x1', 'x2'], '120358916', db_path=app_env['db_path'])

    df = web_app.prepare_display_dataframe(db.load_movements(db_path=app_env['db_path']))

    assert len(df) == 1
