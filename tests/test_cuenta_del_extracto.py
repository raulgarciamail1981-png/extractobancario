"""De dónde sale el número de cuenta de un extracto, y en qué orden.

Resolverlo mal no da error: el movimiento no cruza contra DATOS BANCARIOS,
queda sin empresa y hay que asignarlo a mano en "Resolver filas sin Empresa ni
CUIT", fila por fila. Pasó con Galicia, que exporta sin ningún dato de cuenta
adentro del archivo.

Son cuatro fuentes, de más a menos confiable:

  1. Formato estricto en el contenido  ("118-004636/1")
  2. Nombre del archivo verificado contra el maestro  ("Extracto_CC44772203")
  3. Número pelado en una fila con etiqueta de cuenta
  4. Nombre del archivo sin verificar

Lo que cada test cuida es la FRONTERA entre dos niveles, no el nivel suelto.
"""
import pandas as pd
import pytest
from openpyxl import Workbook

from conciliador import (account_from_labeled_rows, account_from_strict_patterns,
                          extract_account_from_filename, extract_statement_account,
                          gather_statements, resolve_statement_account)

MAESTRO = [
    {'bank': 'GALICIA', 'currency': 'PESOS', 'account': 'CC $ 0000447-7 220-3',
     'empresa': 'ALCO ROSARIO S.A.', 'cuit': '30612502354'},
    {'bank': 'SANTANDER', 'currency': 'PESOS', 'account': '118-004636/1',
     'empresa': 'NEOSTAR S.A.', 'cuit': '34684733496'},
]


def _raw(filas):
    return pd.DataFrame(filas)


# ----------------------------- nivel por nivel -----------------------------

def test_nivel_1_formato_estricto():
    raw = _raw([['Cuenta corriente en Pesos Nro. 118-004636/1'], ['Fecha', 'Importe']])

    assert account_from_strict_patterns(raw) == '1180046361'


def test_nivel_3_numero_pelado_con_etiqueta():
    raw = _raw([['Número', '276109489716345'], ['Fecha', 'Importe']])

    assert account_from_labeled_rows(raw) == '276109489716345'


def test_nivel_3_no_toma_un_numero_sin_etiqueta_en_la_fila():
    # "Comision Servicio De Cuenta" tiene la palabra cuenta, pero no es una
    # etiqueta: la celda no arranca con ella.
    raw = _raw([['Comision Servicio De Cuenta', '123456789']])

    assert account_from_labeled_rows(raw) == ''


def test_nivel_4_nombre_del_archivo():
    assert extract_account_from_filename('Extracto_CC44772203 - 2026-08-04.xlsx') == '44772203'
    assert extract_account_from_filename('descargaUltimosMovimientos.xls') == ''


# --------------------------- fronteras entre niveles ------------------------

def test_el_formato_estricto_le_gana_al_nombre_del_archivo():
    # El nombre podría apuntar a una cuenta que existe en el maestro, pero el
    # contenido trae un dato duro: gana el contenido.
    raw = _raw([['Cuenta corriente Nro. 118-004636/1'], ['Fecha', 'Importe']])

    cuenta = resolve_statement_account(raw, 'Extracto_CC44772203.xlsx', MAESTRO)

    assert cuenta == '1180046361'


def test_el_nombre_verificado_le_gana_al_numero_pelado():
    # El caso Galicia: la fila tiene "Nro Operacion: ..." (parece etiqueta) y
    # un importe de 9 dígitos. El nombre del archivo apunta a una cuenta que
    # existe de verdad en el maestro, así que gana el nombre.
    raw = _raw([
        ['Fecha', 'Descripción', 'Créditos', 'Leyendas Adicionales 2'],
        ['2026-08-04 00:00:00', 'Rescate Fima', '148567758.38', 'Nro Operacion: 232519445'],
    ])

    cuenta = resolve_statement_account(raw, 'Extracto_CC44772203 - 2026-08-04.xlsx', MAESTRO)

    assert cuenta == '44772203'


def test_el_numero_pelado_le_gana_al_nombre_sin_verificar():
    # El nombre trae una cuenta que NO está en el maestro: no se le da
    # prioridad y se usa lo que dice el contenido.
    raw = _raw([['Número', '276109489716345'], ['Fecha', 'Importe']])

    cuenta = resolve_statement_account(raw, 'Extracto_CC99999999.xlsx', MAESTRO)

    assert cuenta == '276109489716345'


def test_sin_nada_en_el_contenido_queda_el_nombre_sin_verificar():
    raw = _raw([['Fecha', 'Importe'], ['2026-08-04 00:00:00', '1000']])

    cuenta = resolve_statement_account(raw, 'Extracto_CC99999999.xlsx', MAESTRO)

    assert cuenta == '99999999'


def test_sin_maestro_el_nombre_sigue_siendo_el_ultimo_recurso():
    # Sin DATOS BANCARIOS no hay nada contra qué verificar: el nombre no puede
    # ascender al nivel 2 y el contenido manda.
    raw = _raw([['Número', '276109489716345']])

    assert resolve_statement_account(raw, 'Extracto_CC44772203.xlsx', None) == '276109489716345'
    assert resolve_statement_account(_raw([['Fecha']]), 'Extracto_CC44772203.xlsx', None) == '44772203'


# ------------------------ importes que no son cuentas -----------------------

@pytest.mark.parametrize('importe', ['148567758.38', '148567758,38', '2.020987.77'])
def test_un_importe_no_se_toma_como_cuenta(importe):
    raw = _raw([['Nro Operacion: 232519445', importe]])

    assert account_from_labeled_rows(raw) != '148567758'


def test_de_una_fila_con_importe_y_cuenta_se_toma_la_cuenta():
    raw = _raw([['Nro. de Cuenta', '44772203', 'Saldo', '148567758.38']])

    assert account_from_labeled_rows(raw) == '44772203'


# --------------------- el orden de las filas ya no decide -------------------

def test_el_formato_estricto_gana_aunque_este_mas_abajo():
    # Antes se resolvía fila por fila: el número pelado de la fila 0 ganaba
    # solo por estar más arriba que el formato estricto de la fila 1.
    raw = _raw([
        ['Número', '999999999999'],
        ['Cuenta corriente Nro. 118-004636/1'],
    ])

    assert extract_statement_account(raw) == '1180046361'


# ------------------------------ de punta a punta ----------------------------

def _extracto_galicia(path):
    # Mismo formato que exporta Galicia: la cuenta no está en ninguna parte
    # del contenido, solo en el nombre del archivo.
    wb = Workbook()
    ws = wb.active
    ws.append(['Fecha', 'Descripción', 'Origen', 'Débitos', 'Créditos',
               'Grupo de Conceptos', 'Concepto', 'Número de Terminal',
               'Observaciones Cliente', 'Número de Comprobante',
               'Leyendas Adicionales 1', 'Leyendas Adicionales 2', 'Tipo de Movimiento', 'Saldo'])
    ws.append(['2026-08-03 00:00:00', 'Comision Servicio De Cuenta', None, '85000', '0',
               '000808 - Comisiones', '907394 - COMISION SERVICIO DE CUENTA', None,
               None, None, 'Julio 2026', None, 'Imputado', '2020987.77'])
    ws.append(['2026-08-04 00:00:00', 'Rescate Fima', None, '0', '148567758.38',
               '000916 - Inversiones', '917138 - RESCATE FIMA', None,
               None, '1', 'FIMA PREMIUM CLASE A', 'Nro Operacion: 232519445',
               'Imputado', '150570279.05'])
    wb.save(path)


def test_un_extracto_de_galicia_se_unifica_sin_pasar_por_resolver(tmp_path):
    _extracto_galicia(tmp_path / 'Extracto_CC44772203 - 2026-08-04T144428.266.xlsx')

    df = gather_statements(tmp_path, company_map={'30612502354': 'ALCO ROSARIO SA'},
                            company_name_map={}, bank_accounts=MAESTRO)

    assert len(df) == 2
    assert set(df['Cuenta']) == {'44772203'}
    assert set(df['Empresa']) == {'ALCO ROSARIO SA'}
    assert set(df['CUIT']) == {'30612502354'}
    assert set(df['Banco']) == {'Galicia'}
    # Y la fecha ISO con hora no se da vuelta: 3 y 4 de agosto, no 8 de marzo.
    assert sorted(str(f)[:10] for f in df['Fecha']) == ['2026-08-03', '2026-08-04']
