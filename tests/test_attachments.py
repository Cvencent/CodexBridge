"""Attachment storage and Codex input translation."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from codexbridge.companion.attachments import AttachmentError, AttachmentStore
from codexbridge.companion.service import CompanionService


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = AttachmentStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upload_round_trip_persists_and_deduplicates(self) -> None:
        payload = b"\x89PNG\r\n\x1a\nfake-image"
        digest = hashlib.sha256(payload).hexdigest()
        upload = self.store.begin_upload(
            thread_id="t1",
            kind="image",
            byte_size=len(payload),
            content_sha256=digest,
            filename="photo.png",
            mime_type="image/png",
        )
        self.store.upload_chunk(upload["upload_id"], 0, payload[:5])
        self.store.upload_chunk(upload["upload_id"], 5, payload[5:])
        record = self.store.end_upload(upload["upload_id"], byte_size=len(payload))

        self.assertEqual(record["kind"], "image")
        self.assertEqual(record["filename"], "photo.png")
        self.assertTrue(self.store.data_url(record["attachment_id"]).startswith("data:image/png;base64,"))

        reopened = AttachmentStore(Path(self.temp.name))
        self.assertIsNotNone(reopened.get(record["attachment_id"]))
        self.assertEqual(
            [row["attachment_id"] for row in reopened.list("t1")["attachments"]],
            [record["attachment_id"]],
        )

    def test_hash_mismatch_is_rejected(self) -> None:
        payload = b"not-the-declared-bytes"
        upload = self.store.begin_upload(
            thread_id="t1",
            kind="file",
            byte_size=len(payload),
            content_sha256="0" * 64,
            filename="a.bin",
            mime_type="application/octet-stream",
        )
        self.store.upload_chunk(upload["upload_id"], 0, payload)
        with self.assertRaises(AttachmentError) as caught:
            self.store.end_upload(upload["upload_id"])
        self.assertEqual(caught.exception.code, "CONTENT_HASH_MISMATCH")

    def test_image_kind_requires_image_mime(self) -> None:
        with self.assertRaises(AttachmentError) as caught:
            self.store.begin_upload(
                thread_id="t1",
                kind="image",
                byte_size=3,
                filename="a.txt",
                mime_type="text/plain",
            )
        self.assertEqual(caught.exception.code, "ATTACHMENT_KIND_MISMATCH")


class InputPartTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: None,
        )

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def _image(self) -> str:
        payload = b"image-bytes"
        upload = self.service.attachments.begin_upload(
            thread_id="t1",
            kind="image",
            byte_size=len(payload),
            filename="p.png",
            mime_type="image/png",
        )
        self.service.attachments.upload_chunk(upload["upload_id"], 0, payload)
        return self.service.attachments.end_upload(upload["upload_id"])["attachment_id"]

    def test_image_part_becomes_inline_input_image(self) -> None:
        attachment_id = self._image()

        inputs = self.service._codex_inputs(
            "看图",
            [{"type": "text", "text": "看图"}, {"type": "image", "attachment_id": attachment_id}],
        )

        self.assertEqual(inputs[0], {"type": "text", "text": "看图"})
        self.assertEqual(inputs[1]["type"], "image")
        self.assertTrue(inputs[1]["url"].startswith("data:image/png;base64,"))

    def test_missing_attachment_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service._codex_inputs("", [{"type": "image", "attachment_id": "missing"}])


if __name__ == "__main__":
    unittest.main()
