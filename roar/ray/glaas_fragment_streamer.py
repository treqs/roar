from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


class GlaasFragmentStreamer:
    def __init__(
        self,
        session_id: str,
        token: str,
        glaas_url: str,
        flush_threshold: int = 50,
    ) -> None:
        self._session_id = session_id
        self._token = token
        self._glaas_url = glaas_url.rstrip("/")
        self._flush_threshold = max(1, int(flush_threshold))
        self._buffer: list[dict[str, Any]] = []
        self._next_sequence = 0

    def append_fragment(self, fragment_dict: dict[str, Any]) -> None:
        self._buffer.append(fragment_dict)
        if len(self._buffer) >= self._flush_threshold:
            self.flush()

    def flush(self) -> bool:
        if not self._buffer:
            return True

        try:
            plaintext = json.dumps(self._buffer, separators=(",", ":")).encode("utf-8")
            key = bytes.fromhex(self._token)
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
            encrypted_batch = base64.b64encode(nonce + ciphertext).decode("ascii")

            request = urllib.request.Request(
                url=(
                    f"{self._glaas_url}/api/v1/fragments/sessions/"
                    f"{self._session_id}/fragments"
                ),
                data=json.dumps(
                    {
                        "encrypted_batch": encrypted_batch,
                        "sequence": self._next_sequence,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-roar-fragment-token": self._token,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
        except Exception as exc:
            _get_logger().warning(
                "Failed to stream Ray fragments for session %s sequence %d: %s",
                self._session_id,
                self._next_sequence,
                exc,
            )
            return False

        self._buffer.clear()
        self._next_sequence += 1
        return True

    def close(self) -> None:
        self.flush()
