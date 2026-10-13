import json
import tempfile
import unittest
from pathlib import Path

from codexbridge.speech_source import (
    SOURCE_DEFAULT,
    SOURCE_OPENAI,
    load_speech_config,
    public_speech_config,
    save_speech_config,
    validate_speech_endpoint,
)


class SpeechSourceConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "speech-source.json"

    def test_probe_treats_no_speech_as_a_reachable_endpoint(self):
        import io
        from unittest.mock import patch
        from urllib import error as urlerror

        from codexbridge.speech_source import OpenAICompatibleSpeechRecognizer

        save_speech_config(
            {
                "source": SOURCE_OPENAI,
                "endpoint": "https://example.test/v1/audio/transcriptions",
                "api_key": "secret-token",
            },
            self.path,
        )
        recognizer = OpenAICompatibleSpeechRecognizer(
            config_loader=lambda: load_speech_config(self.path)
        )

        body = b'{"error":{"message":"[doubaoime] no speech detected","type":"invalid_request_error"}}'
        failure = urlerror.HTTPError(
            "https://example.test/v1/audio/transcriptions",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(body),
        )
        with patch(
            "codexbridge.speech_source.urlrequest.urlopen",
            side_effect=failure,
        ):
            ok, message = recognizer.probe()

        self.assertTrue(ok, message)
        self.assertIn("连接成功", message)

    def test_probe_still_reports_other_http_failures(self):
        import io
        from unittest.mock import patch
        from urllib import error as urlerror

        from codexbridge.speech_source import OpenAICompatibleSpeechRecognizer

        save_speech_config(
            {
                "source": SOURCE_OPENAI,
                "endpoint": "https://example.test/v1/audio/transcriptions",
                "api_key": "secret-token",
            },
            self.path,
        )
        recognizer = OpenAICompatibleSpeechRecognizer(
            config_loader=lambda: load_speech_config(self.path)
        )

        failure = urlerror.HTTPError(
            "https://example.test/v1/audio/transcriptions",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"error":{"message":"upstream exploded"}}'),
        )
        with patch(
            "codexbridge.speech_source.urlrequest.urlopen",
            side_effect=failure,
        ):
            ok, message = recognizer.probe()

        self.assertFalse(ok)
        self.assertIn("HTTP 500", message)

    def test_defaults_to_builtin_source(self):
        config = load_speech_config(self.path)
        self.assertEqual(config["source"], SOURCE_DEFAULT)
        self.assertEqual(config["model"], "whisper-1")

    def test_saves_openai_compatible_configuration(self):
        saved = save_speech_config(
            {
                "source": SOURCE_OPENAI,
                "endpoint": "https://example.test/v1/audio/transcriptions",
                "api_key": "secret-token",
                "model": "whisper-large-v3",
                "language": "zh",
            },
            self.path,
        )
        self.assertEqual(saved["source"], SOURCE_OPENAI)
        self.assertEqual(saved["api_key"], "secret-token")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["model"], "whisper-large-v3")

    def test_public_config_redacts_api_key(self):
        save_speech_config(
            {
                "source": SOURCE_OPENAI,
                "endpoint": "https://example.test/v1/audio/transcriptions",
                "api_key": "secret-token",
            },
            self.path,
        )
        public = public_speech_config(self.path)
        self.assertNotIn("secret-token", json.dumps(public))
        self.assertTrue(public["api_key_set"])

    def test_rejects_endpoint_without_path(self):
        with self.assertRaises(ValueError):
            save_speech_config(
                {
                    "source": SOURCE_OPENAI,
                    "endpoint": "https://example.test",
                },
                self.path,
            )

    def test_endpoint_validation_requires_http(self):
        valid, reason = validate_speech_endpoint("ftp://example.test/v1/audio")
        self.assertFalse(valid)
        self.assertIn("http", reason)
