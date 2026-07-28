import pandas as pd
from openpyxl import Workbook

from conciliador import gather_statements


def _write_xlsx(path, rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


def _write_text_xls(path, lines):
    path.write_bytes('\r\n'.join(lines).encode('cp1252'))


def test_gather_statements_resolves_bank_and_company_from_account_not_filename(tmp_path):
    # El nombre del archivo es genérico a propósito: en producción los extractos
    # no van a traer el banco en el nombre.
    _write_xlsx(tmp_path / 'archivo1.xlsx', [
        ['Extracto de cuenta'],
        ['Cuenta Nro. 111-222333/4'],
        ['Fecha', 'Descripcion', 'Monto', 'Saldo'],
        ['15/07/2026', 'Transferencia recibida', '1500,50', '20000,00'],
    ])
    bank_accounts = [{
        'bank': 'Banco Test', 'currency': 'ARS', 'account': '1112223334',
        'empresa': 'Empresa Test SA', 'cuit': '20111111116',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    record = df.iloc[0]
    assert record['Banco'] == 'Banco Test'
    assert record['Empresa'] == 'Empresa Test SA'
    assert record['CUIT'] == '20111111116'
    assert record['Cuenta'] == '1112223334'
    assert record['Credito'] == 1500.50


def test_gather_statements_prefers_empresas_xlsx_spelling_over_datos_bancarios(tmp_path):
    # Empresas.xlsx (el maestro) dice "ALCO ROSARIO SA" (sin puntos); el
    # cruce de cuenta contra DATOS BANCARIOS trae "ALCO ROSARIO S.A." (con
    # puntos) para la misma empresa. Empresas.xlsx debe ganar siempre que
    # se conozca el CUIT, para que no aparezcan dos variantes del mismo
    # nombre en los filtros.
    _write_xlsx(tmp_path / 'archivo_generico_x7.xlsx', [
        ['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Cuenta'],
        ['15/07/2026', 'Transferencia recibida', '1500,50', '20000,00', '1112223334'],
    ])
    bank_accounts = [{
        'bank': 'Banco Test', 'currency': 'ARS', 'account': '1112223334',
        'empresa': 'ALCO ROSARIO S.A.', 'cuit': '30612502354',
    }]
    company_map = {'30612502354': 'ALCO ROSARIO SA'}

    df = gather_statements(tmp_path, company_map=company_map, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    assert df.iloc[0]['Empresa'] == 'ALCO ROSARIO SA'


def test_gather_statements_leaves_unregistered_account_unresolved(tmp_path):
    _write_xlsx(tmp_path / 'archivo2.xlsx', [
        ['Cuenta Nro. 999-888777/6'],
        ['Fecha', 'Descripcion', 'Monto', 'Saldo'],
        ['16/07/2026', 'Pago proveedor', '-300,00', '19700,00'],
    ])
    bank_accounts = [{
        'bank': 'Banco Test', 'currency': 'ARS', 'account': '1112223334',
        'empresa': 'Empresa Test SA', 'cuit': '20111111116',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    record = df.iloc[0]
    assert record['Banco'] == 'Desconocido'
    assert record['Empresa'] == ''
    assert record['CUIT'] == ''


def test_gather_statements_icbc_description_and_debit_sign(tmp_path):
    # Layout real de ICBC: la cuenta viene en la primera línea del archivo
    # ("Movimientos de CC $ ..."), no en una columna, y el Débito ya viene
    # con el signo puesto (a diferencia de otros bancos, que lo traen como
    # magnitud positiva).
    _write_xlsx(tmp_path / 'extracto_generico.xlsx', [
        ['Movimientos de CC $ 0506/02118194/62'],
        ['Fecha contable', 'Cod de Concepto', 'Concepto', 'Debito en $', 'Credito en $', 'Saldo en $',
         'Informacion Complementaria', 'Nro de cheque', 'Sucursal Origen', 'Canal'],
        ['17/07/2026', '260', 'IMP S/CRED CT', '-123352,18', '', '470504,72', '2', '', '0506', 'PROCESO BATCH'],
    ])
    bank_accounts = [{
        'bank': 'ICBC', 'currency': 'ARS', 'account': '0506/02118194/62',
        'empresa': 'Empresa ICBC SA', 'cuit': '20222222223',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    record = df.iloc[0]
    assert record['Banco'] == 'ICBC'
    assert record['Empresa'] == 'Empresa ICBC SA'
    assert record['Descripcion'] == '260 | IMP S/CRED CT | -123352,18 | PROCESO BATCH | 0506'
    assert record['Debito'] == 123352.18
    assert record['Monto'] == -123352.18
    assert record['Saldo'] == 470504.72


def test_gather_statements_prefers_account_crossref_cuit_over_free_text_lookalike(tmp_path):
    # "37610940543" es un substring de 11 dígitos de un número de cuenta (no
    # un CUIT real). El cruce por cuenta contra DATOS BANCARIOS debe ganarle
    # a cualquier número parecido a un CUIT que aparezca en la descripción.
    _write_xlsx(tmp_path / 'archivo_generico_x1.xlsx', [
        ['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Cuenta'],
        ['14/07/2026', 'TEF DATANET PR Berkley Internatio 37610940543', '93696,26', '5134777,98', '376109405439550'],
    ])
    bank_accounts = [{
        'bank': 'MACRO', 'currency': 'PESOS', 'account': '376109405439550',
        'empresa': 'ALCO ROSARIO S.A.', 'cuit': '30612502354',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    record = df.iloc[0]
    assert record['Banco'] == 'Macro'
    assert record['Empresa'] == 'ALCO ROSARIO S.A.'
    assert record['CUIT'] == '30612502354'


def test_gather_statements_ignores_metadata_row_masquerading_as_header(tmp_path):
    # Fila de metadata ("Tipo"/"Cuenta Corriente") que matchea >=2 tokens
    # conocidos pero no es el encabezado real de la tabla de movimientos.
    _write_xlsx(tmp_path / 'archivo_generico_x2.xlsx', [
        ['Ultimos Movimientos'],
        ['Tipo', None, 'Cuenta Corriente'],
        ['Numero', None, '376109405439550'],
        ['Moneda', None, 'PESOS'],
        ['Fecha', None, None, 'Nro. de Referencia', 'Causal', 'Concepto', 'Importe', None, None, None, 'Saldo'],
        ['14/07/2026', None, None, '1950669654', '1684', 'DBCR 25413 S/DB TASA GRAL', '-18,93', None, None, None, '630420,73'],
    ])
    bank_accounts = [{
        'bank': 'MACRO', 'currency': 'PESOS', 'account': '376109405439550',
        'empresa': 'ALCO ROSARIO S.A.', 'cuit': '30612502354',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    # Solo la fila de transacción real; las filas de metadata (Tipo/Numero/
    # Moneda) no deben colarse como movimientos.
    assert len(df) == 1
    record = df.iloc[0]
    assert record['Banco'] == 'Macro'
    assert record['Empresa'] == 'ALCO ROSARIO S.A.'
    assert record['Descripcion'] == 'DBCR 25413 S/DB TASA GRAL | 1950669654 | 1684'


def test_gather_statements_skips_trailing_blank_row(tmp_path):
    _write_xlsx(tmp_path / 'archivo_generico_x3.xlsx', [
        ['Fecha', 'Descripcion', 'Monto', 'Saldo', 'Cuenta'],
        ['14/07/2026', 'Movimiento real', '100,00', '900,00', '1112223334'],
        [None, None, None, None, None],
    ])
    bank_accounts = [{
        'bank': 'Banco Test', 'currency': 'ARS', 'account': '1112223334',
        'empresa': 'Empresa Test SA', 'cuit': '20111111116',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    assert df.iloc[0]['Descripcion'] == 'Movimiento real'


def test_gather_statements_santander_text_loads_both_pending_and_confirmed_sections(tmp_path):
    # Extracto de texto tab-delimited real de Santander: trae una sección "a
    # confirmar" (Movimientos del Día) y, después del marcador "Últimos
    # Movimientos", la sección ya confirmada. Ahora deben importarse ambas;
    # solo el encabezado repetido y las líneas sueltas (marcador, cuenta,
    # resumen de saldo, timestamp) deben descartarse, no como movimientos.
    _write_text_xls(tmp_path / 'extracto_generico_x6.xls', [
        'Movimientos del Día',
        '',
        'Cuenta corriente en Pesos Nro. 999-111222/3',
        '',
        'Fecha\tSuc. Origen\tDesc. Sucursal\tCod. Operativo\tReferencia\tConcepto\tImporte\tSaldo',
        '17/07/2026\t0000\tCasa Central\t1111\t000001\tPendiente no confirmado\t100,00',
        '',
        'Saldo al 17/07/2026 999999,00',
        '',
        '17/07/2026 10:00:00',
        '',
        'Últimos Movimientos',
        '',
        'Cuenta corriente en Pesos Nro. 999-111222/3',
        '',
        'Fecha\tSuc. Origen\tDesc. Sucursal\tCod. Operativo\tReferencia\tConcepto\tImporte\tSaldo',
        '16/07/2026\t0718\tRosario Centro\t4637\t000035167\tImpuesto ley 25413\t(8.856,00)\t6124505,58',
        '',
        '17/07/2026 10:35:52',
    ])
    bank_accounts = [{
        'bank': 'SANTANDER', 'currency': 'PESOS', 'account': '9991112223',
        'empresa': 'Empresa Test SA', 'cuit': '20111111116',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 2
    pending = df[df['Fecha'] == pd.Timestamp('2026-07-17')].iloc[0]
    confirmed = df[df['Fecha'] == pd.Timestamp('2026-07-16')].iloc[0]
    # "SANTANDER" (mayúsculas, tal como viene de Datos Bancarios) se
    # canoniza a la forma consistente usada en toda la app.
    assert pending['Banco'] == 'Santander RIO'
    assert confirmed['Banco'] == 'Santander RIO'
    assert pending['Descripcion'] == '0000 | Casa Central | 1111 | 000001 | Pendiente no confirmado'
    assert pending['Monto'] == 100.00
    assert pd.isna(pending['Saldo'])
    assert confirmed['Descripcion'] == '0718 | Rosario Centro | 4637 | 000035167 | Impuesto ley 25413'
    assert confirmed['Debito'] == 8856.00
    assert confirmed['Monto'] == -8856.00


def test_gather_statements_uses_datos_bancarios_currency_over_missing_row_currency(tmp_path):
    # El extracto de Santa Fe no trae una columna de moneda confiable, pero
    # la cuenta ya está registrada como USD en Datos Bancarios: esa
    # asignación debe ganar en vez de caer al "$" por defecto.
    _write_xlsx(tmp_path / 'archivo_generico_x8.xlsx', [
        ['Nro. de Cuenta:', 'CC USD 500035642207'],
        ['Fecha', 'Concepto', 'Nro. comprobante', 'Debito', 'Credito', 'Saldo'],
        ['15/07/2026', 'DEP EFEC', '0000180266', None, '72194865', '72404074.93'],
    ])
    bank_accounts = [{
        'bank': 'SANTA FE', 'currency': 'DOLARES', 'account': '500035642207',
        'empresa': 'HIKARI S.A.', 'cuit': '30719146194',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    assert df.iloc[0]['Moneda'] == 'USD'
    assert df.iloc[0]['Banco'] == 'Santa Fe'


def test_gather_statements_municipal_description_combines_descripcion_and_comprobante(tmp_path):
    _write_xlsx(tmp_path / 'archivo_generico_x4.xlsx', [
        ['Cuenta', 'CUIT Cuenta', 'Fecha', 'Monto', 'N de Comprobante', 'Descripcion', 'Saldo'],
        ['CC $ 3000027839', '30612502354', '20/04/2026', '6000000', '121898', '396-H2H Acreditacion DEBIN', '9314184.81'],
    ])
    bank_accounts = [{
        'bank': 'MUNICIPAL', 'currency': 'PESOS', 'account': '3000027839',
        'empresa': 'ALCO ROSARIO S.A.', 'cuit': '30612502354',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    assert df.iloc[0]['Descripcion'] == '396-H2H Acreditacion DEBIN | 121898'


def test_gather_statements_santa_fe_description_combines_concepto_and_comprobante(tmp_path):
    _write_xlsx(tmp_path / 'archivo_generico_x5.xlsx', [
        ['Nro. de Cuenta:', 'CC $ 500035642207'],
        ['Fecha', 'Concepto', 'Nro. comprobante', 'Debito', 'Credito', 'Saldo'],
        ['15/07/2026', 'DEP EFEC', '0000180266', None, '72194865', '72404074.93'],
    ])
    bank_accounts = [{
        'bank': 'SANTA FE', 'currency': 'PESOS', 'account': '500035642207',
        'empresa': 'HIKARI S.A.', 'cuit': '30719146194',
    }]

    df = gather_statements(tmp_path, company_map={}, company_name_map={}, bank_accounts=bank_accounts)

    assert len(df) == 1
    assert df.iloc[0]['Descripcion'] == 'DEP EFEC | 0000180266'
