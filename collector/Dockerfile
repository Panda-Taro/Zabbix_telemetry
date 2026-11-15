# collector(収集エンジン)のコンテナのビルド用ファイル(DL-02)
# docker-compose.yml から、リポジトリの最上位をビルドの起点として使う。
# Python の版は固定する(要件定義書 11.4.5)。
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ライブラリ(版番号を固定したもの)を先に入れる
COPY collector/requirements.txt /app/collector/requirements.txt
RUN pip install --no-cache-dir -r /app/collector/requirements.txt

COPY common /app/common
COPY collector /app/collector

# 管理者でない利用者として動かす(決定事項 D-030)。番号は docker-compose.yml、deploy.sh と同じ
USER 10001:10001

CMD ["python", "-m", "collector"]
