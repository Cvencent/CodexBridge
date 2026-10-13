"""Claim a device token from the loopback CodexBridge admin API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse


DEVICE_ID_PATTERN = re.compile(r"^dotii-[0-9a-f]{12}$")
MAX_RESPONSE_BYTES = 4096


@dataclass(frozen=True)
class PairingToken:
    device_id: str
    token: str = field(repr=False)
    port: int


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("Codex Bridge 地址无效")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Codex Bridge 管理接口必须使用本机回环地址")
    return base_url.rstrip("/")


def claim_codex_bridge_token(
    device_id: str,
    base_url: str = "http://127.0.0.1:8788",
    timeout: float = 2.0,
) -> PairingToken:
    if not isinstance(device_id, str) or DEVICE_ID_PATTERN.fullmatch(device_id) is None:
        raise ValueError("device_id 无效")
    root = _validate_base_url(base_url)
    request = urllib.request.Request(
        f"{root}/api/admin/pairing?{urlencode({'device_id': device_id})}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise ValueError("无法连接 Codex Bridge，请先启动 CodexBridge.exe") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Codex Bridge 配对响应过长")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Codex Bridge 返回了无效配对响应") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("Codex Bridge 未能生成配对令牌")
    if payload.get("device_id") != device_id:
        raise ValueError("Codex Bridge 返回的设备 ID 不匹配")
    token = payload.get("token")
    if not isinstance(token, str) or not 16 <= len(token) <= 64:
        raise ValueError("Codex Bridge 返回的令牌无效")
    port = payload.get("port")
    if port != 8765:
        raise ValueError("Codex Bridge 返回的设备端口无效")
    return PairingToken(device_id=device_id, token=token, port=port)
