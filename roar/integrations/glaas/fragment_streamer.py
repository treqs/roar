from __future__ import annotations

import base64
import copy
import json
import os
import urllib.error
import urllib.request
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_logger():
    from ...core.logging import get_logger

    return get_logger()


DEFAULT_RENEW_TTL_SECONDS = 86400


def renew_fragment_session(
    glaas_url: str,
    session_id: str,
    token: str,
    ttl_seconds: int = DEFAULT_RENEW_TTL_SECONDS,
) -> bool:
    """Extend a fragment session's expiry to now + ttl_seconds.

    GLaaS allows renewal of an already-expired session (the token proves
    ownership), so callers can retry once after a 403 instead of tracking
    the expiry clock themselves.
    """
    try:
        request = urllib.request.Request(
            url=f"{glaas_url.rstrip('/')}/api/v1/fragments/sessions/{session_id}/renew",
            data=json.dumps({"ttl_seconds": int(ttl_seconds)}, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers={
                "content-type": "application/json",
                "x-roar-fragment-token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5):
            pass
    except Exception as exc:
        _get_logger().warning("Failed to renew fragment session %s: %s", session_id, exc)
        return False
    return True


class GlaasFragmentStreamer:
    def __init__(
        self,
        session_id: str,
        token: str,
        glaas_url: str,
        flush_threshold: int = 50,
        renew_ttl_seconds: int = DEFAULT_RENEW_TTL_SECONDS,
    ) -> None:
        self._session_id = session_id
        self._token = token
        self._glaas_url = glaas_url.rstrip("/")
        self._flush_threshold = max(1, int(flush_threshold))
        self._renew_ttl_seconds = renew_ttl_seconds
        self._buffer: list[dict[str, Any]] = []
        self._next_sequence = 0
        self.delivered_batches = 0
        self.failed_batches = 0

    @property
    def pending_fragments(self) -> int:
        """Fragments accepted via append_fragment but not yet delivered."""
        return len(self._buffer)

    def append_fragment(self, fragment_dict: dict[str, Any]) -> None:
        self._buffer.append(fragment_dict)
        if len(self._buffer) >= self._flush_threshold:
            self.flush()

    def flush(self) -> bool:
        if not self._buffer:
            return True

        while self._buffer:
            remaining = len(self._buffer)
            chunk_size = remaining

            while chunk_size >= 1:
                ok, too_large = self._post_chunk(self._buffer[:chunk_size])
                if ok:
                    del self._buffer[:chunk_size]
                    self._next_sequence += 1
                    self.delivered_batches += 1
                    break
                if (
                    too_large
                    and chunk_size == 1
                    and self._split_oversized_fragment(self._buffer[0])
                ):
                    break
                if too_large and chunk_size > 1:
                    chunk_size = max(1, chunk_size // 2)
                    continue
                self.failed_batches += 1
                return False

        return True

    def _split_oversized_fragment(self, fragment: dict[str, Any]) -> bool:
        reads = fragment.get("reads")
        writes = fragment.get("writes")
        if not isinstance(reads, list) or not isinstance(writes, list):
            return False

        refs: list[tuple[str, dict[str, Any]]] = []
        refs.extend(("reads", copy.deepcopy(ref)) for ref in reads if isinstance(ref, dict))
        refs.extend(("writes", copy.deepcopy(ref)) for ref in writes if isinstance(ref, dict))
        if len(refs) <= 1:
            return False

        midpoint = max(1, len(refs) // 2)
        replacement: list[dict[str, Any]] = []
        for subset in (refs[:midpoint], refs[midpoint:]):
            part = copy.deepcopy(fragment)
            part["reads"] = [ref for kind, ref in subset if kind == "reads"]
            part["writes"] = [ref for kind, ref in subset if kind == "writes"]
            replacement.append(part)

        self._buffer[:1] = replacement
        return True

    def _post_chunk(
        self, chunk: list[dict[str, Any]], after_renew: bool = False
    ) -> tuple[bool, bool]:
        try:
            plaintext = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
            key = bytes.fromhex(self._token)
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
            encrypted_batch = base64.b64encode(nonce + ciphertext).decode("ascii")

            request = urllib.request.Request(
                url=(f"{self._glaas_url}/api/v1/fragments/sessions/{self._session_id}/fragments"),
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
        except urllib.error.HTTPError as exc:
            # 403 means the session expired mid-run (long training jobs
            # routinely outlive the registration TTL). Renew once with the
            # session token and retry; a second 403 gives up normally.
            if (
                exc.code == 403
                and not after_renew
                and renew_fragment_session(
                    self._glaas_url,
                    self._session_id,
                    self._token,
                    self._renew_ttl_seconds,
                )
            ):
                return self._post_chunk(chunk, after_renew=True)
            _get_logger().warning(
                "Failed to stream fragments for session %s sequence %d: %s",
                self._session_id,
                self._next_sequence,
                exc,
            )
            return False, exc.code == 413
        except Exception as exc:
            _get_logger().warning(
                "Failed to stream fragments for session %s sequence %d: %s",
                self._session_id,
                self._next_sequence,
                exc,
            )
            return False, False

        return True, False

    def close(self) -> bool:
        """Flush remaining fragments; False when any batch stayed undelivered."""
        return self.flush()
