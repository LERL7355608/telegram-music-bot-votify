# Telegram Music Bot

> Estado de esta copia: experimento Votify conservado para continuar cuando el
> proveedor corrija su autenticacion PlayPlay. Consulta
> [`docs/AI_HANDOFF.md`](docs/AI_HANDOFF.md) antes de desplegarlo.

Bot privado de Telegram para gestionar busquedas y descargas con arquitectura Provider.

La descarga real no esta implementada en este repositorio. El bot usa `MockProvider`, que devuelve resultados de prueba y crea archivos dummy para validar el flujo completo.

## Stack

- `python-telegram-bot`
- `aiohttp`
- `aiosqlite`
- `python-dotenv`
- `asyncio.Queue`
- SQLite

## Flujo

```text
Usuario escribe una busqueda
  -> Provider.search()
  -> botones inline con maximo 5 resultados
  -> usuario elige cancion y calidad
  -> SQLite registra pending
  -> asyncio.Queue procesa el job
  -> Provider.download()
  -> SQLite registra ready + token + expiracion
  -> aiohttp sirve /download/{token}
  -> cleanup borra expirados
```

## Configuracion

1. Copia el archivo de ejemplo:

```bash
cp .env.example .env
```

2. Edita `.env`:

```env
TELEGRAM_BOT_TOKEN=tu_token_aqui
TELEGRAM_USER_ID=tu_id_de_telegram
DOWNLOAD_PATH=/tmp/downloads
DATABASE_PATH=storage/downloads.sqlite3
BASE_URL=http://localhost:8080
HTTP_PORT=8080
MAX_DOWNLOADS_PER_HOUR=10
FILE_EXPIRY_HOURS=12
PROVIDER=mock
```

Para obtener tu `TELEGRAM_USER_ID`, puedes escribirle a bots como `@userinfobot`.

## Ejecucion local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

En Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

El servidor de archivos queda disponible en:

```text
http://localhost:8080/download/{token}
```

## Docker

```bash
docker compose up -d --build
docker compose logs -f bot
```

Detener:

```bash
docker compose down
```

## Deploy basico en EC2

1. Abre el puerto HTTP que usaras para descargas, por ejemplo `8080`, en el Security Group.
2. Instala Docker y Docker Compose en la instancia.
3. Copia este proyecto al EC2.
4. Crea `.env` con:

```env
BASE_URL=http://IP_PUBLICA_DEL_EC2:8080
HTTP_PORT=8080
```

5. Levanta el servicio:

```bash
docker compose up -d --build
```

Para produccion con dominio y HTTPS, pon nginx delante y usa:

```env
BASE_URL=https://tu-dominio.com
```

Nginx debe redirigir `/download/` hacia el contenedor en el puerto `8080`.

## Provider real

El contrato esta en `providers/base.py`:

```python
class DownloadProvider(ABC):
    async def search(self, query: str) -> list[dict]:
        ...

    async def download(self, track_id: str, quality: str, output_dir: Path) -> Path:
        ...
```

Para agregar tu proveedor usando el placeholder incluido:

1. Edita `providers/custom.py`.
2. Implementa `CustomProvider.search()`.
3. Implementa `CustomProvider.download()`.
4. Cambia `.env`:

```env
PROVIDER=custom
CUSTOM_PROVIDER_TOKEN=opcional
```

El bot espera que:

- `search()` devuelva diccionarios con `id`, `title`, `artist`, `album` y `duration`.
- `download()` cree un archivo dentro de `output_dir` y retorne el `Path` final.

## Almacenamiento S3

Para usar S3 en vez del servidor local de archivos:

```env
STORAGE_BACKEND=s3
AWS_REGION=us-west-1
S3_BUCKET=telegram-music-bot-tu-cuenta-us-west-1
S3_PREFIX=downloads
```

El flujo S3 es:

```text
Provider.download() escribe temporalmente en disco
  -> el bot sube el archivo a S3
  -> borra la copia local
  -> genera un presigned URL de 24 horas
  -> cleanup borra el objeto expirado en S3
```

En EC2, usa un IAM role en la instancia en vez de access keys en `.env`.

## Limpieza

El bot corre limpieza automatica cada hora. Tambien puedes ejecutar una pasada manual:

```bash
python -m services.cleanup
```

## SQLite

La tabla principal es `downloads`:

```sql
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    track_id TEXT,
    title TEXT,
    artist TEXT,
    quality TEXT NOT NULL,
    file_path TEXT,
    token TEXT UNIQUE,
    status TEXT NOT NULL,
    error_message TEXT,
    metadata TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP
);
```

Estados usados:

- `pending`
- `downloading`
- `ready`
- `error`
- `expired`

## Seguridad aplicada

- Whitelist por `TELEGRAM_USER_ID`.
- Rate limit en memoria por usuario.
- Tokens unicos por descarga.
- Expiracion por SQLite antes de servir archivo.
- `Cache-Control: no-store`.
- Logs con rotacion.
