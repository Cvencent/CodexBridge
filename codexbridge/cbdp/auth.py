from __future__ import annotations

import hashlib
import hmac as hmac_module
import secrets


def _token_bytes(token: str | bytes) -> bytes:
    return token if isinstance(token, bytes) else str(token).encode("utf-8")


def hmac_auth_proof(
    token: str | bytes,
    session_id: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    device_id: str,
) -> bytes:
    if len(session_id) != 16 or len(client_nonce) != 16 or len(server_nonce) != 16:
        raise ValueError("nonce and session_id must be 16 bytes")
    data = (
        b"CBDP1-AUTH"
        + bytes(session_id)
        + bytes(client_nonce)
        + bytes(server_nonce)
        + str(device_id).encode("utf-8")
    )
    return hmac_module.new(_token_bytes(token), data, hashlib.sha256).digest()


def pairing_token_proof(
    token: str | bytes,
    session_id: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    device_id: str,
) -> bytes:
    if len(session_id) != 16 or len(client_nonce) != 16 or len(server_nonce) != 16:
        raise ValueError("nonce and session_id must be 16 bytes")
    payload = (
        b"CBDP1-TOKEN"
        + _token_bytes(token)
        + bytes(session_id)
        + bytes(client_nonce)
        + bytes(server_nonce)
        + str(device_id).encode("utf-8")
    )
    return hashlib.sha256(payload).digest()


def verify_hmac_auth(
    proof: bytes,
    token: str | bytes,
    session_id: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    device_id: str,
) -> bool:
    expected = hmac_auth_proof(
        token,
        session_id,
        client_nonce,
        server_nonce,
        device_id,
    )
    return hmac_module.compare_digest(expected, bytes(proof))


def verify_pairing_token(
    proof: bytes,
    token: str | bytes,
    session_id: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    device_id: str,
) -> bool:
    expected = pairing_token_proof(
        token,
        session_id,
        client_nonce,
        server_nonce,
        device_id,
    )
    return hmac_module.compare_digest(expected, bytes(proof))
