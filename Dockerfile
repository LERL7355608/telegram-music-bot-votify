FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ffmpeg: requerido por los providers que transcodifican audio.
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --no-compile -r requirements.txt

COPY . .

# /app/config recibe el bind-mount con los archivos privados (ver docker-compose.yml).
RUN mkdir -p /tmp/downloads /app/logs /app/storage /app/config \
    && chown -R appuser:appuser /app /tmp/downloads

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('HTTP_PORT','8080')+'/health', timeout=4).status==200 else 1)"

CMD ["python", "bot.py"]
