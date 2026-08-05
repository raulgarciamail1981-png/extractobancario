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

# Hasta dónde vio cada persona cada tipo de aviso ("extractos", "ci"). Un
# aviso se muestra mientras haya movimiento posterior a este ts. Va en tabla
# aparte y no en el audit_log para no ensuciar la auditoría con clics.
notificaciones_table = Table(
    'notificaciones_vistas', metadata,
    Column('username', Text, primary_key=True),
    Column('tipo', Text, primary_key=True),
    Column('ts', Text),
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
    # Los avisos consultan el audit_log por acción y fecha en cada pantalla, y
    # esa tabla solo crece. Va aparte de create_all() porque para una tabla que
    # ya existe (el servidor) create_all no agrega los índices que falten.
    with engine.begin() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_audit_log_action_ts '
                          'ON audit_log ("action", "ts")'))
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


# Dato que identifica a un movimiento y que NO cambia entre una descarga y la
# siguiente, por banco. La clave es un pedazo del nombre del banco (por
# substring, así el nombre visible puede cambiar sin romper esto) y el valor,
# el campo de "Raw" donde quedó guardado al leer el extracto.
RECONCILE_KEY_FIELDS = {
    'santander': 'referencia',
    'santa fe': 'nro comprobante',
}


def _extract_raw_field(raw_json: str | None, field: str) -> str:
    if not raw_json:
        return ''
    try:
        return str(json.loads(raw_json).get(field, '') or '').strip()
    except (TypeError, ValueError):
        return ''


def _extract_referencia(raw_json: str | None) -> str:
    return _extract_raw_field(raw_json, 'referencia')


_RECONCILE_MATCH_SQL = text('''
    SELECT "RecordHash", "Raw" FROM movements
    WHERE "Banco" = :banco AND "Cuenta" = :cuenta AND "Fecha" = :fecha AND "CUIT" = :cuit
      AND ABS("Monto" - :monto) < 0.005 AND "RecordHash" != :exclude_hash
''')


def _find_reconcilable_match(conn, banco: str, cuenta: str, fecha: str,
                              cuit: str, monto: float | None, raw_json: str | None,
                              exclude_hash: str) -> str | None:
    # Hay bancos que devuelven el mismo movimiento con algún dato distinto
    # entre una descarga y la siguiente. Como esos datos forman parte del
    # RecordHash, el movimiento entra de nuevo y queda duplicado. Cuando eso
    # pasa hay que actualizar la fila existente, no agregar una nueva:
    #
    # - Santander exporta primero movimientos "a confirmar" y a veces
    #   reclasifica la Descripcion de uno ya confirmado (cambia Suc. Origen,
    #   Cod. Operativo o Concepto).
    # - Santa Fe recalcula el Saldo: en "Movimientos del día" la lista se
    #   reordena a medida que entran movimientos nuevos, y el saldo acumulado
    #   de los que ya estaban cambia. El mismo "CRED VS 636478" figuraba con
    #   saldo 36.867.133,87 a las 12:37 y 55.867.133,87 a las 14:21.
    #
    # No alcanza con Fecha+Monto+Cuenta: varias transferencias distintas del
    # mismo día pueden coincidir en importe. Hace falta el dato estable que
    # cada banco le asigna al movimiento (la "Referencia" de Santander, el
    # "Nro. comprobante" de Santa Fe), que es lo que lo identifica sin
    # ambigüedad.
    # La búsqueda del banco es por substring y no por igualdad: el nombre
    # visible puede cambiar (hoy es "Santander RIO") y esto tiene que seguir
    # aplicándose igual, o vuelven a entrar los duplicados.
    if not banco:
        return None
    banco_norm = banco.strip().lower()
    field = next((campo for patron, campo in RECONCILE_KEY_FIELDS.items() if patron in banco_norm), None)
    if field is None:
        return None
    if not fecha or monto is None:
        return None
    clave = _extract_raw_field(raw_json, field)
    if not clave:
        return None
    rows = conn.execute(_RECONCILE_MATCH_SQL, {
        'banco': banco, 'cuenta': cuenta, 'fecha': fecha, 'cuit': cuit,
        'monto': monto, 'exclude_hash': exclude_hash,
    }).all()
    for record_hash, raw in rows:
        if _extract_raw_field(raw, field) == clave:
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


def resumen_por_archivo(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Qué aportó cada extracto ya unificado, para poder revisarlo y sacarlo.

    Se agrupa por archivo y empresa y se junta en Python: `string_agg` y
    `COUNT(...) FILTER` no están en todos los motores, y esto corre igual
    sobre SQLite (tests) que sobre Postgres (servidor).
    """
    engine = get_engine(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT "SourceFile", "Empresa", COUNT(*) AS filas, '
                 'MIN("Fecha") AS desde, MAX("Fecha") AS hasta, '
                 "SUM(CASE WHEN COALESCE(\"CI\", '') <> '' THEN 1 ELSE 0 END) AS con_ci "
                 'FROM movements GROUP BY "SourceFile", "Empresa"'),
        ).all()
    por_archivo: dict[str, dict] = {}
    for source_file, empresa, filas, desde, hasta, con_ci in rows:
        nombre = source_file or ''
        item = por_archivo.setdefault(nombre, {
            'archivo': nombre, 'filas': 0, 'desde': '', 'hasta': '',
            'empresas': set(), 'con_ci': 0,
        })
        item['filas'] += int(filas or 0)
        item['con_ci'] += int(con_ci or 0)
        if empresa:
            item['empresas'].add(str(empresa))
        # Las fechas se guardan como AAAA-MM-DD, así que ordenan como texto.
        if desde and (not item['desde'] or desde < item['desde']):
            item['desde'] = desde
        if hasta and (not item['hasta'] or hasta > item['hasta']):
            item['hasta'] = hasta
    resultado = []
    for item in por_archivo.values():
        item['empresas'] = sorted(item['empresas'])
        resultado.append(item)
    resultado.sort(key=lambda item: (item['hasta'], item['archivo']), reverse=True)
    return resultado


def posibles_duplicados(db_path: Path = DEFAULT_DB_PATH, limite: int = 200) -> list[dict]:
    """Movimientos que parecen el mismo cargado dos veces.

    La huella del problema es siempre igual: mismo banco, cuenta, fecha, CUIT e
    importe, pero distinto Saldo o distinta Descripción. Como esos dos campos
    entran en el RecordHash, el movimiento no se reconoce y entra de nuevo.

    Sirve para detectar con datos si un banco necesita reconciliación, en vez
    de activarla a ciegas: para reconciliar hace falta un identificador por
    movimiento, y hay bancos (Macro, Municipal) donde el comprobante es de la
    operación entera y usarlo borraría movimientos buenos.

    Son "posibles" y no "seguros" a propósito: dos transferencias distintas del
    mismo día por el mismo importe también caen acá y son legítimas. Por eso se
    devuelve el grupo entero, para poder compararlos antes de borrar nada.
    """
    engine = get_engine(db_path)
    columnas = ['RecordHash', 'Fecha', 'Empresa', 'CUIT', 'Cuenta', 'Banco',
                'Descripcion', 'Monto', 'Saldo', 'CI', 'SourceFile']
    seleccion = ', '.join(f'"{col}"' for col in columnas)
    with engine.connect() as conn:
        filas = conn.execute(text(f'SELECT {seleccion} FROM movements')).all()

    grupos: dict[tuple, list[dict]] = {}
    for fila in filas:
        item = dict(zip(columnas, fila))
        monto = None if item['Monto'] is None else round(float(item['Monto']), 2)
        clave = (item['Banco'] or '', item['Cuenta'] or '', item['Fecha'] or '',
                 item['CUIT'] or '', monto)
        grupos.setdefault(clave, []).append(item)

    resultado = []
    for (banco, cuenta, fecha, cuit, monto), movimientos in grupos.items():
        if len(movimientos) < 2:
            continue
        # La marca va por movimiento y no por grupo: en un mismo grupo puede
        # haber un duplicado real y además un movimiento legítimo que coincide
        # en fecha e importe de casualidad. Pasó de verdad con Santa Fe: el
        # "CRED VS 636478" repetido convivía con un "DEP EFEC" de 3.000.000
        # que era un depósito distinto. Los que repiten descripción dentro del
        # grupo son los que casi con seguridad sobran.
        veces = {}
        for movimiento in movimientos:
            veces[movimiento['Descripcion']] = veces.get(movimiento['Descripcion'], 0) + 1
        for movimiento in movimientos:
            movimiento['descripcion_repetida'] = veces[movimiento['Descripcion']] > 1
        resultado.append({
            'banco': banco, 'cuenta': cuenta, 'fecha': fecha, 'cuit': cuit, 'monto': monto,
            'empresa': movimientos[0]['Empresa'],
            'tiene_repetidos': any(cantidad > 1 for cantidad in veces.values()),
            'movimientos': sorted(movimientos, key=lambda m: (m['Saldo'] is None, m['Saldo'] or 0)),
        })
    resultado.sort(key=lambda grupo: (grupo['fecha'], grupo['banco'], grupo['cuenta']), reverse=True)
    return resultado[:limite]


def get_movement(record_hash: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    engine = get_engine(db_path)
    columnas = ['Fecha', 'Empresa', 'CUIT', 'Cuenta', 'Banco', 'Descripcion',
                'Monto', 'Saldo', 'CI', 'SourceFile']
    seleccion = ', '.join(f'"{col}"' for col in columnas)
    with engine.connect() as conn:
        row = conn.execute(
            text(f'SELECT {seleccion} FROM movements WHERE "RecordHash" = :record_hash'),
            {'record_hash': record_hash},
        ).first()
    return dict(zip(columnas, row)) if row else None


def delete_movement(record_hash: str, db_path: Path = DEFAULT_DB_PATH) -> int:
    engine = get_engine(db_path)
    with engine.begin() as conn:
        result = conn.execute(
            text('DELETE FROM movements WHERE "RecordHash" = :record_hash'),
            {'record_hash': record_hash},
        )
        return result.rowcount


def delete_movements_by_source(source_file: str, db_path: Path = DEFAULT_DB_PATH) -> int:
    engine = get_engine(db_path)
    with engine.begin() as conn:
        result = conn.execute(
            text('DELETE FROM movements WHERE "SourceFile" = :source_file'),
            {'source_file': source_file},
        )
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


def get_notificacion_vista(username: str, tipo: str, db_path: Path = DEFAULT_DB_PATH) -> str:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        row = conn.execute(
            text('SELECT "ts" FROM notificaciones_vistas WHERE "username" = :username AND "tipo" = :tipo'),
            {'username': username, 'tipo': tipo},
        ).first()
    return row[0] if row and row[0] else ''


def marcar_notificacion_vista(username: str, tipo: str, ts: str,
                               db_path: Path = DEFAULT_DB_PATH) -> None:
    engine = get_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text('INSERT INTO notificaciones_vistas ("username", "tipo", "ts") '
                 'VALUES (:username, :tipo, :ts) '
                 'ON CONFLICT ("username", "tipo") DO UPDATE SET "ts" = excluded."ts"'),
            {'username': username, 'tipo': tipo, 'ts': ts},
        )


def ci_asignados_desde(ts: str, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Movimientos que quedaron con CI después de `ts`, agrupados por empresa.

    Se cruza el audit_log (cuándo se tocó el CI) con movements (qué empresa es
    y si el CI sigue puesto): así un CI que después se borró no queda avisando.
    """
    engine = get_engine(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT m."Empresa" AS empresa, COUNT(DISTINCT a."record_hash") AS cantidad, '
                 'MAX(a."ts") AS ultimo '
                 'FROM audit_log a JOIN movements m ON m."RecordHash" = a."record_hash" '
                 'WHERE a."action" = :accion AND a."ts" > :ts '
                 "AND COALESCE(m.\"CI\", '') <> '' "
                 'GROUP BY m."Empresa"'),
            {'accion': 'update_ci', 'ts': ts},
        ).all()
    return [{'empresa': row[0] or '', 'cantidad': int(row[1] or 0), 'ultimo': row[2] or ''} for row in rows]


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
