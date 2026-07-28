# Deploy en un VPS con Docker

Guía para poner en marcha el Conciliador en un servidor virtual propio, con
PostgreSQL y HTTPS automático (Caddy + Let's Encrypt).

## 0. Requisitos previos

- Un VPS con acceso SSH (Ubuntu/Debian es lo más común).
- Un dominio propio (ej. `conciliador.tuempresa.com`) con un registro DNS tipo
  **A** apuntando a la IP pública del VPS. Caddy necesita esto para poder pedir
  el certificado HTTPS — sin dominio resuelto, el certificado falla.
- Puertos **80** y **443** abiertos en el firewall del VPS (Caddy los necesita
  para la validación de Let's Encrypt y para servir HTTPS).

## 1. Instalar Docker en el VPS (si no lo tiene)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # cerrar sesión y volver a entrar después de esto
```

Verificar: `docker --version` y `docker compose version`.

## 2. Copiar el proyecto al VPS

Desde tu PC (con el repo actualizado):

```bash
scp -r "Conciliador Cuentas" usuario@IP-DEL-VPS:~/conciliador
```

O cloná/subí el repo por git si preferís ese flujo.

## 3. Preparar los datos persistentes (`./data`)

En el VPS, dentro de `~/conciliador`:

```bash
mkdir -p data/uploads
```

Copiar ahí los archivos maestros reales (los mismos que hoy usa la app en la
PC de Windows):

- `users.json` → `data/users.json` — si todavía no existe, arrancar desde la
  plantilla del repo: `cp users.example.json data/users.json`. Trae un solo
  usuario `admin` con contraseña `cambiame123`; **cambiala apenas entres**
  (Admin → Editar, o desde "Cambiar contraseña"). Sin este archivo la app
  levanta igual, pero el login avisa que no hay usuarios configurados y nadie
  puede entrar.
- `Empresas.xlsx` → `data/Empresas.xlsx`
- `DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx` → `data/DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx`

Estos 4 elementos (`data/uploads/`, `data/users.json`, `data/Empresas.xlsx`,
`data/DATOS BANCARIOS TODAS LAS EMPRESAS.xlsx`) son los que `docker-compose.yml`
monta dentro del contenedor — quedan fuera de la imagen a propósito, para
poder editarlos sin reconstruir nada.

## 4. Completar el `.env`

```bash
cp .env.example .env
```

Editar `.env` y completar:

- `SECRET_KEY`: generar una con `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `POSTGRES_PASSWORD`: una contraseña fuerte para la base.
- `DOMAIN`: el dominio que apunta al VPS (paso 0).

`POSTGRES_USER`/`POSTGRES_DB` ya vienen con un valor por defecto razonable en
`.env.example`, no hace falta tocarlos salvo preferencia.

## 5. Levantar todo

```bash
docker compose up -d --build
```

Esto levanta 3 servicios: `db` (Postgres), `web` (la app) y `proxy` (Caddy,
que obtiene el certificado HTTPS automáticamente la primera vez que arranca).
Verificar que los tres están corriendo: `docker compose ps`.

## 6. Migrar los datos existentes (una sola vez)

Si ya tenías movimientos cargados en el `conciliador.db` de la PC de Windows,
copiarlo también a `data/` (ej. `data/conciliador.db`) y correr, con los
contenedores ya arriba:

```bash
docker compose exec web python migrate_sqlite_to_postgres.py \
    --sqlite /app/uploads/../conciliador.db \
    --to "postgresql+psycopg2://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB"
```

(Más simple: montar el `conciliador.db` en `data/` y referenciarlo por su ruta
dentro del contenedor, ej. copiándolo primero a `data/uploads/conciliador.db`
y usando `--sqlite /app/uploads/conciliador.db`.) El script es idempotente —
se puede correr de nuevo sin duplicar si algo falla a mitad de camino.

## 7. Verificar

- `https://<DOMAIN>/` carga la pantalla de login con candado (certificado
  válido, sin advertencias del navegador).
- Login funciona con los usuarios de `data/users.json`.
- `/records` muestra los movimientos migrados (contrastar el total contra lo
  que mostraba la app en Windows antes de migrar).
- Subir un extracto nuevo y unificarlo funciona de punta a punta.
- Desde otra PC/ubicación (fuera de la red del VPS), `https://<DOMAIN>/`
  también carga — confirma que el acceso remoto multi-usuario ya funciona.

## Seguridad de la sesión

Ya viene configurado, no hay que tocar nada para el deploy con Docker, pero
conviene saberlo:

- **Cookie de sesión**: `HttpOnly`, `SameSite=Lax` y `Secure` (solo viaja por
  HTTPS). Si alguna vez levantás la app por HTTP plano fuera de Docker,
  `python web_app.py` ya apaga `Secure` solo; para otro entrypoint, exportá
  `CONCILIADOR_INSECURE_COOKIES=1`.
- **CSRF**: todos los formularios llevan token. Si aparece un `400 Bad Request`
  al enviar un formulario, suele ser una pestaña vieja abierta desde antes de
  reiniciar la app — recargar la página lo resuelve.
- **Intentos de login**: después de 5 fallidos con el mismo usuario desde la
  misma IP, ese par queda bloqueado 5 minutos y la app responde 429. El bloqueo
  se registra una vez en la auditoría (`login_blocked`). Se libera solo con el
  tiempo, o reiniciando la app (`docker compose restart web`) si alguien
  necesita entrar ya.
- `TRUST_PROXY_HEADERS=1` está puesto en `docker-compose.yml` para que la app
  vea la IP real del usuario y no la de Caddy. **No lo actives si la app queda
  expuesta sin un proxy adelante**: ahí cualquiera podría falsear su IP.

## Operación día a día

- **Reiniciar tras un cambio de código**: `docker compose up -d --build web`.
- **Ver logs**: `docker compose logs -f web`.
- **Backup de la base**: `docker compose exec db pg_dump -U $POSTGRES_USER $POSTGRES_DB > backup.sql`.
- **Actualizar `Empresas.xlsx` / `DATOS BANCARIOS...xlsx`**: reemplazar el
  archivo directamente en `data/` en el VPS, no hace falta reiniciar nada (se
  lee en cada carga/unificación).
