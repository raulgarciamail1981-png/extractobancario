"""Formatos tal como los exportan los bancos.

Los datos son inventados, pero la forma del archivo (codificación, cabecera,
extensión) reproduce la de los archivos reales: los dos casos de acá hacían
que el extracto entero se descartara sin leer un solo movimiento.
"""
from pathlib import Path

import pytest

from conciliador import (gather_statements, is_text_file, read_csv_file,
                          read_text_guessing_encoding)

CUENTAS = [
    {'bank': 'MUNICIPAL', 'currency': 'PESOS', 'account': 'CC $ 3000000001',
     'empresa': 'EMPRESA UNO S.A.', 'cuit': '20111111116'},
    {'bank': 'SANTANDER', 'currency': 'DOLARES', 'account': 'CC U$S 118-000000/1',
     'empresa': 'EMPRESA DOS S.A.', 'cuit': '20222222223'},
]


def _csv_municipal(path: Path) -> None:
    """Reporte de Banco Municipal: cp1252, con "N° de Comprobante"."""
    contenido = (
        'Cuenta         ;CUIT Cuenta;Fecha     ;Monto     ;N° de Comprobante;Descripción                  ;Saldo\n'
        'CC $ 3000000001;20111111116;20/07/2026;-254216,24;335616           ;9104-Débito por Canon Leasing;1449001,63\n'
        'CC $ 3000000001;20111111116;21/07/2026;125000,00 ;335617           ;Acreditación de haberes      ;1574001,63\n'
    )
    path.write_bytes(contenido.encode('cp1252'))


def _xls_santander(path: Path) -> None:
    """Export de Santander: texto plano con extensión .xls, cp1252, con tabs.

    Arranca con "Últimos Movimientos" — esa "Ú" es la que rompía la detección.
    """
    lineas = [
        '', '', 'Últimos Movimientos', '',
        'Cuenta corriente en Dólares Nro. 118-000000/1', '',
        '\tFecha\tSuc. Origen\tDesc. Sucursal\tCod. Operativo\tReferencia\tConcepto\tImporte\tSaldo',
        '\t22/07/2026\t0000\tCasa Central - Work Café\t4805\t00977766\tTransferencia recibida\t15000,50\t120000,00',
        '\t21/07/2026\t0000\tCasa Central - Work Café\t4805\t00814307\tPago a proveedor\t-8300,25\t111699,75',
    ]
    path.write_bytes('\r\n'.join(lineas).encode('cp1252'))


def test_el_export_de_santander_se_reconoce_como_texto(tmp_path):
    # Empieza con "Últimos Movimientos": el acento hacía que la detección lo
    # tomara por binario y descartara el archivo entero.
    archivo = tmp_path / 'descargaUltimosMovimientos.xls'
    _xls_santander(archivo)

    assert is_text_file(archivo) is True


def test_un_xlsx_de_verdad_no_se_confunde_con_texto(tmp_path):
    from openpyxl import Workbook
    archivo = tmp_path / 'real.xlsx'
    wb = Workbook()
    wb.active.append(['Fecha', 'Monto'])
    wb.save(archivo)

    assert is_text_file(archivo) is False


def test_el_csv_de_municipal_se_lee_pese_al_simbolo_de_grado(tmp_path):
    # "N° de Comprobante" viene en cp1252; leerlo en UTF-8 estricto hacía
    # fallar el archivo completo por ese único carácter.
    archivo = tmp_path / 'BEE_Reporte.csv'
    _csv_municipal(archivo)

    df = read_csv_file(archivo)

    assert len(df) == 2
    assert 'cuit cuenta' in df.columns


@pytest.mark.parametrize('encoding', ['cp1252', 'utf-8'])
def test_se_leen_las_dos_codificaciones(tmp_path, encoding):
    archivo = tmp_path / 'reporte.csv'
    archivo.write_bytes('Fecha;Descripción;Monto\n20/07/2026;Depósito;100,00\n'.encode(encoding))

    contenido, _ = read_text_guessing_encoding(archivo)

    assert 'Descripción' in contenido
    assert 'Depósito' in contenido


def test_los_dos_formatos_juntos_entran_al_unificar(tmp_path):
    # Extremo a extremo: los dos archivos que en producción no se podían
    # unificar tienen que resolver banco, empresa y CUIT por cruce de cuenta.
    carpeta = tmp_path / 'uploads'
    carpeta.mkdir()
    _csv_municipal(carpeta / 'BEE_Reporte.csv')
    _xls_santander(carpeta / 'descargaUltimosMovimientos.xls')

    df = gather_statements(carpeta, company_map={}, company_name_map={}, bank_accounts=CUENTAS)

    assert len(df) == 4, f'se esperaban 4 movimientos, llegaron {len(df)}'
    assert set(df['Banco']) == {'Banco Municipal', 'Santander RIO'}
    assert (df['CUIT'].astype(str).str.strip() != '').all(), 'quedaron movimientos sin CUIT'
    assert (df['Empresa'].astype(str).str.strip() != '').all(), 'quedaron movimientos sin empresa'
    assert df['Monto'].notna().all()
    assert df['Fecha'].notna().all()
