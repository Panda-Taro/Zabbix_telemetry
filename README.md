# Zabbix telemetry 中間サーバー

Cisco Nexus が Streaming Telemetry(UDP・JSON)で送ってくるデータを受信し、Zabbix が受け取れる形に変換して送り込む「中間サーバー」です。

## 概要

- Cisco Nexus の PTP の状態と、インターフェースごとの帯域を、Zabbix で監視できるようにします。
- 対象の show コマンドは、次の4つです。
  - `show interface counters brief`
  - `show ptp brief`
  - `show ptp clock`
  - `show ptp parent`
- 値は Zabbix trapper アイテムへ送ります。インターフェースごとのアイテムは、LLD(ローレベルディスカバリ)で自動登録します。
- 機器の登録や設定の変更、動作状態の確認は、WebGUI から行います。

## 構成

| コンテナ | 役割 |
| :---- | :---- |
| collector(収集エンジン) | Telemetry の受信、値の変換、Zabbix への送信、途絶の検知 |
| web(WebGUI) | 設定の変更、状態の確認、ログイン認証 |

- 動作環境:Ubuntu Server 24.04 LTS、Docker Compose
- 送信先:Zabbix 7.0 LTS

## 状態

開発中です。導入の方法と使い方は、完成後にこの文書と `docs/導入手順書.md` に記載します。
