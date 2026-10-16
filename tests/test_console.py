import contextlib
import io
import unittest

from codexbridge.console import emit


class StrictConsole(io.TextIOBase):
    """A console that rejects whatever its code page cannot encode."""

    encoding = "cp1252"

    def __init__(self):
        self.written = []

    def write(self, text):
        text.encode(self.encoding)
        self.written.append(text)
        return len(text)

    def flush(self):
        pass


class ConsoleEmitTest(unittest.TestCase):
    def test_replaces_characters_the_console_cannot_encode(self):
        stream = StrictConsole()
        with contextlib.redirect_stdout(stream):
            emit("Codex 桌面共享连接不可用")
        self.assertIn("Codex", "".join(stream.written))

    def test_passes_text_through_when_the_console_accepts_it(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            emit("Codex 桌面共享连接不可用")
        self.assertEqual(stream.getvalue(), "Codex 桌面共享连接不可用\n")


if __name__ == "__main__":
    unittest.main()
