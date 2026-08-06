"""Santa Fe recalcula el Saldo entre una descarga y la siguiente.

En "Movimientos del día" la lista se reordena a medida que entran movimientos
nuevos, y el saldo acumulado de los que ya estaban cambia. Como el Saldo forma
parte del RecordHash, el mismo movimiento volvía a entrar como uno nuevo.

Caso real: la cuenta 000005252000 de ALCO, bajada dos veces el 05/08/2026.

    12:37   CRED VS  636478   3.000.000   saldo 36.867.133,87
    14:21   CRED VS  636478   3.000.000   saldo 55.867.133,87

Es el mismo movimiento. Lo que lo identifica sin ambigüedad es el
"Nro. comprobante", que no cambia.
"""
import pandas as pd

import db


def _santa_fe(record_hash, comprobante, saldo, monto=3000000.0, concepto='CRED VS',
               fecha='05/08/2026', cuenta='000005252000'):
    return {
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': 'ALCO ROSARIO SA',
        'CUIT': '30612502354', 'Cuenta': cuenta, 'Moneda': '$', 'Banco': 'Santa Fe',
        'Descripcion': f'{concepto} | {comprobante}', 'Debito': None, 'Credito': monto,
        'Monto': monto, 'Saldo': saldo, 'CI': '', 'SourceFile': 'Movimientos del dia.xls',
        'SourceRow': 1, 'RAW_nro comprobante': comprobante,
    }


def test_el_mismo_movimiento_con_otro_saldo_no_se_duplica(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_santa_fe('hash1', '636478', 36867133.87)]), db_path=db_path)

    total, nuevas = db.upsert_movements(
        pd.DataFrame([_santa_fe('hash2', '636478', 55867133.87)]), db_path=db_path)

    assert nuevas == 0
    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 1
    # Queda el saldo de la descarga más reciente.
    assert guardados.iloc[0]['Saldo'] == 55867133.87
    assert guardados.iloc[0]['RecordHash'] == 'hash2'


def test_dos_comprobantes_distintos_siguen_siendo_dos_movimientos(tmp_path):
    # Mismo día, mismo importe, distinto comprobante: son dos depósitos
    # distintos y tienen que quedar los dos.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santa_fe('hash1', '0000023955', 36867133.87, concepto='DEP EFEC'),
    ]), db_path=db_path)

    total, nuevas = db.upsert_movements(pd.DataFrame([
        _santa_fe('hash2', '0000023967', 52867133.87, concepto='DEP EFEC'),
    ]), db_path=db_path)

    assert nuevas == 1
    assert len(db.load_movements(db_path=db_path)) == 2


def test_sin_comprobante_no_se_arriesga_a_fusionar(tmp_path):
    # Si no se pudo leer el comprobante, mejor un duplicado que perder un
    # movimiento real por fusionarlo con otro.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_santa_fe('hash1', '', 100.0)]), db_path=db_path)

    total, nuevas = db.upsert_movements(pd.DataFrame([_santa_fe('hash2', '', 200.0)]), db_path=db_path)

    assert nuevas == 1
    assert len(db.load_movements(db_path=db_path)) == 2


def test_no_se_fusiona_entre_cuentas_distintas(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santa_fe('hash1', '636478', 36867133.87, cuenta='000005252000'),
    ]), db_path=db_path)

    total, nuevas = db.upsert_movements(pd.DataFrame([
        _santa_fe('hash2', '636478', 55867133.87, cuenta='000051173507'),
    ]), db_path=db_path)

    assert nuevas == 1
    assert len(db.load_movements(db_path=db_path)) == 2


def test_no_se_fusiona_entre_dias_distintos(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_santa_fe('hash1', '636478', 100.0, fecha='04/08/2026')]), db_path=db_path)

    total, nuevas = db.upsert_movements(
        pd.DataFrame([_santa_fe('hash2', '636478', 200.0, fecha='05/08/2026')]), db_path=db_path)

    assert nuevas == 1
    assert len(db.load_movements(db_path=db_path)) == 2


def test_la_reconciliacion_vale_para_cualquier_banco(tmp_path):
    # No hay lista de bancos habilitados: alcanza con que el extracto traiga un
    # número de comprobante o referencia (_REFERENCE_FIELD_CANDIDATES). Galicia
    # entra igual que Santa Fe.
    db_path = tmp_path / 'test.db'
    primero = _santa_fe('hash1', '636478', 100.0)
    primero['Banco'] = 'Galicia'
    segundo = _santa_fe('hash2', '636478', 200.0)
    segundo['Banco'] = 'Galicia'
    db.upsert_movements(pd.DataFrame([primero]), db_path=db_path)

    total, nuevas = db.upsert_movements(pd.DataFrame([segundo]), db_path=db_path)

    assert nuevas == 0
    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 1
    assert guardados.iloc[0]['Saldo'] == 200.0


def test_el_ci_ya_cargado_no_se_pierde_al_reconciliar(tmp_path):
    # Si una cajera ya le puso el CI y después llega otra descarga con el
    # saldo cambiado, el CI tiene que seguir ahí.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_santa_fe('hash1', '636478', 36867133.87)]), db_path=db_path)
    db.update_ci('hash1', 'CI-777', db_path=db_path)

    db.upsert_movements(pd.DataFrame([_santa_fe('hash2', '636478', 55867133.87)]), db_path=db_path)

    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 1
    assert guardados.iloc[0]['CI'] == 'CI-777'
