import pandas as pd

import db


def _record(record_hash, ci='', monto=100.0):
    return {
        'RecordHash': record_hash, 'Fecha': '15/07/2026', 'Empresa': 'Empresa Test',
        'CUIT': '20111111116', 'Cuenta': '1112223334', 'Moneda': 'ARS', 'Banco': 'Banco Test',
        'Descripcion': 'Movimiento', 'Debito': None, 'Credito': monto, 'Monto': monto,
        'Saldo': 1000.0, 'CI': ci, 'SourceFile': 'archivo1.xlsx', 'SourceRow': 1,
    }


def test_upsert_and_load_roundtrip(tmp_path):
    db_path = tmp_path / 'test.db'
    df = pd.DataFrame([_record('hash1')])

    total, new_count = db.upsert_movements(df, db_path=db_path)

    assert total == 1
    assert new_count == 1
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 1
    assert loaded.iloc[0]['Empresa'] == 'Empresa Test'
    assert loaded.iloc[0]['Monto'] == 100.0


def test_upsert_preserves_existing_ci_on_conflict(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_record('hash1', ci='CI-100')]), db_path=db_path)

    # Re-unificar el mismo movimiento (mismo RecordHash) no debe pisar el CI ya cargado.
    db.upsert_movements(pd.DataFrame([_record('hash1', ci='')]), db_path=db_path)

    loaded = db.load_movements(db_path=db_path)
    assert loaded.iloc[0]['CI'] == 'CI-100'


def test_upsert_reports_new_vs_existing_counts(tmp_path):
    db_path = tmp_path / 'test.db'
    total1, new1 = db.upsert_movements(pd.DataFrame([_record('hash1'), _record('hash2')]), db_path=db_path)
    assert (total1, new1) == (2, 2)

    # Re-unificar hash1 (ya existe) junto con hash3 (nuevo).
    total2, new2 = db.upsert_movements(pd.DataFrame([_record('hash1'), _record('hash3')]), db_path=db_path)
    assert (total2, new2) == (2, 1)


def test_update_ci_changes_value(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_record('hash1')]), db_path=db_path)

    updated = db.update_ci('hash1', 'CI-200', db_path=db_path)

    assert updated is True
    loaded = db.load_movements(db_path=db_path)
    assert loaded.iloc[0]['CI'] == 'CI-200'


def test_clear_movements_empties_table(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([_record('hash1'), _record('hash2')]), db_path=db_path)

    deleted = db.clear_movements(db_path=db_path)

    assert deleted == 2
    assert db.load_movements(db_path=db_path).empty


def _santander_record(record_hash, descripcion, referencia, monto=100.0, saldo=1000.0, fecha='15/07/2026'):
    return {
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': 'Empresa Test',
        'CUIT': '20111111116', 'Cuenta': '118-004636/1', 'Moneda': 'ARS', 'Banco': 'Santander',
        'Descripcion': descripcion, 'Debito': None, 'Credito': monto, 'Monto': monto,
        'Saldo': saldo, 'CI': '', 'SourceFile': 'archivo1.xls', 'SourceRow': 1,
        'RAW_referencia': referencia,
    }


def test_upsert_santander_updates_descripcion_instead_of_duplicating(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santander_record('hash1', '0718 | Rosario Centro | 1858 | 28775158 | Pago a proveed acred santander rio', '28775158'),
    ]), db_path=db_path)

    # Mismo movimiento (misma Referencia que le asigna Santander), reexportado
    # con una Descripcion distinta y un hash distinto: debe actualizar la fila
    # existente, no agregar una nueva.
    total, new_count = db.upsert_movements(pd.DataFrame([
        _santander_record('hash2', '0000 | Casa Central | 1859 | 28775158 | Acreditacion cta pago proveed', '28775158'),
    ]), db_path=db_path)

    assert new_count == 0
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 1
    assert loaded.iloc[0]['Descripcion'] == '0000 | Casa Central | 1859 | 28775158 | Acreditacion cta pago proveed'
    assert loaded.iloc[0]['RecordHash'] == 'hash2'


def test_upsert_santander_pending_row_gets_saldo_filled_when_confirmed(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santander_record('hash1', 'Transferencia pendiente', '99001', saldo=None),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _santander_record('hash2', 'Transferencia confirmada', '99001', saldo=1000.0),
    ]), db_path=db_path)

    assert new_count == 0
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 1
    assert loaded.iloc[0]['Descripcion'] == 'Transferencia confirmada'
    assert loaded.iloc[0]['Saldo'] == 1000.0


def test_upsert_santander_does_not_merge_movements_with_different_referencia(tmp_path):
    # Caso real detectado: varias transferencias distintas del mismo día
    # pueden coincidir en Fecha e Importe (y ninguna trae Saldo informado
    # todavía), pero cada una tiene su propia Referencia asignada por
    # Santander. Sin exigir esa Referencia, se fusionarían por error.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santander_record('hash1', 'Transferencia recibida - De fulano', '96889889', monto=460000.0, saldo=None),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _santander_record('hash2', 'Transferencia recibida - De mengano', '39381806', monto=460000.0, saldo=None),
    ]), db_path=db_path)

    assert new_count == 1
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 2


def test_upsert_santander_without_referencia_does_not_reconcile(tmp_path):
    # Si por algún motivo no se pudo extraer la Referencia (extracto atípico
    # parseado por el fallback heurístico), no hay que arriesgarse a
    # fusionar: debe insertarse como fila nueva.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santander_record('hash1', 'Movimiento sin referencia', '', saldo=None),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _santander_record('hash2', 'Movimiento sin referencia', '', saldo=None),
    ]), db_path=db_path)

    assert new_count == 1
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 2


def test_log_action_and_load_audit_log(tmp_path):
    db_path = tmp_path / 'test.db'

    db.log_action('admin', 'login_success', db_path=db_path)
    db.log_action('admin', 'update_ci', detail={'antes': '', 'despues': 'CI-1'}, record_hash='hash1', db_path=db_path)

    entries = db.load_audit_log(db_path=db_path)

    assert len(entries) == 2
    assert entries[0]['action'] == 'update_ci'
    assert entries[0]['detail']['despues'] == 'CI-1'
    assert entries[1]['action'] == 'login_success'
