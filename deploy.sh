#!/usr/bin/env bash
# Telemetry 中間サーバーのデプロイ用スクリプト(DL-09、要件定義書 4.5)
#
# 使い方(ダウンロードしたソースコードのフォルダで実行する):
#   sudo bash deploy.sh
#
# 要件定義書 4.5.3 の11の手順を、表の順番どおりに行う。
# - 何度実行しても同じ結果になる。2回目以降も /data の中身(機器、設定、ユーザー、ログ)は消さない(4.5.4)
# - 各手順の開始と結果を表示し、失敗したら、失敗した手順と原因を表示して止まる(4.5.7)
# - 本体のIPアドレスは最後に設定する。変更しないことも選べる(4.5.5)
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TOTAL_STEPS=11
readonly DATA_DIR="/data"
readonly DB_PATH="${DATA_DIR}/telemetry.db"
# コンテナの中のプログラムを動かす利用者の番号(docker-compose.yml、Dockerfile と同じ。決定事項 D-030)
readonly CONTAINER_UID="10001"
readonly COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
readonly CONTAINERS=("telemetry-collector" "telemetry-web")
# UDPの受信バッファの上限(collector が使う 8MB に合わせる。11.1.3、決定事項 D-019)
readonly SYSCTL_FILE="/etc/sysctl.d/90-telemetry.conf"
readonly RMEM_MAX="8388608"
readonly TIMESYNCD_CONF="/etc/systemd/timesyncd.conf.d/90-telemetry.conf"
# 本体側の補助プログラム(決定事項 D-029)
readonly HOSTCTL_RUN_DIR="/run/telemetry-hostctl"
readonly HOSTCTL_LIB_DIR="/usr/local/lib/telemetry-hostctl"
readonly HOSTCTL="${HOSTCTL_LIB_DIR}/tools/telemetry_hostctl.py"
readonly HOSTCTL_SRC="${SCRIPT_DIR}/tools/telemetry_hostctl.py"
readonly TMPFILES_CONF="/etc/tmpfiles.d/telemetry-hostctl.conf"
readonly SYSTEMD_DIR="/etc/systemd/system"
readonly RECOVER_CMD="/usr/local/sbin/telemetry-recover"
# WebGUIが応答するまで待つ時間(秒)
readonly WEB_WAIT_SEC="120"
readonly FALLBACK_PORT="8080"
# コマンドの失敗を見分けるための目印
readonly FAILED_MARK="__TELEMETRY_FAILED__"

CURRENT_STEP="0"
CURRENT_TITLE="準備"
NEW_NETWORK=""

# ----------------------------------------------------------------------
# 表示の部品
# ----------------------------------------------------------------------
step() {
    CURRENT_STEP="$1"
    CURRENT_TITLE="$2"
    echo
    echo "=== 手順 ${CURRENT_STEP}/${TOTAL_STEPS}:${CURRENT_TITLE} ==="
}
info() { echo "  ${*}"; }
result() { echo "  → 結果:${*}"; }
fail() {
    echo >&2
    echo "!!! 手順 ${CURRENT_STEP}(${CURRENT_TITLE})で失敗しました。" >&2
    echo "!!! 原因:${*}" >&2
    echo "!!! 原因を取り除いてから、もう一度 sudo bash deploy.sh を実行してください。" >&2
    exit 1
}
on_error() {
    fail "コマンドが失敗しました(${2}、deploy.sh の ${1} 行目)"
}
trap 'on_error "${LINENO}" "${BASH_COMMAND}"' ERR

# 画面から1行入力してもらう(パイプで実行された場合も、画面から読む)
ask() {
    local prompt="$1"
    local answer=""
    read -r -p "  ${prompt}" answer </dev/tty || true
    printf '%s' "${answer}"
}
yes_no() {
    local answer
    answer="$(ask "$1 [y/N]:")"
    [[ "${answer}" == "y" || "${answer}" == "Y" ]]
}
hostctl() {
    python3 -I "${HOSTCTL_SRC}" --db "${DB_PATH}" "$@"
}

echo "Telemetry 中間サーバーのデプロイを始めます(ソースコードのフォルダ:${SCRIPT_DIR})"

# ----------------------------------------------------------------------
# 手順1:実行環境の確認
# ----------------------------------------------------------------------
step 1 "実行環境の確認(Ubuntu Server 24.04 LTS、管理者権限、インターネットへの接続)"
[[ ${EUID} -eq 0 ]] || fail "管理者権限で実行されていません。sudo bash deploy.sh と入力してください"
[[ -r /etc/os-release ]] || fail "OSの種類を確かめられません(/etc/os-release がありません)"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] \
    || fail "Ubuntu 24.04 LTS ではありません(このOS:${PRETTY_NAME:-不明})"
info "OS:${PRETTY_NAME}"
[[ -f "${COMPOSE_FILE}" && -f "${HOSTCTL_SRC}" ]] \
    || fail "ソースコードの一式が見つかりません。ダウンロードしたフォルダの中で実行してください"
[[ -r /dev/tty ]] || fail "画面から入力できません。サーバーのコンソールか SSH の画面で実行してください"
command -v python3 >/dev/null || fail "python3 が見つかりません"
command -v curl >/dev/null || fail "curl が見つかりません(sudo apt install curl で入れてください)"
for url in "http://archive.ubuntu.com/ubuntu/" "https://registry-1.docker.io/v2/" "https://pypi.org/simple/"; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${url}" || true)"
    [[ "${code}" != "000" ]] || fail "インターネットに接続できません(${url} に届きません)。ネットワークの設定を確かめてください"
    info "接続できました:${url}"
done
result "問題ありません"

# ----------------------------------------------------------------------
# 手順2:Dockerの導入(未導入の場合のみ)
# ----------------------------------------------------------------------
step 2 "Dockerの導入(未導入の場合のみ)"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    result "導入済みのため、入れ直しません($(docker --version))"
else
    info "Docker を入れます(Ubuntu の標準の配布物:docker.io、docker-compose-v2)"
    apt-get update
    packages=(docker.io docker-compose-v2)
    if apt-cache show docker-buildx >/dev/null 2>&1; then
        packages+=(docker-buildx)
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
    docker compose version >/dev/null 2>&1 || fail "Docker Compose を使えるようになりませんでした"
    result "入れました($(docker --version))"
fi
systemctl start docker
docker info >/dev/null 2>&1 || fail "Docker が動いていません(sudo systemctl status docker で確かめてください)"

# ----------------------------------------------------------------------
# 手順3:データ用フォルダ(/data)の作成
# ----------------------------------------------------------------------
step 3 "データ用フォルダ(${DATA_DIR})の作成"
if [[ -f "${DB_PATH}" ]]; then
    info "前回までのデータ(登録した機器、設定、ユーザー、ログ)があります。消さずに、そのまま使います"
fi
mkdir -p "${DATA_DIR}/logs"
# コンテナの中のプログラム(利用者 ${CONTAINER_UID})が読み書きできるようにする。中身は消さない
chown -R "${CONTAINER_UID}:${CONTAINER_UID}" "${DATA_DIR}"
chmod 750 "${DATA_DIR}"
result "${DATA_DIR} を用意しました"

# ----------------------------------------------------------------------
# 手順4:時刻同期(NTP)の設定と、UDPの受信バッファの上限を広げるOSの設定
# ----------------------------------------------------------------------
step 4 "時刻同期(NTP)の設定と、UDPの受信バッファの上限を広げるOSの設定"
if ! systemctl cat systemd-timesyncd.service >/dev/null 2>&1; then
    info "時刻同期の仕組み(systemd-timesyncd)を入れます"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y systemd-timesyncd
fi
current_ntp="Ubuntu標準のサーバー"
if [[ -f "${TIMESYNCD_CONF}" ]]; then
    current_ntp="$(sed -n 's/^NTP=//p' "${TIMESYNCD_CONF}" | head -n 1)"
fi
info "今のNTPサーバー:${current_ntp}"
while true; do
    ntp_input="$(ask "NTPサーバー(複数なら空白で区切る。Enterのみ:今のまま、default:Ubuntu標準に戻す):")"
    if [[ -z "${ntp_input}" || "${ntp_input}" == "default" ]]; then
        break
    fi
    valid=1
    count=0
    for server in ${ntp_input}; do
        count=$((count + 1))
        [[ "${server}" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || valid=0
    done
    if [[ ${valid} -eq 1 && ${count} -le 3 ]]; then
        break
    fi
    info "入力の誤り:NTPサーバーは、ホスト名かIPアドレスを、3つまで空白で区切って入力してください"
done
if [[ "${ntp_input}" == "default" ]]; then
    rm -f "${TIMESYNCD_CONF}"
    info "Ubuntu標準のサーバーに戻しました"
elif [[ -n "${ntp_input}" ]]; then
    mkdir -p "$(dirname "${TIMESYNCD_CONF}")"
    printf '# Telemetry 中間サーバーの deploy.sh が書いたファイル\n[Time]\nNTP=%s\n' "${ntp_input}" >"${TIMESYNCD_CONF}"
    info "NTPサーバーを ${ntp_input} にしました"
fi
timedatectl set-ntp true
systemctl restart systemd-timesyncd
info "時刻同期:有効"
printf '# Telemetry 中間サーバーの deploy.sh が書いたファイル(UDPの受信バッファの上限。要件定義書 11.1.3)\nnet.core.rmem_max = %s\n' \
    "${RMEM_MAX}" >"${SYSCTL_FILE}"
sysctl -q -p "${SYSCTL_FILE}"
[[ "$(sysctl -n net.core.rmem_max)" -ge "${RMEM_MAX}" ]] || fail "UDPの受信バッファの上限を広げられませんでした"
result "時刻同期を有効にし、UDPの受信バッファの上限を $(sysctl -n net.core.rmem_max) バイトにしました"

# ----------------------------------------------------------------------
# 手順5:ファイアウォールが有効な場合の、必要なポートの開放
# ----------------------------------------------------------------------
step 5 "ファイアウォールが有効な場合の、必要なポート(WebGUI用、Telemetry受信用)の開放"
ports="$(hostctl show-ports || true)"
[[ -n "${ports}" ]] || fail "保存された設定のポート番号を読み取れませんでした"
read -r webgui_port udp_port <<<"${ports}"
info "WebGUIのポート番号:${webgui_port}(TCP)、Telemetryの受信ポート番号:${udp_port}(UDP)"
firewall_message="$(hostctl firewall-sync || echo "${FAILED_MARK}")"
[[ "${firewall_message}" != *"${FAILED_MARK}"* ]] || fail "ファイアウォールの設定に失敗しました(上に表示した原因を確かめてください)"
result "${firewall_message}"

# ----------------------------------------------------------------------
# 手順6:コンテナの作成と起動
# ----------------------------------------------------------------------
step 6 "コンテナの作成と起動"
# web が使う、補助プログラムとのやり取りのフォルダ(手順8で中身を整える)
mkdir -p "${HOSTCTL_RUN_DIR}"
info "コンテナを作ります(初回は数分かかります)"
docker compose -f "${COMPOSE_FILE}" --project-directory "${SCRIPT_DIR}" up -d --build --remove-orphans
for container in "${CONTAINERS[@]}"; do
    [[ "$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" == "true" ]] \
        || fail "コンテナ ${container} が動いていません(sudo docker logs ${container} で確かめてください)"
done
ports="$(hostctl show-ports || true)"
[[ -n "${ports}" ]] || fail "保存された設定のポート番号を読み取れませんでした"
read -r webgui_port udp_port <<<"${ports}"
info "WebGUIの応答を待ちます(最大 ${WEB_WAIT_SEC} 秒)"
web_ok=""
for _ in $(seq 1 "${WEB_WAIT_SEC}"); do
    for port in "${webgui_port}" "${FALLBACK_PORT}"; do
        if [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${port}/login" || true)" == "200" ]]; then
            web_ok="${port}"
            break 2
        fi
    done
    sleep 1
done
[[ -n "${web_ok}" ]] || fail "WebGUIが応答しません(sudo docker logs telemetry-web で確かめてください)"
if [[ "${web_ok}" != "${webgui_port}" ]]; then
    info "注意:保存されたポート番号 ${webgui_port} が使えないため、WebGUIは ${web_ok} で動いています"
    webgui_port="${web_ok}"
fi
result "collector と web が動いています(WebGUIのポート番号 ${webgui_port})"

# ----------------------------------------------------------------------
# 手順7:OS起動時の自動起動の設定
# ----------------------------------------------------------------------
step 7 "OS起動時の自動起動の設定"
systemctl enable docker.service >/dev/null 2>&1
if systemctl list-unit-files containerd.service | grep -q containerd; then
    systemctl enable containerd.service >/dev/null 2>&1
fi
for container in "${CONTAINERS[@]}"; do
    policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "${container}" 2>/dev/null || true)"
    [[ "${policy}" == "always" ]] || fail "コンテナ ${container} の自動起動の設定が正しくありません(${policy})"
done
result "OSの起動時に Docker が起動し、collector と web が自動で起動します(異常終了時も自動で起動し直します)"

# ----------------------------------------------------------------------
# 手順8:補助プログラムの配置と、補助プログラムが使う部品(python3-yaml)の確認
# ----------------------------------------------------------------------
step 8 "補助プログラムの配置と、補助プログラムが使う部品(python3-yaml)の確認"
if ! python3 -c "import yaml" >/dev/null 2>&1; then
    info "補助プログラムが使う python3-yaml を入れます"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-yaml
fi
# 補助プログラムを、web コンテナから書き換えられない場所に置く(管理者だけが書き換えられる)
install -d -m 755 "${HOSTCTL_LIB_DIR}" "${HOSTCTL_LIB_DIR}/common" "${HOSTCTL_LIB_DIR}/tools"
install -m 644 "${SCRIPT_DIR}/common/__init__.py" "${SCRIPT_DIR}/common/constants.py" \
    "${SCRIPT_DIR}/common/netconfig.py" "${HOSTCTL_LIB_DIR}/common/"
install -m 644 "${HOSTCTL_SRC}" "${HOSTCTL_LIB_DIR}/tools/"
# web との依頼のやり取りのフォルダ(起動のたびに作り直される。web が書けるのは spool と inbox だけ)
cat >"${TMPFILES_CONF}" <<EOF
# Telemetry 中間サーバーの補助プログラムが使うフォルダ(deploy.sh が書いたファイル)
d ${HOSTCTL_RUN_DIR} 0755 root root -
d ${HOSTCTL_RUN_DIR}/spool 0700 ${CONTAINER_UID} ${CONTAINER_UID} -
d ${HOSTCTL_RUN_DIR}/inbox 0700 ${CONTAINER_UID} ${CONTAINER_UID} -
d ${HOSTCTL_RUN_DIR}/results 0755 root root -
d ${HOSTCTL_RUN_DIR}/state 0755 root root -
EOF
systemd-tmpfiles --create "${TMPFILES_CONF}"
cat >"${SYSTEMD_DIR}/telemetry-hostctl.path" <<EOF
[Unit]
Description=Telemetry 中間サーバー:WebGUIからの依頼の見張り

[Path]
DirectoryNotEmpty=${HOSTCTL_RUN_DIR}/inbox

[Install]
WantedBy=multi-user.target
EOF
cat >"${SYSTEMD_DIR}/telemetry-hostctl.service" <<EOF
[Unit]
Description=Telemetry 中間サーバー:WebGUIからの依頼の処理(IPアドレスの設定、再起動、ファイアウォール)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I ${HOSTCTL} process
EOF
cat >"${SYSTEMD_DIR}/telemetry-hostctl-boot.service" <<EOF
[Unit]
Description=Telemetry 中間サーバー:起動時の処理(本体のIPアドレスの読み取り、ファイアウォールの調整)
After=network-online.target ufw.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I ${HOSTCTL} --db ${DB_PATH} boot

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now telemetry-hostctl.path >/dev/null 2>&1
systemctl enable telemetry-hostctl-boot.service >/dev/null 2>&1
systemctl start telemetry-hostctl-boot.service
[[ -f "${HOSTCTL_RUN_DIR}/state/network.json" ]] || fail "補助プログラムが本体のIPアドレスを読み取れませんでした"
result "補助プログラムを置きました(web コンテナには、特別な権限を与えていません)"

# ----------------------------------------------------------------------
# 手順9:復旧用コマンドの配置
# ----------------------------------------------------------------------
step 9 "復旧用コマンドの配置"
install -m 755 "${SCRIPT_DIR}/tools/telemetry-recover.sh" "${RECOVER_CMD}"
info "sudo telemetry-recover reset-port              WebGUIのポート番号を 8080 に戻す"
info "sudo telemetry-recover reset-password [名前]    管理者のパスワードを初期化する(省略時は admin)"
result "${RECOVER_CMD} を置きました"

# ----------------------------------------------------------------------
# 手順10:本体のIPアドレスの設定
# ----------------------------------------------------------------------
step 10 "本体のIPアドレスの設定"
info "今の設定:"
python3 -I "${HOSTCTL}" show-network || fail "本体のIPアドレスを読み取れませんでした"
current_ip="$(python3 -I "${HOSTCTL}" show-network --field ip || true)"
if yes_no "本体のIPアドレスを変更しますか?(変更しない場合は Enter)"; then
    while true; do
        new_address="$(ask "IPアドレス/プレフィックス長(例:192.0.2.10/24):")"
        new_gateway="$(ask "デフォルトゲートウェイ(例:192.0.2.1):")"
        new_dns="$(ask "DNSサーバー(2つなら空白で区切る。例:192.0.2.53):")"
        # shellcheck disable=SC2086
        checked="$(hostctl set-network --check-only --address "${new_address}" --gateway "${new_gateway}" --dns ${new_dns} \
            || echo "${FAILED_MARK}")"
        if [[ "${checked}" != *"${FAILED_MARK}"* ]]; then
            break
        fi
        info "入力をやり直してください"
    done
    read -r chk_address chk_gateway chk_dns <<<"${checked}"
    echo
    info "次の内容に変更します。"
    info "  IPアドレス/プレフィックス長:${chk_address}"
    info "  デフォルトゲートウェイ    :${chk_gateway}"
    info "  DNSサーバー             :${chk_dns}"
    info "本体のIPアドレスを変えると、全Nexusの送信先と、Zabbix側の受け付け設定も変える必要があります。"
    info "反映は、このスクリプトの最後に行います。遠隔で接続している場合は、そこで接続が切れます。"
    if yes_no "この内容で反映しますか?"; then
        # shellcheck disable=SC2086
        python3 -I "${HOSTCTL}" set-network --address "${chk_address}" --gateway "${chk_gateway}" --dns ${chk_dns}
        NEW_NETWORK="${chk_address}"
        current_ip="${chk_address%%/*}"
        result "設定ファイルを書き換えました(反映は最後に行います)"
    else
        result "変更しません"
    fi
else
    result "変更しません"
fi

# ----------------------------------------------------------------------
# 手順11:結果の表示
# ----------------------------------------------------------------------
step 11 "結果の表示"
echo
echo "  デプロイが終わりました。"
echo
echo "  WebGUIの接続先 :http://${current_ip:-(このサーバーのIPアドレス)}:${webgui_port}/"
echo "  初期アカウント :管理者 admin / パスワード admin"
echo "                  閲覧者 user  / パスワード なし(空欄)"
echo "  ※ 管理者の初期パスワードは公開されている値です。ログインしたら、すぐに変更してください。"
echo
echo "  復旧用コマンド :sudo telemetry-recover reset-port | reset-password [ユーザー名]"
echo "  ソースコードの更新:新しいソースコードをダウンロードし、もう一度 sudo bash deploy.sh を実行します。"
if [[ -n "${NEW_NETWORK}" ]]; then
    echo
    echo "  最後に、本体のIPアドレスを ${NEW_NETWORK} に変更します(3秒後)。"
    echo "  遠隔で接続している場合は、この画面の接続が切れます。新しいIPアドレスで接続し直してください。"
    # 接続が切れてもこのスクリプトの終了に左右されないよう、OS に反映を任せる
    systemd-run --quiet --on-active=3 --unit="telemetry-netplan-apply-$(date +%s)" /usr/sbin/netplan apply
fi
trap - ERR
exit 0
