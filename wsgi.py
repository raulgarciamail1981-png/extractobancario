from waitress import serve

import db
from conciliador import MASTER_FILE
from web_app import BASE_DIR, DB_PATH, UPLOAD_DIR, app

if __name__ == '__main__':
    UPLOAD_DIR.mkdir(exist_ok=True)
    db.migrate_excel_if_needed(BASE_DIR / MASTER_FILE.name, db_path=DB_PATH)
    serve(app, host='0.0.0.0', port=8080)
