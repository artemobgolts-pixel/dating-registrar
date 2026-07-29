FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg используется только для быстрого remux MP4 (`+faststart`, без
# перекодирования). Если конкретный файл не удаётся обработать, приложение
# безопасно оставляет исходник.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Приложение работает от непривилегированного пользователя.
# Сам контейнер стартует от root: entrypoint выравнивает права на /data
# (см. docker-entrypoint.sh) и понижает привилегии через setpriv.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# ВАЖНО: ровно один воркер. Лимиты запросов и троттлинг входа хранятся
# в памяти процесса; SQLite тоже комфортнее с одним писателем.
# IP клиента приложение берёт из X-Real-IP, который перезаписывает Caddy, —
# поэтому никаких --proxy-headers с доверием "*" здесь нет.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
