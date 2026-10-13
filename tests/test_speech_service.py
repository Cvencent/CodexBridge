import base64
import tempfile
import unittest
from pathlib import Path

from codexbridge.speech import SpeechService


class FakeJob:
    def __init__(self, text):
        self.text = text
        self.terminated = False

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return True

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class FakeRecognizer:
    last_error = ""
    recognition_timeout_seconds = 5.0

    def __init__(self, text="hello world"):
        self.text = text
        self.paths = []
        self.job = None

    def start(self, wav_path):
        self.paths.append(Path(wav_path))
        self.job = FakeJob(self.text)
        return self.job

    def result(self, process):
        return True, process.text


class SpeechServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recognizer = FakeRecognizer()
        self.service = SpeechService(
            recognizer=self.recognizer,
            output_dir=Path(self.tmp.name),
        )

    def test_recording_round_trip_emits_transcript(self):
        self.assertEqual(
            self.service.handle_event(
                "d1", {"type": "record_start", "sample_rate": 16000}
            ),
            [],
        )
        pcm = b"\x00\x00" * 16000
        self.service.handle_event(
            "d1",
            {
                "type": "audio_chunk",
                "data": base64.b64encode(pcm).decode("ascii"),
            },
        )
        self.service.handle_event("d1", {"type": "record_stop", "bytes": len(pcm)})
        messages = self.service.poll()
        self.assertEqual(
            messages,
            [("d1", {"type": "transcript", "text": "hello world"})],
        )
        self.assertTrue(self.recognizer.paths[0].is_file())

    def test_record_cancel_discards_capture(self):
        self.service.handle_event("d1", {"type": "record_start"})
        self.service.handle_event(
            "d1",
            {
                "type": "audio_chunk",
                "data": base64.b64encode(b"\x00\x00" * 16000).decode(),
            },
        )
        self.service.handle_event("d1", {"type": "record_cancel"})
        self.assertEqual(self.service.poll(), [])

    def test_incomplete_audio_is_rejected(self):
        self.service.handle_event("d1", {"type": "record_start"})
        self.service.handle_event(
            "d1",
            {
                "type": "audio_chunk",
                "data": base64.b64encode(b"\x00\x00" * 16000).decode(),
            },
        )
        messages = self.service.handle_event(
            "d1", {"type": "record_stop", "bytes": 2}
        )
        self.assertEqual(messages[0]["type"], "error")
