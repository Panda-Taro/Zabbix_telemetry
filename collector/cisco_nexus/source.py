"""Cisco Nexus 用の受信部(NX-OS Telemetry dial-out、UDP・JSON)(RX-01、RX-10、CF-04)。"""

from collector.cisco_nexus.handler import CiscoNexusHandler
from collector.config import CollectorConfig
from collector.runtime import Runtime
from collector.transport.udp import UdpListener
from common.constants import DEVICE_TYPE_CISCO_NEXUS


class CiscoNexusUdpSource:
    """Cisco Nexus から、UDPでTelemetryを受け取る受信部。"""

    name = "Cisco Nexus(UDP・JSON)"
    device_type = DEVICE_TYPE_CISCO_NEXUS

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.handler = CiscoNexusHandler(runtime)
        self._listener: UdpListener | None = None

    @property
    def port(self) -> int | None:
        return self._listener.port if self._listener else None

    async def apply_config(self, config: CollectorConfig) -> str | None:
        """設定を反映する。受信ポート番号が変わった場合は、受け口だけを開き直す(CF-04)。

        新しいポート番号で開けなかった場合は、今の受け口のまま受信を続け、エラーの内容を返す。
        """
        port = config.telemetry_udp_port
        if self._listener is not None and self._listener.port == port:
            return None
        try:
            listener = await UdpListener.open(port, self.handler.handle, self.runtime.now_ns, self.runtime.logger)
        except OSError as exc:
            message = f"{self.name}:受信ポート番号 {port} で受信を開始できません({exc})"
            self.runtime.logger.error(message)
            return message
        if self._listener is not None:
            old_port = self._listener.port
            self._listener.close()
            self.runtime.logger.info("受信ポート番号を %d から %d に変更しました", old_port, port)
        self._listener = listener
        return None

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
