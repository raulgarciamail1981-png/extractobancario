import argparse
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path('.')
EMPRESAS_FILE = ROOT / 'Empresas.xlsx'
BANK_ACCOUNTS_FILE = ROOT / 'DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx'
MASTER_FILE = ROOT / 'extractos_unificados.xlsx'
SUPPORTED_EXTENSIONS = {'.csv', '.xls', '.xlsx'}
LOGO_FILE = Path(__file__).resolve().parent / 'static' / 'neostar-logo.jpg'

# Nombre con el que se muestra cada banco en toda la app (grilla, filtros,
# exportaciones). La clave es el texto que se busca —ya normalizado a
# minúsculas y sin acentos— dentro del nombre de archivo o de la celda del
# maestro de datos bancarios.
#
# OJO al cambiar un valor de acá: el nombre del banco forma parte del
# RecordHash que identifica cada movimiento, así que renombrarlo hace que los
# movimientos ya guardados dejen de reconocerse y se dupliquen en la siguiente
# importación. Hay que acompañarlo con una migración de la base
# (migrate_bank_names.py). Además, las claves de BANK_DESCRIPTION_COLUMNS son
# estos mismos nombres en mayúsculas y tienen que seguir coincidiendo.
BANK_PATTERNS = {
    'icbc': 'ICBC',
    'galicia': 'Galicia',
    'macro': 'Macro',
    'municipal': 'Banco Municipal',
    'santa fe': 'Santa Fe',
    'santander': 'Santander RIO',
}

COMPANY_PATTERNS = {
    'hikari': 'Hikari',
    'alco': 'Alco',
    'daseos': 'Daseos',
    'xinoxia': 'Xinoxia',
    'municipal': 'Municipal',
    'santa fe': 'Santa Fe',
}

COMMON_HEADERS = {
    'fecha', 'fecha contable', 'fecha de operación', 'date', 'fecha movimiento', 'fecha de movimiento',
    'descripcion', 'descripción', 'concepto', 'origen', 'detalle', 'info', 'informacion complementaria',
    'debito', 'debito en', 'debitos', 'débitos', 'credito', 'credito en', 'creditos', 'crédito', 'monto',
    'saldo', 'saldo en', 'nro. de cuenta', 'numero de cuenta', 'cuenta', 'cta. cte. bancaria', 'cuenta corriente',
    'cuit', 'cuit cuenta', 'número de cuenta', 'numero de cuenta', 'moneda', 'tipo', 'cbu', 'sucursal origen',
}

DATE_COLUMNS = ['fecha', 'fecha contable', 'fecha de operación', 'fecha movimiento', 'date']
DESCRIPTION_COLUMNS = ['descripcion', 'descripción', 'concepto', 'origen', 'detalle', 'informacion complementaria', 'observaciones cliente']
# normalize_header() convierte "Debito en $" -> "debito en" (el "$" se
# reemplaza por un espacio y se recorta), por eso estas listas no llevan "$".
DEBIT_COLUMNS = ['debito', 'debito en', 'debitos', 'débitos', 'importe debito', 'debit']
CREDIT_COLUMNS = ['credito', 'credito en', 'creditos', 'crédito', 'importe credito', 'credit']
AMOUNT_COLUMNS = ['monto', 'importe', 'amount']
BALANCE_COLUMNS = ['saldo', 'saldo en']
ACCOUNT_COLUMNS = [
    'nro. de cuenta', 'numero de cuenta', 'cuenta', 'cta. cte. bancaria',
    'cuenta corriente', 'cbu', 'nro cuenta', 'nro cta', 'numero de cta',
    'cuenta cte', 'cuenta bancaria', 'numero de cuenta corriente'
]
CUIT_COLUMNS = [
    'cuit', 'cuit cuenta', 'cuitempresa', 'cuit_empresa', 'cuit empresa',
    'cuit titular', 'cuit contribuyente'
]
COMPANY_COLUMNS = ['empresa', 'razon social', 'razon_social', 'nombre', 'company']
CURRENCY_COLUMNS = ['moneda', 'divisa']
H_COLUMNS = ['h']
I_COLUMNS = ['i']

# Algunos bancos no traen una única columna de "descripción" útil; la
# descripción real hay que armarla concatenando varias columnas puntuales del
# extracto (posición 0-indexed, ej. columna "F" de Excel = índice 5). Se
# arma sobre el banco ya resuelto (cruce de cuenta o filename), no sobre el
# nombre de archivo, para que funcione igual sin importar cómo se detectó el
# banco.
# Las claves son los valores de BANK_PATTERNS en mayúsculas (así los busca
# build_record): si se renombra un banco allá, hay que renombrarlo acá también
# o la descripción de ese banco se arma con la columna genérica.
BANK_DESCRIPTION_COLUMNS = {
    'MACRO': [5, 3, 4],                 # Concepto | Nro. de Referencia | Causal
    'SANTANDER RIO': [1, 2, 3, 4, 5],   # Suc. Origen | Desc. Sucursal | Cod. Operativo | Referencia | Concepto
    'BANCO MUNICIPAL': [5, 4],          # Descripción | N° de Comprobante
    'SANTA FE': [1, 2],                 # Concepto | Nro. comprobante
    'ICBC': [1, 2, 3, 9, 8],            # Cod de Concepto | Concepto | Debito en $ | Canal | Sucursal Origen
}


def clean_text(value: object) -> str:
    if pd.isna(value):
        return ''
    text = str(value).strip()
    text = re.sub(r'[\r\n]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_header(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace('ú', 'u').replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('á', 'a').replace('ñ', 'n')
    value = re.sub(r'[^a-z0-9 ]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def normalize_text(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace('ú', 'u').replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('á', 'a').replace('ñ', 'n')
    value = re.sub(r'[^a-z0-9 ]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def guess_bank(file_name: str) -> str:
    name = file_name.lower()
    for pattern, bank in BANK_PATTERNS.items():
        if pattern in name:
            return bank
    return ''


def canonicalize_bank_name(bank: str) -> str:
    # El banco puede llegar con distinta capitalización según la fuente (el
    # nombre de archivo siempre usa BANK_PATTERNS, pero el texto libre de
    # "DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx" puede traer "SANTANDER" en
    # mayúsculas); sin esto, un mismo banco aparece dos veces en los filtros.
    normalized = normalize_text(bank)
    for pattern, canonical in BANK_PATTERNS.items():
        if pattern in normalized:
            return canonical
    return bank


def canonicalize_currency(value: str) -> str:
    # "DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx" describe la moneda de cada
    # cuenta en texto libre ("PESOS"/"DOLARES"); se traduce al vocabulario
    # interno de la app ('$'/'USD') para que coincida con lo que ya se
    # extrae directamente de algunos extractos.
    normalized = normalize_text(value)
    if 'usd' in normalized or 'dolar' in normalized:
        return 'USD'
    if 'peso' in normalized or normalized == 'ars':
        return '$'
    return value


def guess_company(file_name: str, cuit: Optional[str], company_map: Dict[str, str]) -> str:
    if cuit:
        cuit_norm = normalize_cuit(cuit)
        if cuit_norm in company_map:
            return company_map[cuit_norm]
    name = file_name.lower()
    for pattern, company in COMPANY_PATTERNS.items():
        if pattern in name:
            return company
    return ''


def normalize_cuit(value: object) -> str:
    if pd.isna(value):
        return ''
    text = str(value)
    text = text.strip().replace('-', '').replace('.', '').replace(' ', '')
    return re.sub(r'[^0-9]', '', text)


def normalize_account(value: object) -> str:
    if pd.isna(value):
        return ''
    text = str(value)
    text = text.strip().replace(' ', '').replace('-', '').replace('.', '')
    return re.sub(r'[^0-9]', '', text)


def account_matches(source: object, target: object) -> bool:
    source_norm = normalize_account(source)
    target_norm = normalize_account(target)
    if not source_norm or not target_norm:
        return False
    if source_norm == target_norm:
        return True
    if len(source_norm) >= 6 and len(target_norm) >= 6:
        return source_norm.endswith(target_norm) or target_norm.endswith(source_norm)
    return False


CUIT_TEXT_PATTERN = re.compile(r'(\d{2}-\d{8}-\d|\d{11})')
_CUIT_CHECK_WEIGHTS = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]


def is_valid_cuit(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits[:10], _CUIT_CHECK_WEIGHTS))
    verifier = 11 - (total % 11)
    if verifier == 11:
        verifier = 0
    if verifier == 10:
        return False
    return verifier == int(digits[10])


def extract_cuit_from_text(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ''
    # Un número de cuenta o de comprobante cualquiera puede tener 11 dígitos
    # seguidos por pura casualidad (ej. "...Berkley Internatio 30500035788"
    # en una descripción de transacción). Si el texto no trae el CUIT con
    # guiones (formato inequívoco), solo se acepta si el dígito verificador
    # de AFIP es válido, para no confundir números al azar con un CUIT real.
    for match in CUIT_TEXT_PATTERN.finditer(text):
        raw = match.group(0)
        candidate = normalize_cuit(raw)
        if '-' in raw or is_valid_cuit(candidate):
            return candidate
    return ''


def parse_decimal(value: object) -> Optional[float]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text == '' or text in {'-', '–'}:
        return None
    # detect parentheses as negative amounts e.g. (1.234,56)
    negative = False
    if '(' in text and ')' in text:
        negative = True
        text = text.replace('(', '').replace(')', '')
    text = text.replace('$', '').replace('USD', '').replace('ARS', '').replace('PESOS', '').replace('pesos', '')
    text = re.sub(r'[^0-9,\.\-]', '', text)
    if text.count(',') > 0 and text.count('.') > 0:
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif text.count(',') > 0:
        text = text.replace(',', '.')
    try:
        num = float(text)
        return -num if negative else num
    except ValueError:
        return None


def parse_date(value: object) -> Optional[datetime]:
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text == '':
        return None
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y', '%Y/%m/%d',
                '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        # Un texto que arranca en AAAA-MM-DD es ISO y el día nunca va primero:
        # con dayfirst=True, pandas da vuelta "2026-08-03 00:00:00" y lo lee
        # como 8 de marzo. Macro exporta la fecha así, y el error es silencioso
        # porque solo se nota cuando día y mes son <= 12.
        dayfirst = not re.match(r'\d{4}-\d{2}-\d{2}', text)
        parsed = pd.to_datetime(text, dayfirst=dayfirst, errors='coerce')
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime()
    except Exception:
        return None


def parse_fecha_column(series: pd.Series) -> pd.Series:
    # La columna 'Fecha' puede traer AAAA-MM-DD (ISO, desde la base SQLite) o
    # DD/MM/AAAA (extractos/Excel legado). "dayfirst=True" sobre un texto ISO
    # con día y mes <=12 invierte mes y día (ej. "2026-07-08" se lee como 7
    # de agosto). Por eso primero se intenta ISO estricto, y solo lo que no
    # matchea cae al parseo genérico con dayfirst=True.
    iso_parsed = pd.to_datetime(series, format='%Y-%m-%d', errors='coerce')
    remaining = iso_parsed.isna() & series.notna()
    if remaining.any():
        fallback = pd.to_datetime(series[remaining], dayfirst=True, errors='coerce')
        iso_parsed.loc[remaining] = fallback
    return iso_parsed


def detect_csv_separator(sample_lines: List[str]) -> str:
    if not sample_lines:
        return ';'
    counts = {sep: 0 for sep in [',', ';', '\t', '|']}
    for line in sample_lines[:5]:
        for sep in counts:
            counts[sep] += line.count(sep)
    return max(counts, key=counts.get)


def find_header_row(data: pd.DataFrame) -> Optional[int]:
    for idx in range(min(20, len(data))):
        row = data.iloc[idx].astype(str).fillna('').tolist()
        normalized = [normalize_header(cell) for cell in row]
        found = sum(1 for token in normalized if token in COMMON_HEADERS)
        # Filas de metadata como "Tipo | Cuenta Corriente" también matchean
        # >=2 tokens conocidos; exigir una columna de fecha evita confundirlas
        # con el encabezado real de la tabla de movimientos.
        has_date_column = any(token in DATE_COLUMNS for token in normalized)
        if found >= 2 and has_date_column:
            return idx
    return None


def locate_confirmed_section(raw: pd.DataFrame) -> pd.DataFrame:
    normalized = raw.fillna('').astype(str).map(normalize_text)
    marker = 'ultimos movimientos'
    for idx, row in normalized.iterrows():
        if any(marker in str(cell) for cell in row):
            return raw.iloc[idx + 1 :].reset_index(drop=True)
    return raw


# Formatos vistos en extractos reales: "118-004636/1" (Santander/Galicia) y
# "0506/02118194/62" (ICBC, tres grupos separados por barras).
STRICT_ACCOUNT_PATTERNS = [
    re.compile(r'(\d{2,3}-\d{3,8}/\d+)'),
    re.compile(r'(\d{3,4}/\d{5,10}/\d{1,3})'),
]
# Número "pelado" (ej. Macro, Santa Fe): solo se acepta si otra celda de la
# misma fila ES una etiqueta de cuenta (ej. "Número", "Nro. de Cuenta:"), no
# alcanza con que la palabra aparezca en un texto libre como una descripción
# ("Comisión Servicio de Cuenta" no debe disparar esto).
LOOSE_ACCOUNT_PATTERN = re.compile(r'(\d{6,20})')
ACCOUNT_LABEL_CELL_PATTERN = re.compile(r'^(cuenta|cta|nro|numero|cbu)\b')
ACCOUNT_LABEL_CELL_MAX_LENGTH = 25


def _is_account_label_cell(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized or len(normalized) > ACCOUNT_LABEL_CELL_MAX_LENGTH:
        return False
    return bool(ACCOUNT_LABEL_CELL_PATTERN.match(normalized))


def _iter_row_texts(raw: pd.DataFrame):
    for _, row in raw.iterrows():
        texts = [clean_text(value) for value in row.astype(str).tolist()]
        if any(texts):
            yield texts


def account_from_strict_patterns(raw: pd.DataFrame) -> str:
    """Nivel 1: formatos inconfundibles (ej. "118-004636/1"). Muy confiable."""
    if raw is None or raw.empty:
        return ''
    for texts in _iter_row_texts(raw):
        for text in texts:
            if not text:
                continue
            compact = text.replace(' ', '')
            for pattern in STRICT_ACCOUNT_PATTERNS:
                match = pattern.search(compact)
                if match:
                    return normalize_account(match.group(1))
    return ''


def _es_parte_de_un_importe(compact: str, match: re.Match) -> bool:
    # Ningún número de cuenta tiene decimales. Un "148567758.38" en la columna
    # Créditos no puede ser una cuenta, aunque la fila tenga una celda que
    # parezca etiqueta ("Nro Operacion: ...").
    if re.match(r'[.,]\d', compact[match.end():]):
        return True
    anterior = compact[:match.start()]
    return bool(re.search(r'\d[.,]$', anterior))


def account_from_labeled_rows(raw: pd.DataFrame) -> str:
    """Nivel 3: número pelado en una fila que tiene etiqueta de cuenta.

    Es el más frágil de todos: alcanza con que una celda arranque con "Nro"
    para que se acepte cualquier corrida de 6 a 20 dígitos de esa fila. Por eso
    descarta lo que en realidad es un importe.
    """
    if raw is None or raw.empty:
        return ''
    for texts in _iter_row_texts(raw):
        if not any(_is_account_label_cell(text) for text in texts if text):
            continue
        for text in texts:
            if not text:
                continue
            compact = text.replace(' ', '')
            for match in LOOSE_ACCOUNT_PATTERN.finditer(compact):
                if not _es_parte_de_un_importe(compact, match):
                    return normalize_account(match.group(1))
    return ''


def extract_statement_account(raw: pd.DataFrame) -> str:
    # Las dos pasadas van completas y en orden de confiabilidad. Antes se
    # resolvía fila por fila, así que un número pelado en la fila 2 le ganaba a
    # un formato estricto en la fila 5 solo por estar más arriba.
    return account_from_strict_patterns(raw) or account_from_labeled_rows(raw)


# Algunos bancos (ej. Galicia) exportan extractos sin ningún dato de cuenta ni
# empresa en el contenido del archivo, pero el nombre que generan por default
# trae el número de cuenta con el prefijo "CC" (ej. "Extracto_CC761592207.xlsx").
# Es un último recurso: solo se usa si no se pudo extraer nada del contenido.
FILENAME_ACCOUNT_PATTERN = re.compile(r'cc[\s_-]*(\d{6,})', re.IGNORECASE)


def extract_account_from_filename(file_name: str) -> str:
    match = FILENAME_ACCOUNT_PATTERN.search(file_name)
    if match:
        return normalize_account(match.group(1))
    return ''


def resolve_statement_account(raw: pd.DataFrame, file_name: str,
                               bank_accounts: list[dict[str, str]] | None = None) -> str:
    """Número de cuenta del extracto, por orden de confiabilidad de la fuente.

    1. Formato estricto en el contenido: dato del propio extracto y con una
       forma inconfundible.
    2. Nombre del archivo, pero solo si esa cuenta existe en DATOS BANCARIOS.
       Lo genera el banco al exportar y es verificable.
    3. Número pelado en una fila con etiqueta de cuenta: sirve, pero se
       equivoca (en un extracto de Galicia tomaba el importe de un "Rescate
       Fima" de 148.567.758,38 como número de cuenta, y con eso los 20
       movimientos caían en "Resolver filas sin Empresa ni CUIT").
    4. Nombre del archivo sin verificar: mejor algo que nada.

    La clave del orden es que el nombre del archivo nunca le gana a un dato
    duro del contenido: solo le gana al rastreo frágil.
    """
    estricta = account_from_strict_patterns(raw)
    if estricta:
        return estricta
    from_name = extract_account_from_filename(file_name)
    if from_name and bank_accounts and find_bank_account(from_name, bank_accounts):
        return from_name
    return account_from_labeled_rows(raw) or from_name


TEXT_WHITESPACE_BYTES = (9, 10, 11, 12, 13)


def is_text_file(path: Path) -> bool:
    """¿El archivo es texto plano, más allá de que se llame .xls?

    Santander exporta sus movimientos como texto con extensión .xls. La
    detección mira solo si arranca con la firma de un Excel real (OLE o ZIP) y
    si el principio tiene bytes de control.

    No se exige ASCII puro: el archivo de Santander empieza con "Últimos
    Movimientos" y esa "Ú" (0xDA en cp1252) hacía que se lo tomara por binario,
    con lo cual el extracto entero se descartaba sin poder leerlo.
    """
    raw = path.read_bytes()
    if raw.startswith(b'\xd0\xcf\x11\xe0') or raw.startswith(b'PK'):
        return False
    return all(b in TEXT_WHITESPACE_BYTES or b >= 32 for b in raw[:512])


# Los bancos exportan en la codificación de Windows en español; algunos
# archivos vienen en UTF-8. Se prueba UTF-8 primero (más específico: un texto
# cp1252 con acentos casi nunca decodifica como UTF-8 válido) y se cae a
# cp1252, en vez de fallar y descartar el extracto.
TEXT_ENCODINGS = ('utf-8-sig', 'cp1252')


def read_text_guessing_encoding(path: Path) -> tuple[str, str]:
    """Devuelve (contenido, codificación usada)."""
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding='cp1252', errors='replace'), 'cp1252'


def _deduplicate_columns(columns: List[str]) -> List[str]:
    # A diferencia de dropear columnas duplicadas/vacías, renombrarlas
    # preserva la posición original de cada columna (necesario para poder
    # acceder por índice, ej. BANK_DESCRIPTION_COLUMNS).
    seen: Dict[str, int] = {}
    result = []
    for col in columns:
        if col not in seen:
            seen[col] = 0
            result.append(col)
        else:
            seen[col] += 1
            result.append(f'{col}_{seen[col]}')
    return result


def _parse_delimited_text_section(section_lines: List[str], account: str) -> Optional[pd.DataFrame]:
    if not section_lines:
        return None
    has_tabs = any('\t' in line for line in section_lines[:5])
    separator = '\t' if has_tabs else detect_csv_separator(section_lines)
    header_idx = None
    for idx, line in enumerate(section_lines[:20]):
        values = [normalize_header(cell) for cell in line.split(separator)]
        found = sum(1 for token in values if token in COMMON_HEADERS)
        has_date_column = any(token in DATE_COLUMNS for token in values)
        if found >= 2 and has_date_column:
            header_idx = idx
            break
    if header_idx is None:
        return None
    header = _deduplicate_columns([normalize_header(cell) for cell in section_lines[header_idx].split(separator)])
    records = []
    for line in section_lines[header_idx + 1:]:
        cells = line.split(separator)
        # Extractos con varios bloques (ej. Santander "a confirmar" +
        # "Últimos Movimientos") repiten el encabezado antes del segundo
        # bloque; hay que descartarlo como texto, no cargarlo como dato.
        repeated_values = [normalize_header(cell) for cell in cells]
        repeated_found = sum(1 for token in repeated_values if token in COMMON_HEADERS)
        repeated_has_date = any(token in DATE_COLUMNS for token in repeated_values)
        if repeated_found >= 2 and repeated_has_date:
            continue
        if len(cells) < len(header):
            cells = cells + [''] * (len(header) - len(cells))
        records.append(cells[:len(header)])
    if not records:
        return None
    df = pd.DataFrame(records, columns=header)
    if account and 'cuenta' not in df.columns:
        df['cuenta'] = account
    return df


def read_text_statement(path: Path) -> pd.DataFrame:
    text, _ = read_text_guessing_encoding(path)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    account = ''
    for line in lines[:20]:
        m = re.search(r'Cuenta .*Nro\.?\s*([0-9\-/]+)', line, re.IGNORECASE)
        if m:
            account = m.group(1).strip()
            break

    # Algunos bancos (ej. Santander) exportan primero los movimientos "a
    # confirmar" (todavía pueden cambiar) y, después de un marcador
    # "Últimos Movimientos", los ya confirmados. Se cargan ambos bloques: el
    # marcador y el encabezado repetido se descartan como texto, no como
    # datos, en _parse_delimited_text_section.
    section_lines = lines

    structured = _parse_delimited_text_section(section_lines, account)
    if structured is not None:
        return structured

    # Fallback: parser heurístico basado en "última cifra = saldo, anteúltima
    # = importe", para textos sin una estructura tabular con encabezado claro.
    rows = []
    current = None
    for line in section_lines:
        date_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s+', line)
        if date_match:
            if current:
                rows.append(current)
            current = line
            continue
        if current is not None:
            current += ' ' + line.strip()
    if current:
        rows.append(current)
    records = []
    for line in rows:
        text = re.sub(r'\s{2,}', ' ', line).strip()
        date_match = re.match(r'^(\d{2}/\d{2}/\d{4})\s+(.*)$', text)
        if not date_match:
            continue
        fecha = date_match.group(1)
        rest = date_match.group(2)
        balance = None
        amount = None
        saldo_match = re.search(r'([\(]?[0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?\)?)\s*$', rest)
        if saldo_match:
            balance = saldo_match.group(1)
            rest = rest[:saldo_match.start()].strip()
        amount_match = re.search(r'([\(]?[0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?\)?)\s*$', rest)
        if amount_match:
            amount = amount_match.group(1)
            rest = rest[:amount_match.start()].strip()
        records.append({
            'fecha': fecha,
            'descripcion': rest,
            'importe': amount or '',
            'saldo': balance or '',
            'cuenta': account,
        })
    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError('No se pudieron extraer registros del archivo de texto')
    df.columns = [normalize_header(col) for col in df.columns]
    return df


def read_excel_file(path: Path, return_raw: bool = False) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    if path.suffix.lower() == '.xls' and is_text_file(path):
        df = read_text_statement(path)
        return (df, df) if return_raw else df
    engine = 'openpyxl' if path.suffix.lower() == '.xlsx' else 'xlrd'
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str, engine=engine)
    raw = locate_confirmed_section(raw)
    header_row = find_header_row(raw)
    try:
        if header_row is None:
            df = raw.copy()
            df.columns = [f'column_{i}' for i in range(len(df.columns))]
        else:
            header = raw.iloc[header_row].astype(str).tolist()
            df = raw.iloc[header_row + 1 :].copy()
            df.columns = header
    except Exception:
        if path.suffix.lower() == '.xls':
            df = read_text_statement(path)
        else:
            raise
    df.columns = _deduplicate_columns([normalize_header(col) for col in df.columns])
    if return_raw:
        return df, raw
    return df


def read_csv_file(path: Path, return_raw: bool = False) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    # La codificación se detecta una vez y se reusa para pandas: leer siempre
    # en UTF-8 estricto hacía fallar el archivo entero por un solo carácter
    # (ej. el "°" de "N° de Comprobante" en los reportes de Banco Municipal).
    contenido, encoding = read_text_guessing_encoding(path)
    text = contenido.splitlines()
    separator = detect_csv_separator(text)
    header_row = None
    for idx, line in enumerate(text[:20]):
        values = [normalize_header(cell) for cell in line.split(separator)]
        found = sum(1 for token in values if token in COMMON_HEADERS)
        has_date_column = any(token in DATE_COLUMNS for token in values)
        if found >= 2 and has_date_column:
            header_row = idx
            break
    header = header_row if header_row is not None else 0
    df = pd.read_csv(path, sep=separator, header=header, dtype=str, engine='python', encoding=encoding, skip_blank_lines=True)
    df.columns = _deduplicate_columns([normalize_header(col) for col in df.columns])
    if return_raw:
        return df, pd.DataFrame({0: text})
    return df


def get_positional_text(row: pd.Series, index: int) -> str:
    try:
        value = row.iloc[index]
        return clean_text(value)
    except Exception:
        return ''


def load_bank_accounts(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], [], []
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str, engine='openpyxl')
    except Exception:
        return [], [], []
    if raw.empty:
        return [], [], []
    # "DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx" es una tabla pivot: columna 0
    # = banco, columna 1 = moneda, y desde la columna 2 en adelante una columna
    # por empresa (con el CUIT incluido en el texto del encabezado, ej.
    # "ALCO ROSARIO S.A. CUIT 30-61250235-4"). Cada celda de datos es el número
    # de cuenta de esa empresa en ese banco/moneda.
    header_row = raw.iloc[0]
    company_columns: dict[int, dict[str, str]] = {}
    for col_idx in range(2, len(header_row)):
        cell = clean_text(header_row.iloc[col_idx])
        if not cell:
            continue
        cuit = extract_cuit_from_text(cell)
        company_name = re.split(r'\bCUIT\b', cell, flags=re.IGNORECASE)[0].strip()
        if company_name:
            company_columns[col_idx] = {'empresa': company_name, 'cuit': cuit}
    bank_accounts = []
    current_bank = ''
    for _, row in raw.iloc[1:].iterrows():
        cells = row.tolist()
        bank_cell = clean_text(cells[0]) if len(cells) > 0 else ''
        if bank_cell:
            current_bank = bank_cell
        currency = clean_text(cells[1]) if len(cells) > 1 else ''
        for col_idx, company in company_columns.items():
            if col_idx >= len(cells):
                continue
            account = clean_text(cells[col_idx])
            if not account:
                continue
            bank_accounts.append({
                'bank': current_bank,
                'currency': currency,
                'account': account,
                'empresa': company['empresa'],
                'cuit': company['cuit'],
            })
    company_options: dict[str, str] = {}
    bank_options: set[str] = set()
    for entry in bank_accounts:
        bank_options.add(entry.get('bank', '') or '')
        if entry.get('cuit') and entry.get('empresa'):
            company_options[entry['cuit']] = entry['empresa']
    return bank_accounts, [
        {'cuit': cuit, 'empresa': empresa}
        for cuit, empresa in sorted(company_options.items(), key=lambda item: item[1])
        if cuit and empresa
    ], sorted([bank for bank in bank_options if bank])


def find_bank_account(account: object, bank_accounts: list[dict[str, str]]) -> dict[str, str] | None:
    if not account or not bank_accounts:
        return None
    for entry in bank_accounts:
        if account_matches(account, entry.get('account', '')):
            return entry
    return None


def extract_file_cuit(df: pd.DataFrame, raw_df: pd.DataFrame | None = None) -> str:
    for frame in (df, raw_df):
        if frame is None or frame.empty:
            continue
        if frame is df:
            for _, row in frame.iterrows():
                row_data = {col: clean_text(row.get(col, '')) for col in row.index}
                for candidate in CUIT_COLUMNS:
                    if candidate in row_data and row_data[candidate]:
                        return normalize_cuit(row_data[candidate])
                for value in row_data.values():
                    cuit_value = extract_cuit_from_text(value)
                    if cuit_value:
                        return cuit_value
        else:
            for _, row in frame.iterrows():
                for value in row.astype(str).tolist():
                    cuit_value = extract_cuit_from_text(value)
                    if cuit_value:
                        return cuit_value
    return ''


def build_record(row: pd.Series, metadata: Dict[str, str], company_map: Dict[str, str], company_name_map: Dict[str, str], bank_accounts: list[dict[str, str]] | None = None) -> Dict[str, object]:
    row_data = {col: clean_text(row.get(col, '')) for col in row.index}
    date = None
    for candidate in DATE_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            date = parse_date(row_data[candidate])
            if date:
                break
    account = ''
    for candidate in ACCOUNT_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            account = row_data[candidate]
            break
    if not account:
        for key, value in row_data.items():
            if value and ('cuenta' in key or 'cbu' in key):
                account = value
                break
    if not account and metadata.get('statement_account'):
        account = metadata.get('statement_account', '')
    cuit = ''
    for candidate in CUIT_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            cuit = normalize_cuit(row_data[candidate])
            break
    company = ''
    for candidate in COMPANY_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            company = row_data[candidate]
            break
    if not company and 'raw_empresa' in row_data and row_data['raw_empresa']:
        company = row_data['raw_empresa']
    if cuit:
        resolved_company = company_map.get(normalize_cuit(cuit), '')
        if resolved_company:
            company = resolved_company
    # El cruce por número de cuenta contra "DATOS BANCARIOS TODAS LAS
    # EMPRESAS.xlsx" es la fuente primaria de Banco/Empresa/CUIT: una columna
    # explícita de CUIT/Empresa en el archivo tiene prioridad, pero un CUIT
    # "adivinado" de texto libre (más abajo) o el nombre de archivo son
    # siempre el último recurso, porque no se puede confiar en que sean
    # correctos (ej. un número de comprobante que por casualidad tiene 11
    # dígitos y parece un CUIT).
    bank = metadata['bank']
    bank_match = None
    bank_currency = ''
    if account and bank_accounts:
        bank_match = find_bank_account(account, bank_accounts)
        if bank_match:
            if not cuit and bank_match.get('cuit'):
                cuit = bank_match['cuit']
            if not company and bank_match.get('empresa'):
                company = bank_match['empresa']
            if not bank and bank_match.get('bank'):
                bank = bank_match['bank']
            bank_currency = bank_match.get('currency', '') or ''
    bank = canonicalize_bank_name(bank)
    if not cuit and metadata.get('file_cuit'):
        cuit = metadata['file_cuit']
    if not cuit:
        for value in row_data.values():
            cuit_value = extract_cuit_from_text(value)
            if cuit_value:
                cuit = cuit_value
                break
    if not company:
        company = guess_company(metadata['source_file'], cuit, company_map)
    if company and not cuit:
        cuit = company_name_map.get(normalize_text(company), '')
    # Empresas.xlsx es el maestro de nombres de empresa: si ya tenemos CUIT,
    # su nombre pisa cualquier variante que haya llegado del cruce de cuenta
    # o del archivo (ej. "ALCO ROSARIO S.A." vs "ALCO ROSARIO SA"), para que
    # una misma empresa se muestre siempre igual sin importar el banco.
    if cuit:
        resolved_company = company_map.get(normalize_cuit(cuit), '')
        if resolved_company:
            company = resolved_company
    description = ''
    bank_key = (bank or '').strip().upper()
    if bank_key in BANK_DESCRIPTION_COLUMNS:
        parts = []
        for position in BANK_DESCRIPTION_COLUMNS[bank_key]:
            text = get_positional_text(row, position)
            if text:
                parts.append(text)
        description = ' | '.join(parts)
    if not description:
        for candidate in DESCRIPTION_COLUMNS:
            if candidate in row_data and row_data[candidate]:
                description = row_data[candidate]
                break
    debit = None
    for candidate in DEBIT_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            debit = parse_decimal(row_data[candidate])
            break
    credit = None
    for candidate in CREDIT_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            credit = parse_decimal(row_data[candidate])
            break
    amount = None
    for candidate in AMOUNT_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            amount = parse_decimal(row_data[candidate])
            break
    # El H/I posicional es un último recurso para extractos sin encabezados
    # de importe/débito/crédito reconocibles por nombre. Si alguno de esos ya
    # se identificó por nombre, no se lo debe pisar con una posición fija
    # (ej. en ICBC la columna I es "Sucursal Origen", no un importe).
    h_value = None
    i_value = None
    if amount is None and debit is None and credit is None:
        for candidate in H_COLUMNS:
            if candidate in row_data and row_data[candidate]:
                h_value = parse_decimal(row_data[candidate])
                break
        if h_value is None:
            h_text = get_positional_text(row, 7)
            if h_text:
                h_value = parse_decimal(h_text)
        for candidate in I_COLUMNS:
            if candidate in row_data and row_data[candidate]:
                i_value = parse_decimal(row_data[candidate])
                break
        if i_value is None:
            i_text = get_positional_text(row, 8)
            if i_text:
                i_value = parse_decimal(i_text)
    # Prefer explicit parsed amount when available; preserve its sign
    if amount is not None:
        if amount < 0:
            debit = abs(amount)
            credit = None
        else:
            credit = amount
            debit = None
    elif i_value is not None:
        amount = i_value
        if amount < 0:
            debit = abs(amount)
            credit = None
        else:
            credit = amount
            debit = None
    elif h_value is not None:
        amount = -abs(h_value)
        debit = abs(h_value)
        credit = None
    elif debit is not None or credit is not None:
        if debit is not None and credit is not None:
            amount = (credit or 0.0) - (debit or 0.0)
        elif debit is not None:
            # La mayoría de los bancos guardan el débito como magnitud
            # positiva (hay que negarla), pero algunos (ej. ICBC) ya lo
            # traen con el signo puesto; si ya es negativo no hay que
            # negarlo de nuevo. 'Debito' se muestra siempre como magnitud
            # positiva, consistente con el resto de los bancos.
            amount = debit if debit < 0 else -debit
            debit = abs(debit)
        else:
            amount = credit
    balance = None
    for candidate in BALANCE_COLUMNS:
        if candidate in row_data and row_data[candidate]:
            balance = parse_decimal(row_data[candidate])
            break
    # "DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx" asigna una moneda fija por
    # cuenta y es la fuente primaria (misma lógica que Banco/Empresa/CUIT):
    # un extracto puede no traer una columna de moneda confiable, pero la
    # cuenta ya tiene la moneda correcta registrada ahí.
    currency = canonicalize_currency(bank_currency) if bank_currency else ''
    if not currency:
        for candidate in CURRENCY_COLUMNS:
            if candidate in row_data and row_data[candidate]:
                currency = row_data[candidate]
                break
    if not currency:
        if '$' in metadata['source_file'] or 'cc $' in metadata['source_file'].lower():
            currency = 'ARS'
    bank = bank or 'Desconocido'
    record = {
        'Fecha': date,
        'Descripcion': description,
        'Debito': debit,
        'Credito': credit,
        'Monto': amount,
        'Saldo': balance,
        'Cuenta': account,
        'CUIT': cuit,
        'Moneda': currency,
        'Banco': bank,
        'Empresa': company,
        'SourceFile': metadata['source_file'],
        'CI': '',
        'SourceRow': metadata.get('source_row', ''),
    }
    record.update({f'RAW_{k}': v for k, v in row_data.items() if k not in record})
    return record


def _normalize_hash_token(token: str) -> str:
    token = token.strip()
    if not token:
        return token
    value = parse_decimal(token)
    if value is not None:
        return f'{value:.2f}'
    return token


def normalize_description_for_hash(descripcion: str) -> str:
    # La Descripción de algunos bancos (ej. ICBC) incluye códigos y montos
    # como texto (ver BANK_DESCRIPTION_COLUMNS). El mismo movimiento puede
    # venir formateado distinto entre dos exportaciones del mismo banco (ej.
    # sucursal "0506" vs "506", débito "-11550,00" vs "-11550"); normalizar
    # cada segmento numérico evita que esa diferencia cosmética haga que un
    # movimiento ya importado se duplique.
    if not descripcion:
        return ''
    parts = str(descripcion).split('|')
    return '|'.join(_normalize_hash_token(part) for part in parts)


def hash_record(record: Dict[str, object]) -> str:
    key = '|'.join(str(record.get(field, '') or '') for field in [
        'Banco', 'Cuenta', 'Fecha', 'Monto', 'Saldo', 'CUIT'
    ])
    key += '|' + normalize_description_for_hash(record.get('Descripcion', ''))
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def is_empty_record(record: Dict[str, object]) -> bool:
    # Filas de metadata o líneas sueltas (ej. un timestamp final, una fila en
    # blanco al final de la hoja) que no traen ni descripción ni importe ni
    # saldo: no son un movimiento real, no deben pasar al registro unificado.
    return not record.get('Descripcion') and record.get('Monto') is None and record.get('Saldo') is None


def load_existing_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(path, dtype=str, engine='openpyxl')
    except PermissionError:
        print(f'Warning: no se pudo abrir {path.name} porque está bloqueado o abierto en Excel. Se continuará sin preservar CI.')
        return pd.DataFrame()
    except Exception as exc:
        print(f'Warning: no se pudo leer {path.name}: {exc}')
        return pd.DataFrame()
    df.columns = [normalize_header(col) for col in df.columns]
    canonical = {
        'ci': 'CI',
        'cuit': 'CUIT',
        'sourcefile': 'SourceFile',
        'sourcerow': 'SourceRow',
        'recordhash': 'RecordHash',
        'empresa': 'Empresa',
        'banco': 'Banco',
        'moneda': 'Moneda',
        'fecha': 'Fecha',
        'descripcion': 'Descripcion',
        'saldo': 'Saldo',
        'monto': 'Monto',
        'debito': 'Debito',
        'credito': 'Credito',
    }
    df = df.rename(columns={col: canonical.get(col, col) for col in df.columns})
    if 'CI' not in df.columns:
        df['CI'] = ''
    if 'CUIT' not in df.columns:
        df['CUIT'] = ''
    if 'RecordHash' not in df.columns and 'recordhash' in df.columns:
        df = df.rename(columns={'recordhash': 'RecordHash'})
    return df


def merge_existing_ci(master: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    if existing.empty or 'CI' not in existing.columns:
        return master
    existing = existing.copy()
    existing['record_hash'] = existing.apply(lambda row: hash_record({
        'Banco': row.get('Banco', ''),
        'Cuenta': row.get('Cuenta', ''),
        'Fecha': row.get('Fecha', ''),
        'Monto': row.get('Monto', ''),
        'Descripcion': row.get('Descripcion', ''),
        'Saldo': row.get('Saldo', ''),
        'CUIT': row.get('CUIT', ''),
    }), axis=1)
    master['record_hash'] = master.apply(lambda row: hash_record({
        'Banco': row.get('Banco', ''),
        'Cuenta': row.get('Cuenta', ''),
        'Fecha': row.get('Fecha', ''),
        'Monto': row.get('Monto', ''),
        'Descripcion': row.get('Descripcion', ''),
        'Saldo': row.get('Saldo', ''),
        'CUIT': row.get('CUIT', ''),
    }), axis=1)
    hash_to_ci = existing.set_index('record_hash')['CI'].to_dict()
    master['CI'] = master.apply(lambda row: hash_to_ci.get(row['record_hash'], row.get('CI', '')), axis=1)
    master = master.drop(columns=['record_hash'])
    return master


def pending_statement_files(source_dir: Path) -> List[Path]:
    """Archivos que /unify va a procesar si se lo ejecuta ahora.

    Sirve para mostrarlos antes de unificar: la carpeta puede tener restos de
    subidas anteriores (propias o de otra persona), y unificar los procesa
    todos, no solo los recién subidos.
    """
    if not source_dir.exists():
        return []
    return [p for p in sorted(source_dir.iterdir())
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and p.name != MASTER_FILE.name]


def gather_statements(source_dir: Path, company_map: Dict[str, str], company_name_map: Dict[str, str],
                       bank_accounts: list[dict[str, str]] | None = None,
                       errores: List[tuple] | None = None) -> pd.DataFrame:
    rows = []
    for path in pending_statement_files(source_dir):
        try:
            if path.suffix.lower() == '.csv':
                df, raw_df = read_csv_file(path, return_raw=True)
            else:
                df, raw_df = read_excel_file(path, return_raw=True)
        except Exception as exc:
            # Además de avisar por consola se acumula el error, para poder
            # mostrarle al usuario qué archivo quedó sin procesar: antes
            # se descartaba en silencio y el archivo quedaba en la carpeta
            # reintentándose en cada unificación.
            print(f'Warning: no se pudo leer {path.name}: {exc}')
            if errores is not None:
                errores.append((path.name, str(exc)))
            continue
        bank = guess_bank(path.name)
        file_cuit = extract_file_cuit(df, raw_df)
        statement_account = resolve_statement_account(raw_df, path.name, bank_accounts)
        metadata = {'source_file': path.name, 'bank': bank, 'file_cuit': file_cuit, 'statement_account': statement_account}
        for idx, row in df.iterrows():
            metadata['source_row'] = idx + 1
            record = build_record(row, metadata, company_map, company_name_map, bank_accounts)
            if is_empty_record(record):
                continue
            record['RecordHash'] = hash_record(record)
            rows.append(record)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df['Fecha'] = parse_fecha_column(df['Fecha'])
    df['CI'] = df['CI'].astype(str)
    if 'CUIT' in df.columns:
        df['CUIT'] = df['CUIT'].fillna('').astype(str)
    else:
        df['CUIT'] = ''
    return df


def process_statement_file(path: Path, company_map: Dict[str, str], company_name_map: Dict[str, str], bank_accounts: list[dict[str, str]] | None = None) -> pd.DataFrame:
    if path.suffix.lower() == '.csv':
        df, raw_df = read_csv_file(path, return_raw=True)
    else:
        df, raw_df = read_excel_file(path, return_raw=True)
    bank = guess_bank(path.name)
    file_cuit = extract_file_cuit(df, raw_df)
    statement_account = resolve_statement_account(raw_df, path.name, bank_accounts)
    metadata = {'source_file': path.name, 'bank': bank, 'file_cuit': file_cuit, 'statement_account': statement_account}
    rows = []
    for idx, row in df.iterrows():
        metadata['source_row'] = idx + 1
        record = build_record(row, metadata, company_map, company_name_map, bank_accounts)
        if is_empty_record(record):
            continue
        record['RecordHash'] = hash_record(record)
        rows.append(record)
    if not rows:
        return pd.DataFrame()
    output = pd.DataFrame(rows)
    output['Fecha'] = parse_fecha_column(output['Fecha'])
    output['CI'] = output['CI'].astype(str)
    output['CUIT'] = output['CUIT'].fillna('').astype(str)
    return output


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values(['Fecha', 'SourceFile', 'SourceRow'], ascending=[False, True, True])
    df = df.drop_duplicates(subset=['RecordHash'], keep='first')
    df = df.drop(columns=['RecordHash'])
    return df


def apply_filters(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if args.empresa:
        df = df[df['Empresa'].str.contains(args.empresa, case=False, na=False)]
    if args.cuit:
        value = normalize_cuit(args.cuit)
        df = df[df['CUIT'].str.contains(value, na=False)]
    if args.cuenta:
        df = df[df['Cuenta'].str.contains(args.cuenta, case=False, na=False)]
    if args.moneda:
        df = df[df['Moneda'].str.contains(args.moneda, case=False, na=False)]
    if args.banco:
        df = df[df['Banco'].str.contains(args.banco, case=False, na=False)]
    return df


def _prepare_master_dataframe(df: pd.DataFrame, fields: List[str] | None = None) -> pd.DataFrame:
    df = df.copy()
    df['Fecha'] = parse_fecha_column(df['Fecha'])
    if 'CI' in df.columns:
        df['CI'] = df['CI'].fillna('').astype(str).replace('nan', '')
    if 'CUIT' in df.columns:
        df['CUIT'] = df['CUIT'].fillna('').astype(str).replace('nan', '')
    if fields is None:
        fields = ['Fecha', 'Empresa', 'CUIT', 'Cuenta', 'Moneda', 'Banco', 'Descripcion', 'Debito', 'Credito', 'Monto', 'Saldo', 'CI', 'SourceFile', 'SourceRow', 'RecordHash']
        df = df[[f for f in fields if f in df.columns] + [c for c in df.columns if c.startswith('RAW_')]]
    else:
        df = df[[f for f in fields if f in df.columns]]
    df = df.sort_values('Fecha', ascending=False, na_position='last')
    df['Fecha'] = df['Fecha'].dt.strftime('%d/%m/%Y').fillna('')
    return df


def _write_master_sheet(df: pd.DataFrame, target, header_note: str | None = None) -> None:
    # 'target' puede ser una ruta o un buffer en memoria: la descarga web no
    # escribe a disco (ver build_master_bytes) y el CLI sí.
    with pd.ExcelWriter(target, engine='openpyxl', datetime_format='DD/MM/YYYY', date_format='DD/MM/YYYY') as writer:
        base_rows = 4 if LOGO_FILE.exists() else 0
        startrow = base_rows + (1 if header_note else 0)
        df.to_excel(writer, index=False, engine='openpyxl', startrow=startrow)
        worksheet = writer.sheets[writer.book.sheetnames[0]]
        if LOGO_FILE.exists():
            from openpyxl.drawing.image import Image as XLImage
            image = XLImage(str(LOGO_FILE))
            image.width = 200
            image.height = 56
            worksheet.add_image(image, 'A1')
        if header_note:
            worksheet.cell(row=base_rows + 1, column=1, value=header_note)


def build_master_bytes(df: pd.DataFrame, fields: List[str] | None = None,
                        header_note: str | None = None) -> bytes:
    """Genera el .xlsx unificado en memoria, sin tocar el disco.

    La descarga web usa esto en vez de create_master_file: un archivo temporal
    con nombre fijo y compartido hace que dos descargas simultáneas se pisen, y
    como el contenido depende del rol (la cajera no ve Saldo) y de los filtros
    de quien descarga, el pisón se lleva datos de un usuario al otro.
    """
    buffer = io.BytesIO()
    _write_master_sheet(_prepare_master_dataframe(df, fields), buffer, header_note)
    return buffer.getvalue()


def create_master_file(df: pd.DataFrame, output_path: Path, fields: List[str] | None = None,
                        header_note: str | None = None) -> Path:
    if df.empty:
        print('No hay registros para guardar.')
        return output_path
    prepared = _prepare_master_dataframe(df, fields)

    def save_to(path: Path) -> None:
        _write_master_sheet(prepared, path, header_note)

    try:
        save_to(output_path)
        print(f'Se guardó el archivo unificado en {output_path.name}')
        return output_path
    except PermissionError:
        counter = 1
        while True:
            alt_name = f"{output_path.stem}_nuevo{'' if counter == 1 else f'_{counter}'}{output_path.suffix}"
            alt_path = output_path.with_name(alt_name)
            try:
                save_to(alt_path)
                print(f'Advertencia: no se pudo escribir {output_path.name} porque está abierto. Se guardó como {alt_path.name}')
                return alt_path
            except PermissionError:
                counter += 1
                if counter > 10:
                    raise


def load_companies(path: Path) -> tuple[Dict[str, str], Dict[str, str]]:
    if not path.exists():
        return {}, {}
    try:
        df = pd.read_excel(path, dtype=str, engine='openpyxl')
    except PermissionError:
        print(f'Warning: no se pudo abrir {path.name} porque está bloqueado o abierto en Excel.')
        return {}, {}
    except Exception as exc:
        print(f'Warning: no se pudo leer {path.name}: {exc}')
        return {}, {}
    df.columns = [normalize_header(col) for col in df.columns]
    if 'cuit' not in df.columns or 'empresa' not in df.columns:
        return {}, {}
    df['cuit'] = df['cuit'].astype(str).apply(normalize_cuit)
    df['empresa'] = df['empresa'].astype(str).apply(clean_text)
    company_map = df.set_index('cuit')['empresa'].to_dict()
    company_name_map = {}
    for cuit, empresa in company_map.items():
        key = normalize_text(empresa)
        if key:
            company_name_map[key] = cuit
    return company_map, company_name_map


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Conciliador de extractos bancarios.')
    parser.add_argument('--source-dir', default='.', help='Carpeta donde están los archivos de extractos y Empresas.xlsx')
    parser.add_argument('--output', default=MASTER_FILE.name, help='Archivo de salida unificado')
    parser.add_argument('--scan', action='store_true', help='Leer y unificar todos los extractos disponibles')
    parser.add_argument('--empresa', help='Filtrar por nombre de empresa')
    parser.add_argument('--cuit', help='Filtrar por CUIT')
    parser.add_argument('--cuenta', help='Filtrar por número de cuenta')
    parser.add_argument('--moneda', help='Filtrar por moneda')
    parser.add_argument('--banco', help='Filtrar por banco')
    parser.add_argument('--preserve-ci', action='store_true', help='Preservar valores CI existentes en el maestro anterior')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_dir = Path(args.source_dir)
    company_map, company_name_map = load_companies(source_dir / EMPRESAS_FILE.name)
    existing_master = load_existing_master(source_dir / MASTER_FILE.name) if args.preserve_ci else pd.DataFrame()
    if args.scan:
        statements = gather_statements(source_dir, company_map, company_name_map)
        if statements.empty:
            print('No se encontraron registros nuevos en los extractos.')
            return
        merged = statements
        if not existing_master.empty:
            merged = pd.concat([existing_master, statements], ignore_index=True, sort=False)
        merged = deduplicate(merged)
        if not existing_master.empty:
            merged = merge_existing_ci(merged, existing_master)
        create_master_file(merged, source_dir / args.output)
        filtered = apply_filters(merged, args)
    else:
        merged = existing_master if not existing_master.empty else pd.DataFrame()
        filtered = apply_filters(merged, args)
    if not filtered.empty and args.output != MASTER_FILE.name:
        create_master_file(filtered, source_dir / args.output)
    print(f'Registros totales en la vista: {len(filtered)}')


if __name__ == '__main__':
    main()
