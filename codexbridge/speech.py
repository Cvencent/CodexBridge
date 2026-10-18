from __future__ import annotations

import base64
import binascii
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_BITS = 16
MIN_RECORD_SECONDS = 0.4
MAX_RECORD_SECONDS = 120.0
MAX_AUDIO_BYTES = DEFAULT_SAMPLE_RATE * 2 * int(MAX_RECORD_SECONDS)
DEFAULT_RECOGNITION_TIMEOUT = 120.0


@dataclass
class _Capture:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    bits: int = DEFAULT_BITS
    pcm: bytearray = field(default_factory=bytearray)
    active: bool = False
    cancelled: bool = False

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * (self.bits // 8)

    @property
    def duration_seconds(self) -> float:
        if self.bytes_per_second <= 0:
            return 0.0
        return len(self.pcm) / self.bytes_per_second


@dataclass
class _Job:
    device_id: str
    process: object
    started_at: float
    canceled: bool = False


class SpeechService:
    """Collect device PCM, persist WAV files, and poll async recognition."""

    def __init__(
        self,
        *,
        recognizer=None,
        output_dir: Path | None = None,
        recognition_timeout: float = DEFAULT_RECOGNITION_TIMEOUT,
    ) -> None:
        if recognizer is None:
            from codexbridge.speech_source import create_speech_recognizer

            recognizer = create_speech_recognizer()
        self.recognizer = recognizer
        self.output_dir = (
            Path(output_dir)
            if output_dir is not None
            else Path(__file__).resolve().parent / "runtime"
        )
        self.recognition_timeout = max(0.1, float(recognition_timeout))
        self._captures: dict[str, _Capture] = {}
        self._jobs: dict[str, _Job] = {}
        self._messages: list[tuple[str, dict]] = []
        self._lock = threading.RLock()
        self._counter = 0

    def handle_event(self, device_id: str, event: dict) -> list[dict]:
        if not isinstance(event, dict):
            return []
        event_type = event.get("type")
        if event_type == "record_start":
            return self.record_start(device_id, event)
        if event_type == "audio_chunk":
            return self.audio_chunk(device_id, event)
        if event_type == "record_stop":
            return self.record_stop(device_id, event)
        if event_type == "record_cancel":
            return self.record_cancel(device_id)
        return []

    def record_start(self, device_id: str, event: dict | None = None) -> list[dict]:
        event = event or {}
        try:
            sample_rate = int(event.get("sample_rate") or DEFAULT_SAMPLE_RATE)
            channels = int(event.get("channels") or DEFAULT_CHANNELS)
            bits = int(event.get("bits") or DEFAULT_BITS)
        except (TypeError, ValueError):
            return self._error(device_id, "录音格式无效")
        if (
            not 8000 <= sample_rate <= 48000
            or channels != 1
            or bits != 16
        ):
            return self._error(
                device_id,
                "设备发来了不支持的音频格式，需要 16-bit 单声道 PCM",
            )
        with self._lock:
            self.record_cancel(device_id)
            self._captures[device_id] = _Capture(
                sample_rate=sample_rate,
                channels=channels,
                bits=bits,
                active=True,
            )
        return []

    def audio_chunk(self, device_id: str, event: dict) -> list[dict]:
        encoded = event.get("data")
        if not isinstance(encoded, str):
            return self._error(device_id, "音频分片缺少数据")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return self._error(device_id, "音频分片损坏")
        with self._lock:
            capture = self._captures.get(device_id)
            if capture is None:
                # A brief network reconnect may happen after record_start.
                capture = _Capture(active=True)
                self._captures[device_id] = capture
            if not capture.active:
                return []
            if len(capture.pcm) + len(chunk) > MAX_AUDIO_BYTES:
                capture.active = False
                return self._error(device_id, "录音超过 120 秒限制")
            capture.pcm.extend(chunk)
        return []

    def record_stop(self, device_id: str, event: dict | None = None) -> list[dict]:
        event = event or {}
        with self._lock:
            capture = self._captures.get(device_id)
            if capture is None or capture.cancelled:
                self._captures.pop(device_id, None)
                return []
            capture.active = False
            expected = event.get("bytes")
            if expected is not None and (
                not isinstance(expected, int) or expected != len(capture.pcm)
            ):
                capture.cancelled = True
                self._captures.pop(device_id, None)
                return self._error(device_id, "录音传输不完整，请重新录音")
            if capture.duration_seconds < MIN_RECORD_SECONDS:
                capture.cancelled = True
                self._captures.pop(device_id, None)
                return self._error(
                    device_id,
                    "录音太短，请至少说 0.4 秒后再按结束",
                )
            wav_path = self._write_wav(device_id, capture)
            self._captures.pop(device_id, None)
            self._cancel_job(device_id)

        try:
            process = self.recognizer.start(wav_path)
        except Exception as error:  # noqa: BLE001 - report recognizer startup
            return self._error(device_id, str(error) or "无法开始语音识别")
        with self._lock:
            self._jobs[device_id] = _Job(
                device_id=device_id,
                process=process,
                started_at=time.monotonic(),
            )
        return []

    def record_cancel(self, device_id: str) -> list[dict]:
        with self._lock:
            capture = self._captures.pop(device_id, None)
            if capture is not None:
                capture.cancelled = True
                capture.pcm.clear()
            self._cancel_job(device_id)
        return []

    def transcribe_pcm(
        self,
        device_id: str,
        pcm: bytes,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        bits: int = DEFAULT_BITS,
    ) -> str:
        """Synchronously transcribe one complete PCM buffer.

        Used by the CBDP ``AUDIO_*`` transaction, where the device expects a
        single ``RESULT`` with the transcript instead of the asynchronous
        record_start/audio_chunk/record_stop flow.
        """

        if not pcm:
            return ""
        if channels != 1 or bits != 16 or not 8000 <= int(sample_rate) <= 48000:
            raise ValueError("unsupported audio format")
        if len(pcm) % 2:
            pcm = pcm[:-1]
        capture = _Capture(
            sample_rate=int(sample_rate),
            channels=int(channels),
            bits=int(bits),
            pcm=bytearray(pcm),
            active=False,
        )
        path = self._write_wav(device_id, capture)
        process = None
        try:
            process = self.recognizer.start(path)
            deadline = time.monotonic() + self._timeout()
            while True:
                poll = getattr(process, "poll", None)
                if callable(poll) and poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    self._terminate(process)
                    raise TimeoutError("speech recognition timed out")
                time.sleep(0.05)
            ok, text = self.recognizer.result(process)
        except Exception:
            self._terminate(process)
            raise
        if not ok:
            raise RuntimeError(str(text or "speech recognition failed"))
        return str(text or "").strip()

    def poll(self) -> list[tuple[str, dict]]:
        with self._lock:
            jobs = list(self._jobs.items())
        now = time.monotonic()
        for device_id, job in jobs:
            if job.canceled:
                continue
            poll = getattr(job.process, "poll", None)
            completed = poll() is not None if callable(poll) else None
            timed_out = now - job.started_at >= self._timeout()
            if completed is None and timed_out:
                self._terminate(job.process)
                with self._lock:
                    self._jobs.pop(device_id, None)
                self._append(
                    device_id,
                    {"type": "error", "text": "语音识别超时，请重新录音"},
                )
                continue
            if completed or (timed_out and completed is not None):
                with self._lock:
                    self._jobs.pop(device_id, None)
                if timed_out and completed is False:
                    self._terminate(job.process)
                    self._append(
                        device_id,
                        {"type": "error", "text": "语音识别超时，请重新录音"},
                    )
                    continue
                try:
                    ok, text = self.recognizer.result(job.process)
                except Exception as error:  # noqa: BLE001 - report result failure
                    ok, text = False, str(error) or "语音识别失败"
                if ok:
                    recognized = str(text or "").strip()
                    if recognized:
                        self._append(
                            device_id,
                            {"type": "transcript", "text": recognized},
                        )
                    else:
                        self._append(
                            device_id,
                            {
                                "type": "error",
                                "text": "未识别到文字，请靠近设备后重新录音",
                            },
                        )
                else:
                    self._append(
                        device_id,
                        {"type": "error", "text": str(text or "语音识别失败")},
                    )
        with self._lock:
            messages = list(self._messages)
            self._messages.clear()
        return messages

    def wait(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self._lock:
                if not self._jobs:
                    return
            time.sleep(0.02)

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
            for capture in self._captures.values():
                capture.cancelled = True
                capture.pcm.clear()
            self._captures.clear()
        for job in jobs:
            self._terminate(job.process)

    def _write_wav(self, device_id: str, capture: _Capture) -> Path:
        if len(capture.pcm) % 2:
            del capture.pcm[-1]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._counter += 1
            serial = self._counter
        safe_id = "".join(
            character
            if character.isalnum() or character in {"-", "_"}
            else "_"
            for character in device_id
        ) or "device"
        path = self.output_dir / f"{safe_id}-{serial:06d}.wav"
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(capture.channels)
            wav_file.setsampwidth(capture.bits // 8)
            wav_file.setframerate(capture.sample_rate)
            wav_file.writeframes(capture.pcm)
        return path

    def _timeout(self) -> float:
        configured = getattr(
            self.recognizer,
            "recognition_timeout_seconds",
            self.recognition_timeout,
        )
        try:
            value = float(configured)
        except (TypeError, ValueError):
            return self.recognition_timeout
        return value if value > 0 else self.recognition_timeout

    def _cancel_job(self, device_id: str) -> None:
        job = self._jobs.pop(device_id, None)
        if job is not None:
            job.canceled = True
            self._terminate(job.process)

    @staticmethod
    def _terminate(process) -> None:
        for method_name in ("terminate", "kill"):
            method = getattr(process, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:  # noqa: BLE001 - best-effort cancellation
                    pass
                return

    def _error(self, device_id: str, text: str) -> list[dict]:
        message = {"type": "error", "text": text}
        self._append(device_id, message)
        return [message]

    def _append(self, device_id: str, message: dict) -> None:
        with self._lock:
            self._messages.append((device_id, message))

