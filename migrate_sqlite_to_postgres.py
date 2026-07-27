"""Migración de una sola vez: copia los datos del conciliador.db (SQLite)
actual a una base PostgreSQL nueva.

Uso:
    python migrate_sqlite_to_postgres.py --sqlite conciliador.db \
        --to postgresql+psycopg2://usuario:clave@host:5432/basededatos

Es seguro correrlo más de una vez: los movimientos se cargan con el mismo
upsert por RecordHash que usa la app (db.upsert_movements), así que no se
duplican. El log de auditoría, al ser un historial de solo-inserción sin una
clave natural para dedupe, se migra una única vez: si el audit_log de destino
ya tiene registros, se omite (para no duplicar en una segunda corrida).
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import text

import db


def migrate_movements(sqlite_path: str, target_url: str) -> tuple[int, int]:
    df = db.load_movements(db_path=sqlite_path)
    if df.empty:
        return 0, 0
    return db.upsert_movements(df, db_path=target_url)


def migrate_audit_log(sqlite_path: str, target_url: str) -> int:
    source_engine = db.get_engine(sqlite_path)
    target_engine = db.get_engine(target_url)

    with target_engine.connect() as conn:
        existing = conn.execute(text('SELECT COUNT(*) FROM audit_log')).scalar()
    if existing:
        print(f'El audit_log de destino ya tiene {existing} filas: se omite la migración de auditoría.')
        return 0

    with source_engine.connect() as conn:
        rows = conn.execute(
            text('SELECT "ts", "username", "action", "detail", "record_hash" FROM audit_log ORDER BY "id" ASC')
        ).all()
    if not rows:
        return 0

    insert_sql = text(
        'INSERT INTO audit_log ("ts", "username", "action", "detail", "record_hash") '
        'VALUES (:ts, :username, :action, :detail, :record_hash)'
    )
    with target_engine.begin() as conn:
        for ts, username, action, detail, record_hash in rows:
            conn.execute(insert_sql, {
                'ts': ts, 'username': username, 'action': action,
                'detail': detail, 'record_hash': record_hash,
            })
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', required=True, help='Ruta al conciliador.db de origen')
    parser.add_argument('--to', required=True, help='URL de SQLAlchemy de la Postgres de destino')
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f'No se encontró {sqlite_path}', file=sys.stderr)
        sys.exit(1)

    print(f'Migrando movimientos desde {sqlite_path} hacia {args.to} ...')
    total, new_count = migrate_movements(str(sqlite_path), args.to)
    print(f'Movimientos procesados: {total}. Nuevos/actualizados: {new_count}.')

    print('Migrando log de auditoría...')
    audit_count = migrate_audit_log(str(sqlite_path), args.to)
    print(f'Filas de auditoría migradas: {audit_count}.')

    print('Listo.')


if __name__ == '__main__':
    main()
