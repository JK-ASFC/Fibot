FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/cache

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

# Le bot tourne sans droits root ; /data contient data.json (positions, alertes, acceptations)
RUN useradd --create-home fibot && mkdir /data && chown fibot /data
USER fibot
VOLUME /data

CMD ["python", "bot.py"]
