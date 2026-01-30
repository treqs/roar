"""
Omit filter for redacting secrets from registration data.

This module provides filtering of sensitive data from commands, URLs,
metadata, and telemetry before registering with GLaaS.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any


def _get_logger():
    from ..core.di import resolve_or_default
    from ..core.interfaces.logger import ILogger
    from ..services.logging import NullLogger

    return resolve_or_default(ILogger, NullLogger)


@dataclass
class OmitMatch:
    """Represents a detected secret that was redacted."""

    pattern_id: str
    original_length: int
    field: str = ""


@dataclass
class OmitResult:
    """Result of filtering operation."""

    filtered: str
    detections: list[OmitMatch] = field(default_factory=list)

    @property
    def was_modified(self) -> bool:
        return len(self.detections) > 0


# Built-in patterns for common secret formats
# Each pattern is a tuple of (id, compiled_regex, replacement)
BUILTIN_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # AWS credentials
    (
        "aws_access_key",
        re.compile(r"(AKIA[A-Z0-9]{16})"),
        "[AWS_KEY_REDACTED]",
    ),
    (
        "aws_secret_key",
        re.compile(
            r"(aws_secret_access_key|aws_secret)[=:\s]+['\"]?([A-Za-z0-9/+=]{40})['\"]?",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
    # GitHub tokens
    (
        "github_token",
        re.compile(r"(ghp_[A-Za-z0-9]{36,})"),
        "[GITHUB_TOKEN_REDACTED]",
    ),
    (
        "github_pat",
        re.compile(r"(github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59})"),
        "[GITHUB_PAT_REDACTED]",
    ),
    # OpenAI / Anthropic keys
    (
        "openai_key",
        re.compile(r"(sk-[a-zA-Z0-9]{20,})"),
        "[OPENAI_KEY_REDACTED]",
    ),
    (
        "anthropic_key",
        re.compile(r"(sk-ant-[a-zA-Z0-9\-]+)"),
        "[ANTHROPIC_KEY_REDACTED]",
    ),
    # HuggingFace token
    (
        "huggingface_token",
        re.compile(r"(hf_[a-zA-Z0-9]{34})"),
        "[HF_TOKEN_REDACTED]",
    ),
    # Generic API key patterns (command line args)
    (
        "generic_api_key_arg",
        re.compile(r"(--?(?:api[_-]?key|apikey))[=\s]+['\"]?([^\s'\"]{16,})['\"]?", re.IGNORECASE),
        r"\1=[REDACTED]",
    ),
    # Generic token patterns (command line args)
    (
        "generic_token_arg",
        re.compile(
            r"(--?(?:token|auth[_-]?token))[=\s]+['\"]?([^\s'\"]{16,})['\"]?", re.IGNORECASE
        ),
        r"\1=[REDACTED]",
    ),
    # Password patterns (command line args)
    (
        "generic_password_arg",
        re.compile(r"(--?(?:password|passwd|pwd))[=\s]+['\"]?([^\s'\"]+)['\"]?", re.IGNORECASE),
        r"\1=[REDACTED]",
    ),
    # Secret patterns (command line args)
    (
        "generic_secret_arg",
        re.compile(r"(--?(?:secret|secret[_-]?key))[=\s]+['\"]?([^\s'\"]+)['\"]?", re.IGNORECASE),
        r"\1=[REDACTED]",
    ),
    # Bearer tokens
    (
        "bearer_token",
        re.compile(r"(bearer)\s+([a-zA-Z0-9\-._~+/]{20,}=*)", re.IGNORECASE),
        r"\1 [REDACTED]",
    ),
    # Git URL credentials (https://user:token@host)
    (
        "git_url_creds",
        re.compile(r"(https?://)([^:@]+):([^@]+)@"),
        r"\1\2:[REDACTED]@",
    ),
    # Database connection strings
    (
        "database_url",
        re.compile(r"((?:postgres|mysql|mongodb|redis)://)([^:]+):([^@]+)@", re.IGNORECASE),
        r"\1\2:[REDACTED]@",
    ),
    # Private keys
    (
        "private_key",
        re.compile(r"(-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----)"),
        "[PRIVATE_KEY_REDACTED]",
    ),
    # Slack webhooks
    (
        "slack_webhook",
        re.compile(r"(hooks\.slack\.com/services/)([A-Z0-9/]+)", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    # Environment variable assignments in commands
    (
        "env_var_assignment",
        re.compile(
            r"([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTH)[A-Z_]*)=([^\s]+)",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
]


class OmitFilter:
    """
    Filters sensitive data from registration payloads.

    Uses:
    1. Built-in patterns (API keys, tokens, passwords)
    2. User-configured patterns (custom regex)
    3. User-configured explicit secrets (literal values to always redact)
    4. Environment variable names (values redacted from metadata)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the omit filter.

        Args:
            config: Configuration dict with structure:
                {
                    "enabled": bool,
                    "secrets": {"values": [...]},
                    "env_vars": {"names": [...]},
                    "patterns": [{"id": str, "pattern": str, "description": str}, ...],
                    "allowlist": {"patterns": [...]},
                }
        """
        config = config or {}
        self.enabled = config.get("enabled", True)

        # Load explicit secrets to redact
        secrets_config = config.get("secrets", {})
        self.explicit_secrets: list[str] = secrets_config.get("values", [])

        # Load env var names whose values should be redacted
        env_vars_config = config.get("env_vars", {})
        self.env_var_names: list[str] = env_vars_config.get("names", [])

        # Load allowlist patterns
        allowlist_config = config.get("allowlist", {})
        allowlist_patterns = allowlist_config.get("patterns", [])
        self.allowlist: list[re.Pattern] = [re.compile(p) for p in allowlist_patterns if p]

        # Load and compile custom patterns
        custom_patterns = config.get("patterns", [])
        self.custom_patterns: list[tuple[str, re.Pattern, str]] = []
        for p in custom_patterns:
            if isinstance(p, dict) and "pattern" in p:
                try:
                    compiled = re.compile(p["pattern"])
                    pattern_id = p.get("id", f"custom_{len(self.custom_patterns)}")
                    replacement = p.get("replacement", "[REDACTED]")
                    self.custom_patterns.append((pattern_id, compiled, replacement))
                except re.error as e:
                    _get_logger().warning(f"Invalid custom pattern '{p.get('id', 'unknown')}': {e}")

    def _is_allowlisted(self, text: str) -> bool:
        """Check if text matches any allowlist pattern."""
        return any(pattern.search(text) for pattern in self.allowlist)

    def _redact_explicit_secrets(self, text: str, field: str = "") -> tuple[str, list[OmitMatch]]:
        """Redact explicit secrets from text."""
        detections = []
        result = text

        for secret in self.explicit_secrets:
            if secret and secret in result:
                detections.append(
                    OmitMatch(
                        pattern_id="explicit_secret",
                        original_length=len(secret),
                        field=field,
                    )
                )
                result = result.replace(secret, "[REDACTED]")

        return result, detections

    def _apply_patterns(
        self,
        text: str,
        patterns: list[tuple[str, re.Pattern, str]],
        field: str = "",
    ) -> tuple[str, list[OmitMatch]]:
        """Apply regex patterns to redact secrets."""
        detections = []
        result = text

        for pattern_id, pattern, replacement in patterns:
            matches = list(pattern.finditer(result))
            if matches:
                # Check if any match is allowlisted
                non_allowlisted_matches = [
                    m for m in matches if not self._is_allowlisted(m.group(0))
                ]

                if non_allowlisted_matches:
                    for match in non_allowlisted_matches:
                        detections.append(
                            OmitMatch(
                                pattern_id=pattern_id,
                                original_length=len(match.group(0)),
                                field=field,
                            )
                        )

                    # Apply replacement (this handles all matches including allowlisted,
                    # but only non-allowlisted ones were logged)
                    # We need to be more careful here - only replace non-allowlisted
                    for match in reversed(non_allowlisted_matches):
                        start, end = match.span()
                        replaced = pattern.sub(replacement, match.group(0), count=1)
                        result = result[:start] + replaced + result[end:]

        return result, detections

    def filter_string(self, text: str, field: str = "") -> OmitResult:
        """
        Filter secrets from a string.

        Args:
            text: The string to filter
            field: Optional field name for logging

        Returns:
            OmitResult with filtered string and list of detections
        """
        if not self.enabled or not text:
            return OmitResult(filtered=text)

        all_detections: list[OmitMatch] = []
        result = text

        # First, redact explicit secrets
        result, explicit_detections = self._redact_explicit_secrets(result, field)
        all_detections.extend(explicit_detections)

        # Apply built-in patterns
        result, builtin_detections = self._apply_patterns(result, BUILTIN_PATTERNS, field)
        all_detections.extend(builtin_detections)

        # Apply custom patterns
        result, custom_detections = self._apply_patterns(result, self.custom_patterns, field)
        all_detections.extend(custom_detections)

        return OmitResult(filtered=result, detections=all_detections)

    def filter_command(self, command: str) -> tuple[str, list[str]]:
        """
        Filter secrets from a command string.

        Args:
            command: The command string to filter

        Returns:
            Tuple of (filtered_command, list_of_detection_ids)
        """
        result = self.filter_string(command, field="command")
        detection_ids = [d.pattern_id for d in result.detections]
        return result.filtered, detection_ids

    def filter_git_url(self, url: str) -> tuple[str, list[str]]:
        """
        Filter credentials from a git URL.

        Args:
            url: The git URL to filter

        Returns:
            Tuple of (filtered_url, list_of_detection_ids)
        """
        result = self.filter_string(url, field="git_url")
        detection_ids = [d.pattern_id for d in result.detections]
        return result.filtered, detection_ids

    def filter_metadata(self, metadata: dict) -> tuple[dict, list[str]]:
        """
        Filter secrets from metadata dict.

        Specifically handles:
        - runtime.env_vars: Redacts values for configured env var names
        - Recursively filters string values

        Args:
            metadata: The metadata dict to filter

        Returns:
            Tuple of (filtered_metadata, list_of_detection_ids)
        """
        if not self.enabled or not metadata:
            return metadata, []

        all_detections: list[str] = []
        result = self._deep_filter_dict(metadata, all_detections, "metadata")

        # Special handling for env_vars
        if "runtime" in result and isinstance(result["runtime"], dict):
            env_vars = result["runtime"].get("env_vars", {})
            if isinstance(env_vars, dict):
                for var_name in self.env_var_names:
                    if var_name in env_vars:
                        env_vars[var_name] = "[REDACTED]"
                        all_detections.append(f"env_var:{var_name}")
                result["runtime"]["env_vars"] = env_vars

        return result, all_detections

    def _deep_filter_dict(self, obj: Any, detections: list[str], path: str = "") -> Any:
        """Recursively filter strings in a dict/list structure."""
        if isinstance(obj, str):
            result = self.filter_string(obj, field=path)
            detections.extend([d.pattern_id for d in result.detections])
            return result.filtered
        elif isinstance(obj, dict):
            return {
                k: self._deep_filter_dict(v, detections, f"{path}.{k}" if path else k)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [
                self._deep_filter_dict(item, detections, f"{path}[{i}]")
                for i, item in enumerate(obj)
            ]
        else:
            return obj

    def filter_telemetry(self, telemetry: str) -> tuple[str, list[str]]:
        """
        Filter secrets from telemetry JSON string.

        Args:
            telemetry: The telemetry JSON string to filter

        Returns:
            Tuple of (filtered_telemetry, list_of_detection_ids)
        """
        if not self.enabled or not telemetry:
            return telemetry, []

        try:
            data = json.loads(telemetry)
            all_detections: list[str] = []
            filtered_data = self._deep_filter_dict(data, all_detections, "telemetry")
            return json.dumps(filtered_data), all_detections
        except json.JSONDecodeError:
            # If not valid JSON, treat as plain string
            result = self.filter_string(telemetry, field="telemetry")
            return result.filtered, [d.pattern_id for d in result.detections]

    def detect_secrets(self, text: str, field: str = "") -> list[OmitMatch]:
        """
        Detect secrets in text without redacting.

        Useful for showing users what will be filtered before proceeding.

        Args:
            text: The string to scan for secrets
            field: Optional field name for logging

        Returns:
            List of OmitMatch detections (empty if disabled or no secrets found)
        """
        if not self.enabled or not text:
            return []

        result = self.filter_string(text, field)
        return result.detections

    def get_detection_summary(self, detections: list[OmitMatch]) -> list[str]:
        """
        Get a human-readable summary of detected secrets.

        Args:
            detections: List of OmitMatch objects

        Returns:
            List of unique pattern IDs that were detected
        """
        return list({d.pattern_id for d in detections})
