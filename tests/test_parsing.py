from datetime import datetime

import pandas as pd

from conciliador import (account_matches, canonicalize_bank_name, canonicalize_currency,
                          extract_cuit_from_text, hash_record, is_valid_cuit,
                          normalize_account, normalize_cuit, parse_date, parse_decimal,
                          parse_fecha_column)


def test_parse_decimal_argentine_thousands_and_decimals():
    assert parse_decimal('1.234,56') == 1234.56


def test_parse_decimal_parentheses_are_negative():
    assert parse_decimal('(1.234,56)') == -1234.56


def test_parse_decimal_us_style_thousands():
    assert parse_decimal('1,234.56') == 1234.56


def test_parse_decimal_strips_currency_symbols():
    assert parse_decimal('$ 500,00') == 500.0


def test_parse_decimal_empty_or_dash_is_none():
    assert parse_decimal('') is None
    assert parse_decimal('-') is None
    assert parse_decimal(None) is None


def test_parse_date_dmy_format():
    assert parse_date('15/07/2026') == datetime(2026, 7, 15)


def test_parse_date_iso_format():
    assert parse_date('2026-07-15') == datetime(2026, 7, 15)


def test_parse_fecha_column_does_not_swap_day_and_month_for_iso_dates():
    # Bug real detectado: pd.to_datetime(..., dayfirst=True) sobre una fecha
    # ISO con día y mes <=12 (ej. "2026-07-08") la leía como 7 de agosto en
    # vez de 8 de julio. parse_fecha_column debe evitarlo.
    result = parse_fecha_column(pd.Series(['2026-07-08']))
    assert result.iloc[0] == pd.Timestamp('2026-07-08')


def test_parse_fecha_column_still_handles_legacy_dmy_strings():
    result = parse_fecha_column(pd.Series(['08/07/2026']))
    assert result.iloc[0] == pd.Timestamp('2026-07-08')


def test_parse_fecha_column_handles_mixed_iso_and_dmy_in_same_column():
    result = parse_fecha_column(pd.Series(['2026-07-08', '08/07/2026', '15/07/2026', None]))
    assert result.tolist() == [
        pd.Timestamp('2026-07-08'), pd.Timestamp('2026-07-08'), pd.Timestamp('2026-07-15'), pd.NaT,
    ]


def test_parse_date_passthrough_datetime():
    value = datetime(2026, 7, 15)
    assert parse_date(value) == value


def test_parse_date_invalid_returns_none():
    assert parse_date('no es una fecha') is None


def test_normalize_cuit_strips_separators():
    assert normalize_cuit('20-12345678-9') == '20123456789'


def test_normalize_account_strips_separators_and_slash():
    assert normalize_account('123-456/7') == '1234567'


def test_account_matches_exact():
    assert account_matches('1112223334', '1112223334') is True


def test_account_matches_suffix_when_long_enough():
    assert account_matches('040-1112223334', '1112223334') is True


def test_account_matches_false_when_different():
    assert account_matches('1112223334', '9998887776') is False


def test_account_matches_false_when_too_short_and_different():
    assert account_matches('123', '456') is False


def test_extract_cuit_from_text_finds_pattern():
    assert extract_cuit_from_text('CUIT: 20-12345678-9 Titular') == '20123456789'


def test_extract_cuit_from_text_no_match_returns_empty():
    assert extract_cuit_from_text('sin datos aqui') == ''


def test_is_valid_cuit_accepts_real_cuit():
    # ALCO ROSARIO S.A., tomado de DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx
    assert is_valid_cuit('30612502354') is True


def test_is_valid_cuit_rejects_arbitrary_11_digit_run():
    # Sustring de un número de cuenta (Macro), no es un CUIT real.
    assert is_valid_cuit('37610940543') is False


def test_extract_cuit_from_text_rejects_false_positive_without_dashes():
    # Un número de comprobante embebido en una descripción de transacción no
    # debe confundirse con un CUIT solo porque tiene 11 dígitos seguidos.
    text = 'TEF DATANET PR Berkley Internatio 37610940543'
    assert extract_cuit_from_text(text) == ''


def test_extract_cuit_from_text_accepts_dashed_even_if_checksum_looks_off():
    # Con guiones el formato es inequívoco, se acepta sin exigir el dígito
    # verificador (por si la fuente tiene un error de tipeo real).
    assert extract_cuit_from_text('CUIT 37-61094054-3') == '37610940543'


def test_hash_record_differs_on_any_field_change():
    base = {'Banco': 'Galicia', 'Cuenta': '123', 'Fecha': '15/07/2026', 'Monto': '100', 'Descripcion': 'pago', 'Saldo': '900', 'CUIT': '20123456789'}
    other = dict(base, Monto='200')
    assert hash_record(base) != hash_record(other)


def test_hash_record_stable_for_identical_input():
    record = {'Banco': 'Galicia', 'Cuenta': '123', 'Fecha': '15/07/2026', 'Monto': '100', 'Descripcion': 'pago', 'Saldo': '900', 'CUIT': '20123456789'}
    assert hash_record(record) == hash_record(dict(record))


def test_hash_record_ignores_cosmetic_formatting_differences_in_description():
    # Mismo movimiento de ICBC exportado dos veces: un archivo trae la
    # sucursal con cero adelante y el débito con decimales explícitos, el
    # otro no. No debe generar dos filas distintas en el unificado.
    base = {
        'Banco': 'ICBC', 'Cuenta': '05060211819462', 'Fecha': '2026-07-01',
        'Monto': -11550.0, 'Saldo': 22152417.36, 'CUIT': '30719146194',
        'Descripcion': '206 | I V A | -11550,00 | PROCESO BATCH | 0506',
    }
    reexported = dict(base, Descripcion='206 | I V A | -11550 | PROCESO BATCH | 506')
    assert hash_record(base) == hash_record(reexported)


def test_canonicalize_bank_name_normalizes_case_variants():
    # "Datos Bancarios" puede traer el banco en mayúsculas, distinto de como
    # lo escribe guess_bank() por nombre de archivo; deben terminar iguales.
    assert canonicalize_bank_name('SANTANDER') == 'Santander'
    assert canonicalize_bank_name('santander') == 'Santander'
    assert canonicalize_bank_name('Santander') == 'Santander'


def test_canonicalize_bank_name_leaves_unknown_bank_unchanged():
    assert canonicalize_bank_name('Banco Test') == 'Banco Test'


def test_canonicalize_currency_translates_spanish_text_to_app_vocabulary():
    assert canonicalize_currency('PESOS') == '$'
    assert canonicalize_currency('DOLARES') == 'USD'
    assert canonicalize_currency('Dólares') == 'USD'


def test_hash_record_still_differs_for_genuinely_different_description():
    base = {'Banco': 'Galicia', 'Cuenta': '123', 'Fecha': '15/07/2026', 'Monto': '100', 'Descripcion': 'Pago proveedor', 'Saldo': '900', 'CUIT': '20123456789'}
    other = dict(base, Descripcion='Cobro cliente')
    assert hash_record(base) != hash_record(other)
