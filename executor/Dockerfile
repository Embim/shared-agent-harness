FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# bash-тул рассчитывает на обычный юникс-инструментарий, в slim его нет.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils findutils grep sed gawk diffutils procps git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Владельца /workspace выставляем в образе: свежий named volume наследует
# права из образа, иначе non-root пользователь не сможет туда писать.
RUN useradd -u 10001 -m runner \
    && mkdir -p /workspace \
    && chown -R 10001:10001 /workspace /srv

USER 10001
EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1"]
