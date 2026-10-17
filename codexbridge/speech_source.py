#!/usr/bin/env python3
"""Speech source configuration and recognizer implementations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import tempfile
import threading
import wave
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


SOURCE_DEFAULT = "default"
SOURCE_OPENAI = "openai"
SOURCE_LABELS = {
    SOURCE_DEFAULT: "内置识别",
    SOURCE_OPENAI: "OpenAI 兼容接口",
}

DEFAULT_MODEL = "whisper-1"
DEFAULT_LANGUAGE = "zh"
DEFAULT_TIMEOUT_SECONDS = 90
VOSK_MODEL_DIRNAME = "vosk-model-small-cn-0.22"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_VOSK_MODELS: dict[str, object] = {}
_VOSK_LOCK = threading.Lock()


def _state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "CodexBridge"


def speech_source_path() -> Path:
    return _state_dir() / "speech-source.json"


def normalize_speech_source(source: object) -> str:
    value = str(source or "").strip().lower()
    return value if value in SOURCE_LABELS else SOURCE_DEFAULT


def validate_speech_endpoint(endpoint: object) -> tuple[bool, str]:
    value = str(endpoint or "").strip()
    if not value:
        return False, "请先填写 OpenAI 兼容接口地址"
    if any(character.isspace() for character in value):
        return False, "接口地址不能包含空白字符"
    try:
        parsed = urlparse.urlsplit(value)
    except ValueError as error:
        return False, f"接口地址无效：{error}"
    if parsed.scheme not in {"http", "https"}:
        return False, "接口地址必须以 http:// 或 https:// 开头"
    if not parsed.netloc:
        return False, "接口地址缺少主机名"
    if not parsed.path or parsed.path == "/":
        return False, "接口地址需要包含完整的 /v1/audio/transcriptions 路径"
    return True, ""


def _normalize_timeout(value: object) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return timeout if 1 <= timeout <= 600 else DEFAULT_TIMEOUT_SECONDS


def _default_config() -> dict:
    return {
        "source": SOURCE_DEFAULT,
        "endpoint": "",
        "api_key": "",
        "model": DEFAULT_MODEL,
        "language": DEFAULT_LANGUAGE,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


def _normalize_config(data: object) -> dict:
    config = _default_config()
    if isinstance(data, str):
        config["source"] = normalize_speech_source(data)
        return config
    if not isinstance(data, dict):
        return config

    config["source"] = normalize_speech_source(data.get("source"))
    config["endpoint"] = str(data.get("endpoint") or "").strip()
    config["api_key"] = str(data.get("api_key") or "").strip()
    config["model"] = (
        str(data.get("model") or "").strip() or DEFAULT_MODEL
    )
    config["language"] = (
        str(data.get("language") or "").strip() or DEFAULT_LANGUAGE
    )
    config["timeout_seconds"] = _normalize_timeout(
        data.get("timeout_seconds")
    )
    return config


def load_speech_config(path: Path | None = None) -> dict:
    config_path = Path(path) if path is not None else speech_source_path()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _default_config()
    return _normalize_config(data)


def prepare_speech_config(
    config: dict,
    path: Path | None = None,
) -> dict:
    config_path = Path(path) if path is not None else speech_source_path()
    existing = load_speech_config(config_path)
    normalized = _normalize_config(config)
    clear_api_key = (
        bool(config.get("clear_api_key")) if isinstance(config, dict) else False
    )
    if not normalized["api_key"] and existing["api_key"] and not clear_api_key:
        normalized["api_key"] = existing["api_key"]
    if clear_api_key:
        normalized["api_key"] = ""

    endpoint = normalized["endpoint"]
    if endpoint:
        valid, reason = validate_speech_endpoint(endpoint)
        if not valid:
            raise ValueError(reason)
    return normalized


def save_speech_config(
    config: dict,
    path: Path | None = None,
) -> dict:
    config_path = Path(path) if path is not None else speech_source_path()
    normalized = prepare_speech_config(config, config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return normalized


def public_speech_config(path: Path | None = None) -> dict:
    config = load_speech_config(path)
    public = dict(config)
    public["api_key_set"] = bool(config["api_key"])
    public.pop("api_key", None)
    return public


def _endpoint_label(endpoint: str) -> str:
    try:
        parsed = urlparse.urlsplit(endpoint)
    except ValueError:
        return endpoint
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"


def _encode_multipart(
    fields: dict[str, str],
    file_path: Path,
    boundary: str,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        text = str(value or "")
        if not text:
            continue
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{name}"\r\n'
                    "\r\n"
                ).encode("ascii"),
                text.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="file"; '
                'filename="speech.wav"\r\n'
                "Content-Type: audio/wav\r\n"
                "\r\n"
            ).encode("ascii"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks)


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def _error_message(raw: bytes, content_type: str = "") -> str:
    text = _decode_body(raw)
    if text and ("json" in content_type.lower() or text[:1] in {"{", "["}):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    if message:
                        return message[:300]
                message = str(payload.get("message") or "").strip()
                if message:
                    return message[:300]
    return text[:300] or "服务器未返回错误详情"


PROBE_EXPECTED_SILENCE_MARKERS = (
    "no speech detected",
    "no speech was recognised",
    "no speech was recognized",
)


def _is_expected_silence_probe_error(code: int, detail: str) -> bool:
    if code != 422:
        return False
    lowered = detail.lower()
    return any(marker in lowered for marker in PROBE_EXPECTED_SILENCE_MARKERS)


def _parse_transcription_response(raw: bytes, content_type: str = "") -> str:
    text = _decode_body(raw)
    if not text:
        raise RuntimeError("接口未返回文字")
    if "json" in content_type.lower() or text[:1] in {"{", "["}:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeError("接口返回的 JSON 无效") from error
        if not isinstance(payload, dict):
            raise RuntimeError("接口响应格式异常")
        result = str(payload.get("text") or "").strip()
        if not result:
            raise RuntimeError("接口未返回文字")
        return result
    return text


class _RecognitionJob:
    def __init__(self, worker, *args) -> None:
        self._done = threading.Event()
        self._result: tuple[bool, str] = (False, "识别任务未完成")
        self._thread = threading.Thread(
            target=self._run,
            args=(worker, args),
            daemon=True,
        )
        self._thread.start()

    def _run(self, worker, args) -> None:
        try:
            self._result = (True, worker(*args))
        except Exception as error:  # noqa: BLE001 - expose async failure to caller
            self._result = (False, str(error) or error.__class__.__name__)
        finally:
            self._done.set()

    def outcome(self) -> tuple[bool, str]:
        self._done.wait()
        return self._result

    def poll(self):
        return True if self._done.is_set() else None

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def terminate(self) -> None:
        # HTTP requests cannot be safely interrupted. The configured timeout
        # bounds them, and SpeechService drops late results after cancellation.
        pass

    def kill(self) -> None:
        self.terminate()


class OpenAICompatibleSpeechRecognizer:
    engine = "openai-compatible"

    # A live round-trip is the only way to know the endpoint really transcribes:
    # a gateway can answer /models with 24 chat models and still 404 every audio
    # request. The check is cached so the tray, settings page and every
    # recognition never pay for it repeatedly.
    PROBE_TTL_SECONDS = 300.0
    PROBE_TIMEOUT_SECONDS = 10

    def __init__(self, config_loader=load_speech_config) -> None:
        self._config_loader = config_loader
        self.last_error = ""
        self._probe: tuple[bool, str] | None = None
        self._probe_at = 0.0
        self._probe_key = ""
        self._probe_lock = threading.Lock()
        self._probing = False

    def _config(self) -> dict:
        return _normalize_config(self._config_loader())

    @staticmethod
    def _signature(config: dict) -> str:
        # Identity of "the same endpoint we already proved". A new model name, a
        # different gateway or a saved API key must all force a fresh probe,
        # otherwise a verdict earned by one configuration gets reused by another.
        return "|".join(
            [
                str(config.get("endpoint") or ""),
                str(config.get("model") or ""),
                str(config.get("language") or ""),
                str(hash(str(config.get("api_key") or ""))),
            ]
        )

    def invalidate_probe(self) -> None:
        with self._probe_lock:
            self._probe = None
            self._probe_at = 0.0
            self._probe_key = ""

    @property
    def recognition_timeout_seconds(self) -> float:
        return float(self._config()["timeout_seconds"]) + 5.0

    def available(self) -> tuple[bool, str]:
        """Honest availability, without making a recording wait for a probe.

        "Configured" is not "working": a gateway can list two dozen chat models
        and still 404 every audio request, which is how the device ended up
        silently transcribing through a fallback while the user only saw wrong
        text. A proven verdict -- good or bad -- is reused for
        ``PROBE_TTL_SECONDS``. While nothing is known the endpoint is assumed
        usable and a background probe settles it, so no take is delayed by the
        check itself.
        """
        config = self._config()
        valid, reason = validate_speech_endpoint(config["endpoint"])
        if not valid:
            return False, reason
        label = (
            f"OpenAI 兼容接口已配置：{_endpoint_label(config['endpoint'])}"
            f" · {config['model']}"
        )
        key = self._signature(config)
        with self._probe_lock:
            verdict = self._probe
            fresh = (
                verdict is not None
                and self._probe_key == key
                and (time.monotonic() - self._probe_at) < self.PROBE_TTL_SECONDS
            )
            start_probe = (not fresh) and (not self._probing)
            if start_probe:
                self._probing = True
        if fresh and verdict is not None:
            ok, detail = verdict
            if not ok:
                return False, f"接口实测不可用：{detail}"
            return True, label
        if start_probe:
            threading.Thread(
                target=self._background_probe,
                args=(key,),
                name="speech-probe",
                daemon=True,
            ).start()
        return True, label

    def _background_probe(self, key: str) -> None:
        try:
            ok, detail = self._run_probe()
        except Exception as error:  # noqa: BLE001 - the probe is best effort
            ok, detail = False, str(error) or error.__class__.__name__
        with self._probe_lock:
            if self._probe_key == key or not self._probe_key:
                self._probe = (ok, detail)
                self._probe_at = time.monotonic()
                self._probe_key = key
            self._probing = False

    def _run_probe(self) -> tuple[bool, str]:
        """Round-trip with one second of silence to learn the real answer."""
        config = self._config()
        valid, reason = validate_speech_endpoint(config["endpoint"])
        if not valid:
            return False, reason
        with tempfile.TemporaryDirectory(prefix="codexbridge-probe-") as temp_dir:
            path = Path(temp_dir) / "probe.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 8000)
            try:
                self._request(path, timeout=self.PROBE_TIMEOUT_SECONDS)
            except Exception as error:  # noqa: BLE001 - report, never raise
                detail = str(error)
                if not (
                    "HTTP 422" in detail
                    and _is_expected_silence_probe_error(422, detail)
                ):
                    self.last_error = detail
                    return False, self.last_error
        self.last_error = ""
        return True, f"连接成功：{_endpoint_label(config['endpoint'])}"


    def _request(self, wav_path: Path, timeout: float | None = None):
        config = self._config()
        endpoint = config["endpoint"]
        valid, reason = validate_speech_endpoint(endpoint)
        if not valid:
            raise RuntimeError(reason)
        if timeout is not None:
            config["timeout_seconds"] = timeout
        source_path = Path(wav_path)
        if not source_path.is_file():
            raise RuntimeError(f"录音文件不存在：{source_path}")

        fields = {
            "model": config["model"],
            "language": config["language"],
            "response_format": "json",
        }
        boundary = secrets.token_hex(16)
        body = _encode_multipart(fields, source_path, boundary)
        headers = {
            "Accept": "application/json, text/plain",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "CodexBridge/0.1",
        }
        if config["api_key"]:
            headers["Authorization"] = f"Bearer {config['api_key']}"
        request = urlrequest.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(
                request,
                timeout=config["timeout_seconds"],
            ) as response:
                return response.read(), response.headers.get("Content-Type", "")
        except urlerror.HTTPError as error:
            raw = error.read()
            detail = _error_message(raw, error.headers.get("Content-Type", ""))
            raise RuntimeError(f"接口返回 HTTP {error.code}：{detail}") from error
        except (OSError, urlerror.URLError) as error:
            raise RuntimeError(f"无法连接接口：{error}") from error

    def transcribe(self, wav_path: Path) -> str:
        raw, content_type = self._request(Path(wav_path))
        text = _parse_transcription_response(raw, content_type)
        self.last_error = ""
        return text

    def probe(self) -> tuple[bool, str]:
        """Explicit test behind the settings page "测试" button.

        The verdict is cached under the current signature, so an endpoint that
        just passed a manual test is not probed again by ``available()`` until
        the TTL expires.
        """
        key = self._signature(self._config())
        ok, detail = self._run_probe()
        with self._probe_lock:
            self._probe = (ok, detail)
            self._probe_at = time.monotonic()
            self._probe_key = key
        return ok, detail

    def start(self, wav_path: Path):
        return _RecognitionJob(self.transcribe, wav_path)

    def result(self, process) -> tuple[bool, str]:
        ok, text = process.outcome()
        self.last_error = "" if ok else text
        return ok, text


def _vosk_model_candidates(tools_dir: Path):
    explicit = os.environ.get("CODEXBRIDGE_VOSK_MODEL")
    if explicit:
        yield Path(explicit)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / VOSK_MODEL_DIRNAME
    yield tools_dir / VOSK_MODEL_DIRNAME
    yield tools_dir.parent / "models" / VOSK_MODEL_DIRNAME
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        yield executable_dir / VOSK_MODEL_DIRNAME
        yield executable_dir / "models" / VOSK_MODEL_DIRNAME
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        yield Path(local_appdata) / "CodexBridge" / "models" / VOSK_MODEL_DIRNAME


def find_vosk_model(tools_dir: Path) -> Path | None:
    for candidate in _vosk_model_candidates(Path(tools_dir)):
        if (candidate / "am").is_dir() and (candidate / "conf").is_dir():
            return candidate
    return None


def _shared_vosk_model(model_path: str, vosk_module):
    with _VOSK_LOCK:
        model = _VOSK_MODELS.get(model_path)
        if model is None:
            model = vosk_module.Model(model_path)
            _VOSK_MODELS[model_path] = model
        return model


class WindowsSpeechRecognizer:
    """Offline recognizer using bundled Vosk or Windows System.Speech."""

    last_error = ""

    def __init__(self, tools_dir: Path, locale: str = "zh-CN") -> None:
        self.script = Path(tools_dir) / "windows_speech_recognize.ps1"
        self.locale = locale
        self.model_dir = find_vosk_model(Path(tools_dir))
        self._vosk = None
        self._vosk_probed = False

    @property
    def engine(self) -> str:
        return "vosk" if self._load_vosk() is not None else "system-speech"

    @property
    def recognition_timeout_seconds(self) -> float:
        return 120.0

    def _load_vosk(self):
        if self._vosk_probed:
            return self._vosk
        self._vosk_probed = True
        if self.model_dir is None:
            return None
        try:
            import vosk

            vosk.SetLogLevel(-1)
            self._vosk = vosk
        except Exception:  # noqa: BLE001 - dependency is optional
            self._vosk = None
        return self._vosk

    def _transcribe(self, wav_path: Path) -> str:
        vosk = self._vosk
        if vosk is None or self.model_dir is None:
            raise RuntimeError("离线识别模型未就绪")
        model = _shared_vosk_model(str(self.model_dir), vosk)
        with wave.open(str(wav_path), "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                raise RuntimeError("录音格式不受设备支持")
            audio = wav_file.readframes(wav_file.getnframes())
            recognizer = vosk.KaldiRecognizer(model, wav_file.getframerate())
            for start in range(0, len(audio), 8000):
                recognizer.AcceptWaveform(audio[start:start + 8000])
            text = json.loads(recognizer.FinalResult()).get("text", "")
        return str(text).replace(" ", "").strip()

    def start(self, wav_path: Path):
        self.last_error = ""
        if self._load_vosk() is not None:
            return _RecognitionJob(self._transcribe, wav_path)
        return subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-WavPath",
                str(wav_path),
                "-Locale",
                self.locale,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )

    def result(self, process) -> tuple[bool, str]:
        if isinstance(process, _RecognitionJob):
            ok, payload = process.outcome()
            self.last_error = "" if ok else payload
            return ok, payload
        stdout, stderr = process.communicate()
        if process.returncode == 0 and stdout.strip():
            self.last_error = ""
            return True, stdout.strip().splitlines()[-1]
        detail = (
            stderr.strip().splitlines()[-1]
            if stderr.strip()
            else "未识别到文字"
        )
        self.last_error = detail
        return False, detail


class SwitchableSpeechRecognizer:
    def __init__(
        self,
        default_recognizer,
        generic_recognizer: OpenAICompatibleSpeechRecognizer,
        source: str | None = None,
    ) -> None:
        self.default_recognizer = default_recognizer
        self.generic_recognizer = generic_recognizer
        self._source = normalize_speech_source(
            load_speech_config()["source"] if source is None else source
        )
        self.last_error = ""
        self._fallback_reason = ""
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        with self._lock:
            return self._source

    @property
    def recognition_timeout_seconds(self) -> float:
        if self.source != SOURCE_OPENAI:
            return float(
                getattr(
                    self.default_recognizer,
                    "recognition_timeout_seconds",
                    0.0,
                )
            )
        try:
            return float(self.generic_recognizer.recognition_timeout_seconds)
        except (TypeError, ValueError):
            return 0.0

    @property
    def engine(self) -> str:
        if self.source == SOURCE_OPENAI:
            available, _reason = self.generic_recognizer.available()
            if available:
                return self.generic_recognizer.engine
            return f"{self.default_recognizer.engine} (remote not ready)"
        return self.default_recognizer.engine

    def available(self) -> tuple[bool, str]:
        if self.source == SOURCE_OPENAI:
            return self.generic_recognizer.available()
        return True, "内置识别可用"

    def set_source(self, source: object) -> str:
        value = save_speech_config({"source": source})["source"]
        with self._lock:
            self._source = value
            self._fallback_reason = ""
        return value

    def take_fallback_reason(self) -> str:
        with self._lock:
            reason = self._fallback_reason
            self._fallback_reason = ""
        return reason

    def _generic_then_default(self, wav_path: Path) -> str:
        try:
            return self.generic_recognizer.transcribe(wav_path)
        except Exception as error:  # noqa: BLE001 - fall back deliberately
            reason = str(error) or error.__class__.__name__
            with self._lock:
                self._fallback_reason = (
                    f"OpenAI 接口不可用，已回退内置识别：{reason}"
                )
            return self._default_transcribe(wav_path)

    def _default_transcribe(self, wav_path: Path) -> str:
        process = self.default_recognizer.start(wav_path)
        process.wait()
        ok, text = self.default_recognizer.result(process)
        if not ok:
            raise RuntimeError(text or "内置语音识别失败")
        return text

    def start(self, wav_path: Path):
        self.last_error = ""
        with self._lock:
            self._fallback_reason = ""
            source = self._source
        if source == SOURCE_DEFAULT:
            return self.default_recognizer.start(wav_path)
        available, reason = self.generic_recognizer.available()
        if not available:
            with self._lock:
                self._fallback_reason = (
                    f"OpenAI 接口不可用，已回退内置识别：{reason}"
                )
            return self.default_recognizer.start(wav_path)
        return _RecognitionJob(lambda: self._generic_then_default(wav_path))

    def result(self, process) -> tuple[bool, str]:
        if isinstance(process, _RecognitionJob):
            ok, text = process.outcome()
            self.last_error = "" if ok else text
            return ok, text
        ok, text = self.default_recognizer.result(process)
        self.last_error = "" if ok else text
        return ok, text


def create_speech_recognizer(
    tools_dir: Path | None = None,
    locale: str = "zh-CN",
) -> SwitchableSpeechRecognizer:
    directory = (
        Path(tools_dir)
        if tools_dir is not None
        else Path(__file__).resolve().parent
    )
    return SwitchableSpeechRecognizer(
        WindowsSpeechRecognizer(directory, locale),
        OpenAICompatibleSpeechRecognizer(),
    )


def probe_speech_config(
    path: Path | None = None,
    config: dict | None = None,
) -> tuple[bool, str]:
    if config is None:
        config_loader = lambda: load_speech_config(path)
    else:
        config_loader = lambda: prepare_speech_config(config, path)
    recognizer = OpenAICompatibleSpeechRecognizer(config_loader=config_loader)
    return recognizer.probe()
