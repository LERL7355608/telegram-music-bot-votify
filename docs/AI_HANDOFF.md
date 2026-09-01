# Traspaso tecnico: variante Votify

## Proposito de este repositorio

Este repositorio conserva por separado la variante Votify del bot de Telegram.
La arquitectura general del bot funciona, pero el proveedor Votify no completa
descargas con Spotify en el estado documentado aqui. No debe describirse como
operativo hasta repetir las pruebas indicadas al final.

Los secretos y archivos ligados a una cuenta no forman parte del repositorio:

- `.env`
- `config/cookies.txt`
- `config/Spotify.dll`
- archivos `.wvd`, claves SSH y respaldos de WinSCP
- SQLite, logs y archivos descargados

## Arquitectura

```text
Telegram
  -> handlers
  -> DownloadProvider (providers/base.py)
  -> asyncio queue
  -> almacenamiento temporal
  -> S3 o servidor aiohttp
  -> SQLite para estado e historial
```

La frontera estable es `DownloadProvider`. Los handlers, la cola y el
almacenamiento no deben acoplarse a Votify. El loader en
`providers/__init__.py` selecciona el proveedor mediante `PROVIDER`.

Proveedores presentes:

- `mock`: prueba segura del flujo sin descargas reales.
- `custom`: proveedor anterior conservado como referencia.
- `votify`: experimento actual con Spotify Web API, Votify CLI y ffmpeg.

## Funciones existentes del bot

- busqueda inline de canciones
- resolucion de URLs de canciones y playlists
- descarga individual y por playlist mediante el Provider
- calidad `mp3_320` o `flac`
- ZIP por partes para playlists grandes
- historial por usuario en SQLite
- letras LRC de mejor esfuerzo
- almacenamiento local o S3 con enlaces temporales
- limpieza automatica, logging y metricas de rendimiento

## Configuracion de la variante Votify

Parte publica/no secreta:

```env
PROVIDER=votify
SPOTIFY_DLL_PATH=/app/config/Spotify.dll
COOKIES_PATH=/app/config/cookies.txt
```

Tambien requiere `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` en `.env` para
las consultas oficiales de metadata. Nunca guardar valores reales en Git.

Docker monta `./config` como `/app/config`. Los archivos privados deben
colocarse manualmente en el host despues de clonar el repositorio.

## Estado confirmado de Votify

Versiones evaluadas:

- Votify `1.9.9`
- unplayplay `0.0.9`
- Spotify.dll `1.2.88.485`
- sesion Votify `desktop`

La DLL fue validada por el emulador de claves y las cookies contenian una
sesion vigente. El problema no fue la ausencia de esos archivos.

Resultado del diagnostico:

1. La autenticacion `desktop` de Votify recibio HTTP 400 al consultar perfil y
   metadata mediante Pathfinder.
2. Una segunda sesion `WEB` usada solo para metadata corrigio Pathfinder,
   letras, creditos, GID, metadata extendida y URLs de stream.
3. Mantener la sesion `desktop` para solicitar la licencia llevo el flujo hasta
   PlayPlay, que respondio HTTP 400.
4. Por ello no se integro el parche de doble sesion: habria ocultado Pathfinder
   sin conseguir una descarga completa.

Error bloqueante observado:

```text
PlayPlay license request failed with status code 400
```

La conclusion es que Votify debe actualizar su identidad/autenticacion de
cliente desktop o su compatibilidad con la licencia PlayPlay. No atribuir este
fallo automaticamente a cookies, Premium o a la DLL.

## Despliegue de referencia

El despliegue usado para probar esta copia estaba en una instancia EC2 con
Docker Compose y el proyecto en `/opt/telegram-music-bot`. La direccion IP y el
estado de la instancia pueden cambiar; deben consultarse en AWS antes de usarla.

Instalacion limpia:

```bash
cp .env.example .env
mkdir -p config logs storage storage/downloads
docker compose up -d --build
curl -fsS http://127.0.0.1:8080/health
docker compose logs --tail=100 bot
```

Primero debe validarse `PROVIDER=mock`. Solo despues se configura Votify con
los secretos locales.

## Criterio para reanudar

Cuando exista una nueva version de Votify:

1. Trabajar en una rama nueva.
2. Actualizar una dependencia por vez.
3. Probar una sola cancion en un directorio temporal.
4. Confirmar que Pathfinder y PlayPlay responden correctamente.
5. Verificar que el archivo final existe, tiene audio valido y metadata.
6. Ejecutar el bot con `mock` y luego con `votify`.
7. Desplegar en EC2 solo despues de las pruebas anteriores.

No subir cookies, tokens, DLL, WVD, archivos descargados ni logs de solicitudes
que puedan contener credenciales.
