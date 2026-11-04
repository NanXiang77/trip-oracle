FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    ENABLE_SCHEDULER=1 \
    SKIP_HEAT=1 \
    COLLECT_HOURS=8,20

WORKDIR /app
COPY . /app

# 纯标准库，无需 pip install；如需百度指数可自行加 qdata
RUN python -c "import sqlite3; print('sqlite ok')"

EXPOSE 8901
CMD ["python", "cloud_run.py"]
