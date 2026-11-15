# Zabbix telemetry 中間サーバー

Cisco Nexus が Streaming Telemetry(UDP・JSON)で送ってくるデータを受信し、Zabbix が受け取れる形に変換して送り込む「中間サーバー」です。

## 概要

- Cisco Nexus の PTP の状態と、インターフェースごとの帯域を、Zabbix で監視できるようにします。
- 対象の show コマンドは、次の4つです。
  - `show interface counters brief`(入力帯域、出力帯域)
  - `show ptp brief`(PTPポート状態)
  - `show ptp clock`(ロック状態、Offset from Master)
  - `show ptp parent`(GM-ID)
- 値は Zabbix trapper アイテムへ送ります。インターフェースごとのアイテムは、LLD(ローレベルディスカバリ)で自動登録します。
- 機器ごと・コマンドごとの受信の途絶や、中間サーバー自身の停止も、Zabbix で検知できます。
- 機器の登録や設定の変更、動作状態の確認は、WebGUI から行います。

```
[ Cisco Nexus ] --- Telemetry(UDP 50051 / JSON) ---> [ 中間サーバー ] --- Zabbix trapper(TCP 10051) ---> [ Zabbix Server 7.0 LTS ]
                                                          ^
                              [ 運用者のPC ] --- WebGUI(HTTP 8080)
```

## 構成

| コンテナ | 役割 |
| :---- | :---- |
| collector(収集エンジン) | Telemetry の受信、値の変換、Zabbix への送信、途絶の検知 |
| web(WebGUI) | 設定の変更、状態の確認、ログイン認証 |

- 動作環境:Ubuntu Server 24.04 LTS、Docker Compose(ホストネットワークモード)
- 送信先:Zabbix 7.0 LTS
- 設定とデータは、中間サーバーの `/data` に置きます。更新や再導入をしても消えません。

## 導入の方法

導入は、次の2つの操作だけで完了します。くわしい手順は [docs/導入手順書.md](docs/導入手順書.md) を見てください。

1. 中間サーバー(Ubuntu Server 24.04 LTS)で、ソースコード一式をダウンロードします。

   ```
   git clone https://github.com/Panda-Taro/Zabbix_telemetry.git
   ```

2. ダウンロードしたフォルダで、デプロイ用スクリプトを管理者権限で実行します。

   ```
   cd Zabbix_telemetry
   sudo bash deploy.sh
   ```

- 導入のときは、中間サーバーがインターネットに直接つながっている必要があります(プロキシには対応していません)。
- 実行の途中で、時刻同期(NTP)のサーバーと、本体のIPアドレスを変更するかどうかを聞かれます。
- 更新するときも、新しいソースコードをダウンロードして、もう一度 `sudo bash deploy.sh` を実行します。登録した機器や設定は消えません。

## 初期アカウント

| ユーザー名 | パスワード | 権限 |
| :---- | :---- | :---- |
| admin | admin | 管理者 |
| user | (空欄) | 閲覧者 |

**この初期アカウントは、誰でも見られるこの文書に載っている値です。導入したら、すぐに管理者(admin)のパスワードを変更してください。** WebGUI の画面右上のユーザー名を押すと、パスワード変更の画面が開きます。

## 使い方の入口

- WebGUI:ブラウザ(Google Chrome または Microsoft Edge)で `http://中間サーバーのIPアドレス:8080/` を開きます。
- 機器を追加するときの流れ、Zabbix の準備、困ったときの対処は、[docs/導入手順書.md](docs/導入手順書.md) にまとめています。
- Zabbix のテンプレートは [zabbix_templates/](zabbix_templates/) にあります。
  - `telemetry_nwsw.yaml`:各 Nexus のホストに適用する「Telemetry NWSW用テンプレート」
  - `telemetry_collector.yaml`:「telemetry」ホストに適用する「Telemetry 中間サーバー用テンプレート」
- 実機の Nexus の代わりに Telemetry を送る検証用のツールの使い方は、[docs/模擬送信ツール.md](docs/模擬送信ツール.md) にあります。

## 困ったときの復旧用コマンド

中間サーバーのコンソールで実行します。

| コマンド | 内容 |
| :---- | :---- |
| `sudo telemetry-recover reset-port` | WebGUI のポート番号を既定値の 8080 に戻します |
| `sudo telemetry-recover reset-password` | 管理者 admin のパスワードを初期化します(admin に戻ります) |
| `sudo telemetry-recover reset-password ユーザー名` | 指定した管理者のパスワードを、ユーザー名と同じ文字列に初期化します |

## フォルダの構成

| フォルダ・ファイル | 内容 |
| :---- | :---- |
| `collector/` | 収集エンジン |
| `web/` | WebGUI |
| `common/` | collector と web が共通で使う部品 |
| `tools/` | 補助プログラム、復旧用コマンド、模擬送信ツール、Zabbix テンプレートを作るプログラム |
| `zabbix_templates/` | Zabbix テンプレートファイル |
| `tests/` | 自動テストと、架空の値に置き換えたテストデータ |
| `docs/` | 導入手順書、模擬送信ツールの使い方 |
| `deploy.sh` | デプロイ用スクリプト |
| `docker-compose.yml` | Docker Compose の構成ファイル |

## 自動テスト(開発用)

開発用のPCで、Python 3.12 を使って実行します。中間サーバーには入れません。

```
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Windows の場合は、`.venv/bin/` を `.venv\Scripts\` に読み替えてください。
