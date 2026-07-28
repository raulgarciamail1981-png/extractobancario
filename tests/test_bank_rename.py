"""El renombre de bancos toca el RecordHash, que es lo que evita duplicados.

Estos tests cubren el camino completo: datos cargados con el nombre viejo,
migración, y reimportación de los mismos extractos con el nombre nuevo.
"""
import pandas as pd
import pytest
from openpyxl import Workbook

import conciliador
import db
import migrate_bank_names
from conciliador import BANK_DESCRIPTION_COLUMNS, BANK_PATTERNS, gather_statements

# Cómo se llamaban los bancos antes del renombre.
BANK_PATTERNS_VIEJOS = dict(BANK_PATTERNS, santander='Santander', municipal='Municipal')
BANK_DESCRIPTION_COLUMNS_VIEJOS = {
    **{k: v for k, v in BANK_DESCRIPTION_COLUMNS.items() if k not in ('SANTANDER RIO', 'BANCO MUNICIPAL')},
    'SANTANDER': BANK_DESCRIPTION_COLUMNS['SANTANDER RIO'],
    'MUNICIPAL': BANK_DESCRIPTION_COLUMNS['BANCO MUNICIPAL'],
}


def _escribir_extracto_santander(path):
    wb = Workbook()
    ws = wb.active
    ws.append(['Fecha', 'Suc. Origen', 'Desc. Sucursal', 'Cod. Operativo', 'Referencia', 'Concepto', 'Monto', 'Saldo'])
    ws.append(['15/07/2026', '118', 'ROSARIO', '0042', '99887766', 'TRANSFERENCIA', '1500,50', '20000,00'])
    ws.append(['16/07/2026', '118', 'ROSARIO', '0043', '99887767', 'PAGO PROVEEDOR', '-845,30', '19154,70'])
    wb.save(path)


def _importar(carpeta):
    return gather_statements(carpeta, company_map={}, company_name_map={}, bank_accounts=None)


@pytest.fixture
def con_nombres_viejos(monkeypatch):
    """Deja el módulo como estaba antes del renombre, para simular datos ya cargados."""
    monkeypatch.setattr(conciliador, 'BANK_PATTERNS', BANK_PATTERNS_VIEJOS)
    monkeypatch.setattr(conciliador, 'BANK_DESCRIPTION_COLUMNS', BANK_DESCRIPTION_COLUMNS_VIEJOS)
    yield
    monkeypatch.undo()


def test_migracion_deja_los_hashes_listos_para_la_proxima_importacion(tmp_path, con_nombres_viejos, monkeypatch):
    # Este es el test que justifica que exista migrate_bank_names.py: sin él,
    # reimportar los mismos extractos después del renombre duplicaría todo.
    db_path = tmp_path / 'conciliador.db'
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _escribir_extracto_santander(carpeta / 'extracto_santander_julio.xlsx')

    # 1. Carga original, con el nombre viejo, y una cajera carga un CI.
    viejos = _importar(carpeta)
    assert set(viejos['Banco']) == {'Santander'}
    db.upsert_movements(viejos, db_path=db_path)
    hash_con_ci = viejos.iloc[0]['RecordHash']
    db.update_ci(hash_con_ci, 'CI-9001', db_path=db_path)

    # 2. Se despliega el renombre y se corre la migración.
    monkeypatch.undo()
    migrate_bank_names.migrate(str(db_path))

    # 3. El mes siguiente se vuelve a subir el mismo extracto.
    nuevos = _importar(carpeta)
    assert set(nuevos['Banco']) == {'Santander RIO'}
    total, agregados = db.upsert_movements(nuevos, db_path=db_path)

    assert agregados == 0, 'la reimportación duplicó movimientos: el hash migrado no coincide'
    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 2
    assert set(guardados['Banco']) == {'Santander RIO'}
    assert set(guardados.loc[guardados['CI'] != '', 'CI']) == {'CI-9001'}


def test_migracion_es_idempotente(tmp_path, con_nombres_viejos, monkeypatch):
    db_path = tmp_path / 'conciliador.db'
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _escribir_extracto_santander(carpeta / 'extracto_santander_julio.xlsx')
    db.upsert_movements(_importar(carpeta), db_path=db_path)

    monkeypatch.undo()
    primera = migrate_bank_names.migrate(str(db_path))
    segunda = migrate_bank_names.migrate(str(db_path))

    assert primera['renombradas'] == 2
    assert segunda['renombradas'] == 0
    assert len(db.load_movements(db_path=db_path)) == 2


def test_migracion_fusiona_en_vez_de_fallar_si_ya_se_importo_con_el_nombre_nuevo(
    tmp_path, con_nombres_viejos, monkeypatch,
):
    # Caso real: alguien actualiza el código y sube extractos ANTES de correr
    # la migración. Quedan las dos versiones del mismo movimiento; migrar no
    # tiene que reventar por clave duplicada ni perder el CI cargado.
    db_path = tmp_path / 'conciliador.db'
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _escribir_extracto_santander(carpeta / 'extracto_santander_julio.xlsx')

    viejos = _importar(carpeta)
    db.upsert_movements(viejos, db_path=db_path)
    db.update_ci(viejos.iloc[0]['RecordHash'], 'CI-9001', db_path=db_path)

    monkeypatch.undo()
    nuevos = _importar(carpeta)
    db.upsert_movements(nuevos, db_path=db_path)
    assert len(db.load_movements(db_path=db_path)) == 4, 'precondición: conviven las dos versiones'

    resumen = migrate_bank_names.migrate(str(db_path))

    assert resumen['fusionadas'] == 2
    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 2
    assert set(guardados['Banco']) == {'Santander RIO'}
    # El CI que la cajera había cargado sobre la fila vieja se conserva.
    assert set(guardados.loc[guardados['CI'] != '', 'CI']) == {'CI-9001'}


def test_dry_run_no_toca_la_base(tmp_path, con_nombres_viejos, monkeypatch):
    db_path = tmp_path / 'conciliador.db'
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _escribir_extracto_santander(carpeta / 'extracto_santander_julio.xlsx')
    db.upsert_movements(_importar(carpeta), db_path=db_path)

    monkeypatch.undo()
    resumen = migrate_bank_names.migrate(str(db_path), dry_run=True)

    assert resumen['renombradas'] == 2
    assert set(db.load_movements(db_path=db_path)['Banco']) == {'Santander'}


def test_descripcion_de_santander_se_sigue_armando_con_las_columnas_posicionales(tmp_path):
    # BANK_DESCRIPTION_COLUMNS se indexa por el nombre del banco en mayúsculas:
    # si el renombre no se replica ahí, la descripción pasa a armarse con la
    # columna genérica y queda incompleta.
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _escribir_extracto_santander(carpeta / 'extracto_santander_julio.xlsx')

    df = _importar(carpeta)

    assert df.iloc[0]['Descripcion'] == '118 | ROSARIO | 0042 | 99887766 | TRANSFERENCIA'


def test_reconciliacion_de_santander_sigue_activa_con_el_nombre_nuevo(tmp_path):
    # db._find_reconcilable_match filtra por nombre de banco. Si ese filtro no
    # reconoce "Santander RIO", los movimientos "a confirmar" que el banco
    # reclasifica vuelven a entrar duplicados.
    db_path = tmp_path / 'conciliador.db'
    base = {
        'Fecha': '15/07/2026', 'Empresa': 'Empresa Test', 'CUIT': '20111111116',
        'Cuenta': '1112223334', 'Moneda': '$', 'Banco': 'Santander RIO',
        'Debito': None, 'Credito': 100.0, 'Monto': 100.0, 'Saldo': None,
        'CI': '', 'SourceFile': 'santander.xls', 'SourceRow': 1,
        'RAW_referencia': '99887766',
    }
    pendiente = dict(base, RecordHash='hash_pendiente', Descripcion='A CONFIRMAR')
    db.upsert_movements(pd.DataFrame([pendiente]), db_path=db_path)

    # El banco reclasifica el mismo movimiento: cambia la descripción y el
    # hash, pero mantiene el número de referencia.
    confirmado = dict(base, RecordHash='hash_confirmado', Descripcion='118 | TRANSFERENCIA', Saldo=5000.0)
    db.upsert_movements(pd.DataFrame([confirmado]), db_path=db_path)

    guardados = db.load_movements(db_path=db_path)
    assert len(guardados) == 1, 'la reconciliación de Santander dejó de reconocer el banco renombrado'
    assert guardados.iloc[0]['Descripcion'] == '118 | TRANSFERENCIA'
    assert guardados.iloc[0]['Saldo'] == 5000.0
