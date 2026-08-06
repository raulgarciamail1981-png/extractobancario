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


def _santa_fe_record(record_hash, nro_comprobante, saldo, source_file, monto=3000000.0, fecha='05/08/2026'):
    return {
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': 'Alco Rosario SA',
        'CUIT': '30612502354', 'Cuenta': '000005252000', 'Moneda': 'ARS', 'Banco': 'Santa Fe',
        'Descripcion': 'CRED VS | ' + nro_comprobante, 'Debito': None, 'Credito': monto, 'Monto': monto,
        'Saldo': saldo, 'CI': '', 'SourceFile': source_file, 'SourceRow': 14,
        'RAW_nro comprobante': nro_comprobante,
    }


def test_upsert_santa_fe_reconciles_by_nro_comprobante_despite_different_saldo(tmp_path):
    # Bug real reportado: dos extracciones de Santa Fe el mismo día muestran
    # el mismo movimiento ("Movimientos del día", saldo todavía provisorio)
    # con Saldo distinto porque entre una y otra se asentaron más
    # movimientos. Fecha, Monto y Descripcion son iguales; solo el Saldo
    # cambia. La reconciliación no es exclusiva de Santander: cualquier banco
    # con un número de comprobante estable en el Raw debe reusar la fila.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santa_fe_record('hash1', '636478', 36867133.87, 'Movimientos del dia - 2026-08-05T123745.xls'),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _santa_fe_record('hash2', '636478', 55867133.87, 'Movimientos del dia - 2026-08-05T142114.xls'),
    ]), db_path=db_path)

    assert new_count == 0
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 1
    assert loaded.iloc[0]['Saldo'] == 55867133.87
    assert loaded.iloc[0]['RecordHash'] == 'hash2'


def test_upsert_santa_fe_does_not_merge_different_nro_comprobante(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _santa_fe_record('hash1', '636478', 36867133.87, 'archivo1.xls'),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _santa_fe_record('hash2', '636999', 40000000.0, 'archivo2.xls'),
    ]), db_path=db_path)

    assert new_count == 1
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 2


def _macro_btob_record(record_hash, nro_referencia, monto, fecha='08/05/2026',
                        empresa='Alco Rosario SA', cuenta='376109405439550'):
    return {
        'RecordHash': record_hash, 'Fecha': fecha, 'Empresa': empresa,
        'CUIT': '30612502354', 'Cuenta': cuenta, 'Moneda': 'ARS', 'Banco': 'Macro',
        'Descripcion': f'TEF DATANET BTOB | {nro_referencia} | 2026', 'Debito': abs(monto),
        'Credito': None, 'Monto': monto, 'Saldo': 1000000.0, 'CI': '', 'SourceFile': 'archivo1.xls',
        'SourceRow': 1, 'RAW_nro de referencia': nro_referencia,
    }


def test_get_interbanking_candidates_lists_only_untagged_macro_btob(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _macro_btob_record('hash1', '120378926', -2163345.13),
        _macro_btob_record('hash2', '120378928', -375933.48),
        # Otra cuenta: no debe aparecer al pedir la cuenta de arriba.
        _macro_btob_record('hash3', '999', -100.0, cuenta='OTRA-CUENTA'),
        # Otro banco con concepto parecido: no es candidato.
        {**_record('hash4'), 'Banco': 'Santander', 'Descripcion': 'TEF DATANET BTOB | 1 | 2026'},
    ]), db_path=db_path)

    candidates = db.get_interbanking_candidates('Alco Rosario SA', '376109405439550', db_path=db_path)

    assert {c['hash'] for c in candidates} == {'hash1', 'hash2'}
    by_hash = {c['hash']: c for c in candidates}
    assert by_hash['hash1']['monto_abs'] == 2163345.13
    assert by_hash['hash1']['referencia'] == '120378926'


def test_macro_btob_never_auto_reconciles_even_with_same_amount_and_referencia(tmp_path):
    # Caso real reportado: un TEF DATANET BTOB reaparece en dos extractos con
    # el mismo importe y la misma "Nro. de Referencia". A diferencia de
    # Santander/Santa Fe, acá NO debe fusionarse solo (aunque parezca "el
    # mismo movimiento") — la decisión de agrupar TEF DATANET BTOB queda
    # siempre en manos del usuario, vía "Unificar registros Interbanking".
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _macro_btob_record('hash1', '120358916', -55705270.66),
    ]), db_path=db_path)

    total, new_count = db.upsert_movements(pd.DataFrame([
        _macro_btob_record('hash2', '120358916', -55705270.66),
    ]), db_path=db_path)

    assert new_count == 1
    loaded = db.load_movements(db_path=db_path)
    assert len(loaded) == 2
    candidates = db.get_interbanking_candidates('Alco Rosario SA', '376109405439550', db_path=db_path)
    assert {c['hash'] for c in candidates} == {'hash1', 'hash2'}


def test_get_interbanking_candidates_excludes_already_tagged(tmp_path):
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _macro_btob_record('hash1', '120378926', -2163345.13),
        _macro_btob_record('hash2', '120378928', -375933.48),
    ]), db_path=db_path)
    db.apply_interbanking_group(['hash1'], 'GRUPO-1', db_path=db_path)

    candidates = db.get_interbanking_candidates('Alco Rosario SA', '376109405439550', db_path=db_path)

    assert {c['hash'] for c in candidates} == {'hash2'}


def test_get_interbanking_pending_accounts_reflects_state(tmp_path):
    db_path = tmp_path / 'test.db'
    assert db.get_interbanking_pending_accounts(db_path=db_path) == []

    db.upsert_movements(pd.DataFrame([
        _macro_btob_record('hash1', '120378926', -2163345.13),
    ]), db_path=db_path)
    pending = db.get_interbanking_pending_accounts(db_path=db_path)
    assert pending == [{'empresa': 'Alco Rosario SA', 'cuenta': '376109405439550'}]

    db.apply_interbanking_group(['hash1'], 'GRUPO-1', db_path=db_path)
    assert db.get_interbanking_pending_accounts(db_path=db_path) == []


def test_apply_interbanking_group_tags_all_given_hashes_and_nothing_else(tmp_path):
    # Caso real: 1.520.000 = 120.000 + 1.000.000 + 400.000.
    db_path = tmp_path / 'test.db'
    db.upsert_movements(pd.DataFrame([
        _macro_btob_record('a_compensar', '999999', -1520000.0),
        _macro_btob_record('detalle1', '111111', -120000.0),
        _macro_btob_record('detalle2', '222222', -1000000.0),
        _macro_btob_record('detalle3', '333333', -400000.0),
        _macro_btob_record('otro', '444444', -50000.0),
    ]), db_path=db_path)

    updated = db.apply_interbanking_group(
        ['a_compensar', 'detalle1', 'detalle2', 'detalle3'], '999999', db_path=db_path,
    )

    assert updated == 4
    loaded = db.load_movements(db_path=db_path).set_index('RecordHash')
    for h in ['a_compensar', 'detalle1', 'detalle2', 'detalle3']:
        assert loaded.loc[h, 'RegistroUnificado'] == '999999'
    assert loaded.loc['otro', 'RegistroUnificado'] == ''


def test_log_action_and_load_audit_log(tmp_path):
    db_path = tmp_path / 'test.db'

    db.log_action('admin', 'login_success', db_path=db_path)
    db.log_action('admin', 'update_ci', detail={'antes': '', 'despues': 'CI-1'}, record_hash='hash1', db_path=db_path)

    entries = db.load_audit_log(db_path=db_path)

    assert len(entries) == 2
    assert entries[0]['action'] == 'update_ci'
    assert entries[0]['detail']['despues'] == 'CI-1'
    assert entries[1]['action'] == 'login_success'
