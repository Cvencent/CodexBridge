import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codexbridge.cbdp.constants import TYPE_DELTA, TYPE_PING
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
        self.allowed_subscriptions = set()

    def bind_transport(self, transport):
        self.transport = transport

    def begin_session(self):
        self.started += 1
        return object()

    def handle_frame(self, frame):
        self.frames.append(frame)

    def has_subscription(self, topic_id, scope):
        return (int(topic_id), int(scope)) in self.allowed_subscriptions

    def delta_frame(self, event):
        return Frame(message_type=TYPE_DELTA, topic_id=int(event.topic_id))


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

    def test_send_event_drops_unsubscribed_topic(self):
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
        event = SimpleNamespace(topic_id=0x10000008, event_type=0x0301)

        sent = session.send_event(event)

        self.assertFalse(sent)
        self.assertEqual(sock.sent, [])

    def test_send_event_emits_matching_subscription(self):
        gateway = _GatewayStub()
        gateway.allowed_subscriptions.add((0x10000008, 1))
        session = CbdpDeviceSession(
            "127.0.0.1",
            8765,
            "abcdefghijklmnop",
            device_id="dev-1",
            gateway=gateway,
        )
        sock = _SocketStub([])
        session._socket = sock
        event = SimpleNamespace(topic_id=0x10000008, event_type=0x0301)

        sent = session.send_event(event)

        self.assertTrue(sent)
        self.assertEqual(len(sock.sent), 1)


if __name__ == "__main__":
    unittest.main()
