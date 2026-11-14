"""受信部の一覧(4.4.1、RX-10)。

受信部は、メーカー・方式ごとに独立した部品とする。
新しい受信部(例:フェーズ2の Arista)を追加するときは、collector/ の下に部品のフォルダを作り、
create_sources() の一覧に1行加えるだけでよい。既存の受信部(cisco_nexus)は変更しない。
"""

from typing import Protocol

from collector.cisco_nexus.source import CiscoNexusUdpSource
from collector.config import CollectorConfig
from collector.runtime import Runtime


class TelemetrySource(Protocol):
    """受信部が持つもの。"""

    name: str
    device_type: str
    """この受信部が受け持つ機器の種類(devices.device_type)。"""

    async def apply_config(self, config: CollectorConfig) -> str | None:
        """設定を反映する(初回の開始を含む)。うまくいかなかった場合は、エラーの内容を返す。"""
        ...

    async def stop(self) -> None:
        """受信を止める。"""
        ...


def create_sources(runtime: Runtime) -> list[TelemetrySource]:
    """collector で動かす受信部を作る。"""
    return [
        CiscoNexusUdpSource(runtime),
    ]
