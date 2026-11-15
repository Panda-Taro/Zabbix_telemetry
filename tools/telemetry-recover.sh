#!/usr/bin/env bash
# Telemetry 中間サーバーの復旧用コマンド(DL-05、要件定義書 7.4.7.5、OP-07)
#
# deploy.sh が /usr/local/sbin/telemetry-recover として置く。中間サーバーのコンソールで使う。
#   sudo telemetry-recover reset-port                 WebGUIのポート番号を既定値(8080)に戻す
#   sudo telemetry-recover reset-password [ユーザー名]   管理者のパスワードを初期化する(省略時は admin)
#
# 中身の処理は、web のコンテナのイメージで動かす(web が止まっていても動く)。
# ダウンロードしたソースコードのフォルダは使わない(フォルダを消しても動く)。
# 本体のIPアドレスは、このコマンドでは直さない。コンソールで、Ubuntuの標準の手順で直す。
set -Eeuo pipefail

readonly WEB_IMAGE="telemetry-web:latest"
readonly WEB_CONTAINER="telemetry-web"
readonly DATA_DIR="/data"
readonly CONTAINER_UID="10001"
readonly HOSTCTL="/usr/local/lib/telemetry-hostctl/tools/telemetry_hostctl.py"
readonly FALLBACK_PORT="8080"

usage() {
    cat <<'EOF'
使い方:
  sudo telemetry-recover reset-port
      WebGUIのポート番号を、既定値の 8080 に戻します。WebGUIは起動し直します。
  sudo telemetry-recover reset-password [ユーザー名]
      管理者のパスワードを、ユーザー名と同じ文字列に初期化します(ユーザー名を省略すると admin)。
EOF
}

if [[ ${EUID} -ne 0 ]]; then
    echo "管理者の権限で実行してください(例:sudo telemetry-recover reset-port)" >&2
    exit 1
fi
if ! docker image inspect "${WEB_IMAGE}" >/dev/null 2>&1; then
    echo "WebGUIのコンテナのイメージ(${WEB_IMAGE})が見つかりません。deploy.sh をもう一度実行してください" >&2
    exit 1
fi

run_tool() {
    docker run --rm --network none --user "${CONTAINER_UID}:${CONTAINER_UID}" \
        -v "${DATA_DIR}:/data" "${WEB_IMAGE}" python -m tools.recovery "$@"
}

case "${1:-}" in
    reset-port)
        run_tool reset-port
        if [[ -f "${HOSTCTL}" ]]; then
            # ファイアウォールが有効な場合は、8080 を開ける(無効なら何もしない)
            python3 -I "${HOSTCTL}" firewall-sync --db "${DATA_DIR}/telemetry.db" \
                || echo "注意:ファイアウォールの調整に失敗しました。sudo ufw allow ${FALLBACK_PORT}/tcp を実行してください" >&2
        fi
        docker restart "${WEB_CONTAINER}" >/dev/null
        echo "WebGUIを起動し直しました。ブラウザで http://(このサーバーのIPアドレス):${FALLBACK_PORT}/ を開いてください"
        ;;
    reset-password)
        if [[ $# -gt 2 ]]; then
            usage
            exit 1
        fi
        run_tool reset-password ${2:+"$2"}
        ;;
    *)
        usage
        exit 1
        ;;
esac
