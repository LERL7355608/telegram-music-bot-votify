FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# <-- PEGAR AQUÍ: Instalar ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --no-compile -r requirements.txt

COPY . .

# <-- MODIFICAR ESTA LÍNEA: Agregar /app/config
RUN mkdir -p /tmp/downloads /app/logs /app/storage /app/config \
    && chown -R appuser:appuser /app /tmp/downloads

USER appuser

EXPOSE 8080

CMD ["python", "bot.py"]
