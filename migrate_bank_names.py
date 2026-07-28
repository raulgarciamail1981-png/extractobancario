"""Migración de una sola vez: renombra bancos en los movimientos ya guardados.

Se corre después de cambiar un valor de BANK_PATTERNS en conciliador.py.

Por qué hace falta: el nombre del banco es el primer campo del RecordHash que
identifica a cada movimiento. Si solo se cambia el nombre en el código, los
movimientos ya guardados quedan con el hash viejo y la próxima importación de
los mismos extractos los vuelve a insertar como nuevos — duplicados, y con el
CI en blanco porque el CI vive en la fila vieja.

Este script actualiza el nombre Y recalcula el hash de esas filas, de modo que
coincida con el que va a calcular la próxima importación. Es idempotente: las
filas ya renombradas se saltean, y si el hash nuevo ya existe (porque alguien
importó con el nombre nuevo antes de migrar) fusiona las dos filas en vez de
fallar.

Uso:
    python migrate_bank_names.py                    # SQLite local (conciliador.db)
    python migrate_bank_names.py --db "postgresql+psycopg2://usr:pass@db:5432/base"
    python migrate_bank_names.py --dry-run          # muestra qué haría, sin tocar nada
"""
import argparse
import os
from datetime import datetime

from sqlalchemy import text

import db
from conciliador import hash_record

# Nombre viejo -> nombre nuevo. Al agregar un renombre futuro, sumarlo acá y
# volver a correr el script.
RENAMES = {
    'Santander': 'Santander RIO',
    'Municipal': 'Banco Municipal',
}


def _fecha_como_en_importacion(fecha_guardada: str):
    """Devuelve la Fecha con el mismo tipo que tiene al calcular el hash.

    El hash se calcula durante la importación, cuando 'Fecha' todavía es un
    datetime — str() da 'AAAA-MM-DD 00:00:00'. En la base se guarda ya
    normalizada como texto 'AAAA-MM-DD'. Recalcular el hash sobre el texto
    daría un valor distinto al que va a producir la próxima importación, que
    es justamente lo que hay que evitar.
    """
    texto = (fecha_guardada or '').strip()
    if not texto:
        return ''
    try:
        return datetime.strptime(texto, '%Y-%m-%d')
    except ValueError:
        return texto


def _nuevo_hash(row: dict, banco_nuevo: str) -> str:
    return hash_record({
        'Banco': banco_nuevo,
        'Cuenta': row['Cuenta'],
        'Fecha': _fecha_como_en_importacion(row['Fecha']),
        'Monto': row['Monto'],
        'Saldo': row['Saldo'],
        'CUIT': row['CUIT'],
        'Descripcion': row['Descripcion'],
    })


_SELECT_SQL = text('''
    SELECT "RecordHash", "Banco", "Cuenta", "Fecha", "Monto", "Saldo", "CUIT", "Descripcion", "CI"
    FROM movements WHERE "Banco" = :banco
''')

_CAMPOS = ('RecordHash', 'Banco', 'Cuenta', 'Fecha', 'Monto', 'Saldo', 'CUIT', 'Descripcion', 'CI')


def migrate(db_path, dry_run: bool = False) -> dict:
    engine = db.get_engine(db_path)
    resumen = {'renombradas': 0, 'fusionadas': 0, 'sin_cambios': 0}

    with engine.begin() as conn:
        for banco_viejo, banco_nuevo in RENAMES.items():
            filas = [dict(zip(_CAMPOS, r)) for r in conn.execute(_SELECT_SQL, {'banco': banco_viejo}).all()]
            for fila in filas:
                hash_viejo = fila['RecordHash']
                hash_nuevo = _nuevo_hash(fila, banco_nuevo)
                if hash_nuevo == hash_viejo:
                    resumen['sin_cambios'] += 1
                    continue

                existe = conn.execute(
                    text('SELECT "CI" FROM movements WHERE "RecordHash" = :h'), {'h': hash_nuevo},
                ).first()

                if dry_run:
                    resumen['fusionadas' if existe else 'renombradas'] += 1
                    continue

                if existe:
                    # Ya hay una fila con el nombre nuevo: nos quedamos con
                    # ella y le pasamos el CI de la vieja si el suyo está en
                    # blanco, para no perder trabajo de la cajera.
                    ci_existente = (existe[0] or '').strip()
                    ci_viejo = (fila['CI'] or '').strip()
                    if not ci_existente and ci_viejo:
                        conn.execute(
                            text('UPDATE movements SET "CI" = :ci WHERE "RecordHash" = :h'),
                            {'ci': ci_viejo, 'h': hash_nuevo},
                        )
                    conn.execute(text('DELETE FROM movements WHERE "RecordHash" = :h'), {'h': hash_viejo})
                    resumen['fusionadas'] += 1
                else:
                    conn.execute(
                        text('UPDATE movements SET "Banco" = :banco, "RecordHash" = :nuevo '
                             'WHERE "RecordHash" = :viejo'),
                        {'banco': banco_nuevo, 'nuevo': hash_nuevo, 'viejo': hash_viejo},
                    )
                    resumen['renombradas'] += 1

                # El log de auditoría referencia movimientos por hash: se
                # reapunta para no cortar la trazabilidad de los cambios de CI.
                conn.execute(
                    text('UPDATE audit_log SET "record_hash" = :nuevo WHERE "record_hash" = :viejo'),
                    {'nuevo': hash_nuevo, 'viejo': hash_viejo},
                )
    return resumen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=None,
                        help='URL de SQLAlchemy o ruta al .db. Por defecto usa DATABASE_URL o conciliador.db')
    parser.add_argument('--dry-run', action='store_true', help='Solo informa, no modifica la base')
    args = parser.parse_args()

    db_path = args.db or os.environ.get('DATABASE_URL') or str(db.DEFAULT_DB_PATH)
    print(f'Base: {db_path}')
    print('Renombres: ' + ', '.join(f'{v} -> {n}' for v, n in RENAMES.items()))
    if args.dry_run:
        print('(dry-run: no se modifica nada)')

    resumen = migrate(db_path, dry_run=args.dry_run)

    print(f"Movimientos renombrados: {resumen['renombradas']}")
    print(f"Movimientos fusionados con una fila ya existente: {resumen['fusionadas']}")
    print(f"Sin cambios: {resumen['sin_cambios']}")
    print('Listo.' if not args.dry_run else 'Dry-run terminado, no se escribió nada.')


if __name__ == '__main__':
    main()
