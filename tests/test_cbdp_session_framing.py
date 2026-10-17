import socket
import unittest
from unittest.mock import patch

from codexbridge.cbdp.constants import TYPE_PING
from codexbridge.cbdp.frame import Frame, encode_frame
from codexbridge.devices.session import CbdpDeviceSession


class _SocketStub:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.shutdown_called = False

    def setsockopt(self, *_args):
        return None

    def fileno(self):
        return 0

    def recv(self, _size):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def sendall(self, payload):
        self.sent.append(bytes(payload))

    def shutdown(self, _how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


class _SubscriptionsStub:
    def __init__(self):
        self.dropped = []

    def drop_device(self, device_id):
        self.dropped.append(device_id)


class _GatewayStub:
    def __init__(self):
        self.frames = []
        self.started = 0
        self.session = None
        self.subscriptions = _SubscriptionsStub()

    def bind_transport(self, transport):
        self.transport = transport

    def begin_session(self):
        self.started += 1
        return object()

    def handle_frame(self, frame):
        self.frames.append(frame)


class CbdpSessionFramingTests(unittest.TestCase):
    def test_session_drains_fragmented_and_coalesced_frames(self):
        first = encode_frame(Frame(message_type=TYPE_PING, header={0x0097: b"\x00\x00\x00\x01"}))
        second = encode_frame(Frame(message_type=TYPE_PING, header={0x0097: b"\x00\x00\x00\x02"}))
        sock = _SocketStub([first[:5], first[5:] + second])
        gateway = _GatewayStub()
        session = CbdpDeviceSession(
            "127.0.0.1",
            8765,
            "abcdefghijklmnop",
            device_id="dev-1",
            gateway=gateway,
        )

        with patch("codexbridge.devices.session.socket.create_connection", return_value=sock):
            session.run_once()

        self.assertEqual([frame.header[0x0097] for frame in gateway.frames], [b"\x00\x00\x00\x01", b"\x00\x00\x00\x02"])
        self.assertEqual(gateway.started, 1)

    def test_session_cleanup_uses_hello_device_id(self):
        class _IdentitySession:
            ready = False
            authenticated = False
            device_id = "renamed-device"

        gateway = _GatewayStub()
        gateway.session = _IdentitySession()
        session = CbdpDeviceSession(
            "127.0.0.1",
            8765,
            "abcdefghijklmnop",
            device_id="dialed-device",
            gateway=gateway,
        )
        sock = _SocketStub([])

        with patch("codexbridge.devices.session.socket.create_connection", return_value=sock):
            session.run_once()

        self.assertEqual(gateway.subscriptions.dropped, ["renamed-device"])

    def test_send_is_binary_cbdp_not_json(self):
        gateway = _GatewayStub()
        session = CbdpDeviceSession(
            "127.0.0.1",
            8765,
            "abcdefghijklmnop",
            device_id="dev-1",
            gateway=gateway,
        )
        sock = _SocketStub([])
        session._socket = sock
        frame = Frame(message_type=TYPE_PING)

        session._send_frame(frame)

        self.assertEqual(sock.sent, [encode_frame(frame)])
        self.assertFalse(sock.sent[0].startswith(b"{"))


if __name__ == "__main__":
    unittest.main()
