import tempfile
import threading
import unittest
from pathlib import Path


from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.devices.session import CbdpDeviceSession
from codexbridge.hub import BridgeHub


class FakeCbdpSession:
    created = None

    def __init__(self, host, port, token, **kwargs):
        self.host = host
        self.port = port
        self.token = token
        self.kwargs = kwargs
        self.stopped = threading.Event()
        FakeCbdpSession.created = self

    def run_once(self):
        self.kwargs["on_device_info"](
            {
                "device_id": "cbdp-1",
                "model": "esp32",
                "capabilities": ["status", "text"],
            }
        )
        self.kwargs["on_online"]()
        self.stopped.wait(1.0)
        self.kwargs["on_offline"]()

    def stop(self):
        self.stopped.set()


class ProtocolSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        self.registry.upsert(
            "cbdp-1",
            model="esp32",
            token="abcdefghijklmnop",
            supported=["status", "text"],
        )
        self.hub = BridgeHub(self.registry)
        self.manager = DeviceManager(
            self.registry,
            self.hub,
            session_factory=FakeCbdpSession,
        )
        self.addCleanup(self.manager.stop)

    def test_cbdp_discovery_result_uses_cbdp_session_factory(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="esp32",
            device_id="cbdp-1",
            name="Desk",
            port=8765,
            capabilities=["status", "text"],
            protocol="cbdp",
        )
        self.manager.sync([result])
        self.assertTrue(FakeCbdpSession.created is not None)
        self.assertEqual(FakeCbdpSession.created.kwargs.get("protocol"), "cbdp")


if __name__ == "__main__":
    unittest.main()