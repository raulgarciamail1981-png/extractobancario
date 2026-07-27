import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import (Column, Engine, Float, Integer, MetaData, Table, Text, bindparam, create_engine,
                         event, text)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / 'conciliador.db'

MOVEMENT_COLUMNS = [
    'Fecha', 'Empresa', 'CUIT', 'Cuenta', 'Moneda', 'Banco', 'Descripcion',
    'Debito', 'Credito', 'Monto', 'Saldo', 'CI', 'SourceFile', 'SourceRow',
]

metadata = MetaData()

movements_table = Table(
    'movements', metadata,
    Column('RecordHash', Text, primary_key=True),
    Column('Fecha', Text),
    Column('Empresa', Text),
    Column('CUIT', Text),
    Column('Cuenta', Text),
    Column('Moneda', Text),
    Column('Banco', Text),
    Column('Descripcion', Text),
    Column('Debito', Float),
    Column('Credito', Float),
    Column('Monto', Float),
    Column('Saldo', Float),
    Column('CI', Text),
    Column('SourceFile', Text),
    Column('SourceRow', Integer),
    Column('Raw', Text),
)

audit_log_table = Table(
    'audit_log', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('ts', Text),
    Column('username', Text),
    Column('action', Text),
    Column('detail', Text),
    Column('record_hash', Text),
)

# Cache de engines por URL: create_engine() abre un pool de conexiones, no
# tiene sentido recrearlo en cada llamada (sobre todo para Postgres).
_ENGINES: dict[str, Engine] = {}


def _engine_url(db_path) -> str:
    # Acepta tanto lo que siempre recibió esta función (una Path/string a un
    # archivo .db de SQLite) como una URL completa de SQLAlchemy
    # (postgresql+psycopg2://...), para poder apuntar a Postgres en
    # producción sin cambiar ningún call site.
    text_value = str(db_path)
    if '://' in text_value:
        return text_value
    return f'sqlite:///{text_value}'


def get_engine(db_path: Path = DEFAULT_DB_PATH) -> Engine:
    url = _engine_url(db_path)
    engine = _ENGINES.get(url)
    if engine is not None:
        return engine
    engine = create_engine(url, future=True)
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=5000')
            cursor.close()
    metadata.create_all(engine)
    _ENGINES[url] = engine
    return engine


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    get_engine(db_path)


def _normalize_fecha(value: object) -> str:
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        try:
            if pd.isna(value):
                return ''
        except TypeError:
            pass
        return value.strftime('%Y-%m-%d')
    text_value = str(value).strip()
    if not text_value or text_value.lower() in ('nat', 'nan'):
        return ''
    parsed = pd.to_datetime(text_value, dayfirst=True, errors='coerce')
    if pd.isna(parsed):
        return text_value
    return parsed.strftime('%Y-%m-%d')


def _row_to_db_params(row: dict) -> dict:
    raw = {k[4:]: v for k, v in row.items() if k.startswith('RAW_')}
    return {
        'RecordHash': row.get('RecordHash', ''),
        'Fecha': _normalize_fecha(row.get('Fecha')),
        'Empresa': str(row.get('Empresa', '') or ''),
        'CUIT': str(row.get('CUIT', '') or ''),
        'Cuenta': str(row.get('Cuenta', '') or ''),
        'Moneda': str(row.get('Moneda', '') or ''),
        'Banco': str(row.get('Banco', '') or ''),
        'Descripcion': str(row.get('Descripcion', '') or ''),
        'Debito': row.get('Debito') if row.get('Debito') not in ('', None) else None,
        'Credito': row.get('Credito') if row.get('Credito') not in ('', None) else None,
        'Monto': row.get('Monto') if row.get('Monto') not in ('', None) else None,
        'Saldo': row.get('Saldo') if row.get('Saldo') not in ('', None) else None,
        'CI': str(row.get('CI', '') or ''),
        'SourceFile': str(row.get('SourceFile', '') or ''),
        'SourceRow': int(row.get('SourceRow') or 0),
        'Raw': json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
    }


def get_existing_hashes(hashes: list[str], db_path: Path = DEFAULT_DB_PATH) -> set[str]:
    engine = get_engine(db_path)
    unique_hashes = list(dict.fromkeys(h for h in hashes if h))
    existing: set[str] = set()
    if not unique_hashes:
        return existing
    query = text('SELECT "RecordHash" FROM movements WHERE "RecordHash" IN :hashes').bindparams(
        bindparam('hashes', expanding=True)
    )
    with engine.connect() as conn:
        chunk_size = 500
        for i in range(0, len(unique_hashes), chunk_size):
            chunk = unique_hashes[i:i + chunk_size]
            rows = conn.execute(query, {'hashes': chunk}).all()
            existing.update(row[0] for row in rows)
    return existing


def _extract_referencia(raw_json: str | None) -> str:
    if not raw_json:
        return ''
    try:
        return str(json.loads(raw_json).get('referencia', '') or '').strip()
    except (TypeError, ValueError):
        return ''


_RECONCILE_MATCH_SQL = text('''
    SELECT "RecordHash", "Raw" FROM movements
    WHERE "Banco" = :banco AND "Cuenta" = :cuenta AND "Fecha" = :fecha AND "CUIT" = :cuit
      AND ABS("Monto" - :monto) < 0.005 AND "RecordHash" != :exclude_hash
''')


def _find_reconcilable_match(conn, banco: str, cuenta: str, fecha: str,
                              cuit: str, monto: float | None, raw_json: str | None,
                              exclude_hash: str) -> str | None:
    # Santander exporta primero movimientos "a confirmar" y además a veces
    # reclasifica la Descripcion de un movimiento ya confirmado entre una
    # extracción y la siguiente (cambia Suc. Origen/Cod. Operativo/Concepto).
    # En ambos casos es el mismo movimiento real: hay que actualizar la fila
    # existente, no agregar una nueva. No alcanza con Fecha+Monto+Cuenta
    # (varias transferencias distintas del mismo día pueden coincidir en
    # importe, y Saldo no siempre viene informado); el número de
    # "Referencia" que Santander asigna a cada movimiento sí se mantiene
    # estable entre reclasificaciones y es lo que realmente identifica al
    # movimiento sin ambigüedad.
    if not banco or banco.strip().lower() != 'santander':
        return None
    if not fecha or monto is None:
        return None
    referencia = _extract_referencia(raw_json)
    if not referencia:
        return None
    rows = conn.execute(_RECONCILE_MATCH_SQL, {
        'banco': banco, 'cuenta': cuenta, 'fecha': fecha, 'cuit': cuit,
        'monto': monto, 'exclude_hash': exclude_hash,
    }).all()
    for record_hash, raw in rows:
        if _extract_referencia(raw) == referencia:
            return record_hash
    return None


_INSERT_SQL = text('''
    INSERT INTO movements (
        "RecordHash", "Fecha", "Empresa", "CUIT", "Cuenta", "Moneda", "Banco", "Descripcion",
        "Debito", "Credito", "Monto", "Saldo", "CI", "SourceFile", "SourceRow", "Raw"
    ) VALUES (
        :RecordHash, :Fecha, :Empresa, :CUIT, :Cuenta, :Moneda, :Banco, :Descripcion,
        :Debito, :Credito, :Monto, :Saldo, :CI, :SourceFile, :SourceRow, :Raw
    )
    ON CONFLICT("RecordHash") DO UPDATE SET
        "Fecha"=excluded."Fecha", "Empresa"=excluded."Empresa", "CUIT"=excluded."CUIT",
        "Cuenta"=excluded."Cuenta", "Moneda"=excluded."Moneda", "Banco"=excluded."Banco",
        "Descripcion"=excluded."Descripcion", "Debito"=excluded."Debito",
        "Credito"=excluded."Credito", "Monto"=excluded."Monto", "Saldo"=excluded."Saldo",
        "CI" = CASE WHEN movements."CI" IS NOT NULL AND movements."CI" != ''
                  THEN movements."CI" ELSE excluded."CI" END,
        "SourceFile"=excluded."SourceFile", "SourceRow"=excluded."SourceRow", "Raw"=excluded."Raw"
''')

_RECONCILE_UPDATE_SQL = text('''
    UPDATE movements SET
        "RecordHash" = :new_hash, "Descripcion" = :descripcion, "Saldo" = COALESCE(:saldo, "Saldo"),
        "SourceFile" = :source_file, "SourceRow" = :source_row, "Raw" = :raw
    WHERE "RecordHash" = :match_hash
''')


def upsert_movements(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> tuple[int, int]:
    """Inserta/actualiza movimientos. Devuelve (total procesado, nuevos agregados)."""
    if df.empty:
        return 0, 0
    engine = get_engine(db_path)
    records = df.to_dict(orient='records')
    values = [_row_to_db_params(record) for record in records if record.get('RecordHash')]
    if not values:
        return 0, 0
    incoming_hashes = [value['RecordHash'] for value in values]
    existing_hashes = get_existing_hashes(incoming_hashes, db_path)
    new_count = 0
    with engine.begin() as conn:
        for value in values:
            record_hash = value['RecordHash']
            if record_hash in existing_hashes:
                conn.execute(_INSERT_SQL, value)
                continue
            match_hash = _find_reconcilable_match(
                conn, value['Banco'], value['Cuenta'], value['Fecha'], value['CUIT'],
                value['Monto'], value['Raw'], record_hash,
            )
            if match_hash:
                conn.execute(_RECONCILE_UPDATE_SQL, {
                    'new_hash': record_hash, 'descripcion': value['Descripcion'], 'saldo': value['Saldo'],
                    'source_file': value['SourceFile'], 'source_row': value['SourceRow'], 'raw': value['Raw'],
                    'match_hash': match_hash,
                })
                continue
            conn.execute(_INSERT_SQL, value)
            existing_hashes.add(record_hash)
            new_count += 1
    return len(values), new_count


def load_movements(db_path: Path = DEFAULT_DB_PATH) -> pd.DataFrame:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        df = pd.read_sql_query('SELECT * FROM movements', conn)
    if df.empty:
        return pd.DataFrame(columns=MOVEMENT_COLUMNS + ['RecordHash'])
    raw_series = df.pop('Raw')
    raw_frames = []
    for value in raw_series:
        if value:
            raw_frames.append({f'RAW_{k}': v for k, v in json.loads(value).items()})
        else:
            raw_frames.append({})
    if any(raw_frames):
        df = pd.concat([df, pd.DataFrame(raw_frames)], axis=1)
    return df


def clear_movements(db_path: Path = DEFAULT_DB_PATH) -> int:
    engine = get_engine(db_path)
    with engine.begin() as conn:
        result = conn.execute(text('DELETE FROM movements'))
        return result.rowcount


def update_ci(record_hash: str, ci_value: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    engine = get_engine(db_path)
    with engine.begin() as conn:
        result = conn.execute(
            text('UPDATE movements SET "CI" = :ci WHERE "RecordHash" = :record_hash'),
            {'ci': ci_value, 'record_hash': record_hash},
        )
        return result.rowcount > 0


def log_action(username: str, action: str, detail: dict | None = None, record_hash: str | None = None,
                db_path: Path = DEFAULT_DB_PATH) -> None:
    engine = get_engine(db_path)
    ts = datetime.now(timezone.utc).isoformat()
    detail_json = json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None
    with engine.begin() as conn:
        conn.execute(
            text('INSERT INTO audit_log ("ts", "username", "action", "detail", "record_hash") '
                 'VALUES (:ts, :username, :action, :detail, :record_hash)'),
            {'ts': ts, 'username': username, 'action': action, 'detail': detail_json, 'record_hash': record_hash},
        )


def _audit_row_to_dict(row) -> dict:
    entry = dict(zip(('ts', 'username', 'action', 'detail', 'record_hash'), row))
    if entry.get('detail'):
        try:
            entry['detail'] = json.loads(entry['detail'])
        except (TypeError, ValueError):
            pass
    return entry


def load_audit_log(limit: int = 200, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT "ts", "username", "action", "detail", "record_hash" FROM audit_log '
                 'ORDER BY "id" DESC LIMIT :limit'),
            {'limit': limit},
        ).all()
    return [_audit_row_to_dict(row) for row in rows]


def get_last_action_entry(action: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        row = conn.execute(
            text('SELECT "ts", "username", "action", "detail", "record_hash" FROM audit_log '
                 'WHERE "action" = :action ORDER BY "id" DESC LIMIT 1'),
            {'action': action},
        ).first()
    if not row:
        return None
    return _audit_row_to_dict(row)


def migrate_excel_if_needed(excel_path: Path, db_path: Path = DEFAULT_DB_PATH) -> None:
    # Bootstrap de una sola vez desde el maestro Excel legado: solo tiene
    # sentido para un archivo SQLite nuevo (no hay .xlsx legado que importar
    # a una Postgres recién creada; esa migración la hace
    # migrate_sqlite_to_postgres.py explícitamente).
    if '://' in str(db_path):
        init_db(db_path)
        return
    if Path(db_path).exists() or not excel_path.exists():
        init_db(db_path)
        return
    from conciliador import hash_record, load_existing_master

    df = load_existing_master(excel_path)
    init_db(db_path)
    if df.empty:
        return
    if 'RecordHash' not in df.columns:
        df = df.copy()
        df['RecordHash'] = df.apply(lambda row: hash_record(row.to_dict()), axis=1)
    upsert_movements(df, db_path)
