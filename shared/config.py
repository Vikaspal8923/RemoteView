"""RemoteView configuration: dataclass + JSON loader/validator.

Both the home (daemon) and remote (viewer) machines use the same config.json
schema. See the design doc's Configuration section for field semantics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


class ConfigError(ValueError):
    """Raised when a config file is missing required fields or has bad values."""


@dataclass
class Config:
    """Runtime configuration for the daemon and/or viewer.

    Required fields are validated by :meth:`validate`, which
    :meth:`load` calls automatically.
    """

    # --- Connection ---
    viewer_ip: str = "127.0.0.1"
    viewer_port: int = 9000

    # --- Auth / TLS ---
    auth_token: str = ""            # 64-char hex string (32 bytes)
    tls_cert: str = "certs/server.crt"
    tls_key: str = "certs/server.key"  # daemon only; never shipped to viewer

    # --- Capture (daemon only) ---
    frame_rate: int = 5            # capture FPS (low-latency default)
    quality: int = 45              # JPEG quality 1-95 (low-latency default)
    monitor: int = 0               # 0 = primary monitor
    max_width: int = 1280          # stream downscale limit; 0 = native width
    max_height: int = 720          # stream downscale limit; 0 = native height
    capture_backend: str = "auto"
    affinity_enabled: bool = True
    affinity_poll_ms: int = 1000
    affinity_max_failures: int = 5

    # --- Paths / runtime ---
    config_path: Optional[str] = field(default=None, repr=False)

    @staticmethod
    def load(path: str) -> "Config":
        """Load and validate a config from a JSON file.

        Raises :class:`ConfigError` (a ``ValueError``) on missing file,
        malformed JSON, or validation failure.
        """
        if not os.path.isfile(path):
            raise ConfigError(f"config file not found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("config root must be a JSON object")

        cfg = Config(
            viewer_ip=raw.get("viewer_ip", Config.viewer_ip),
            viewer_port=raw.get("viewer_port", Config.viewer_port),
            auth_token=raw.get("auth_token", ""),
            tls_cert=raw.get("tls_cert", "certs/server.crt"),
            tls_key=raw.get("tls_key", "certs/server.key"),
            frame_rate=raw.get("frame_rate", 5),
            quality=raw.get("quality", 45),
            monitor=raw.get("monitor", 0),
            max_width=raw.get("max_width", 1280),
            max_height=raw.get("max_height", 720),
            capture_backend=raw.get("capture_backend", "auto"),
            affinity_enabled=raw.get("affinity_enabled", True),
            affinity_poll_ms=raw.get("affinity_poll_ms", 1000),
            affinity_max_failures=raw.get("affinity_max_failures", 5),
            config_path=path,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Validate required fields and value ranges.

        Raises :class:`ConfigError` listing every problem found.
        """
        errors = []

        if not self.viewer_ip or not str(self.viewer_ip).strip():
            errors.append("viewer_ip is required")

        if not isinstance(self.viewer_port, int) or not (1 <= self.viewer_port <= 65535):
            errors.append("viewer_port must be an integer in 1..65535")

        if not self.auth_token or len(self.auth_token) != 64:
            errors.append("auth_token must be a 64-char hex string")

        if not self.tls_cert:
            errors.append("tls_cert is required")

        if not isinstance(self.frame_rate, int) or not (1 <= self.frame_rate <= 120):
            errors.append("frame_rate must be an integer in 1..120")

        if not isinstance(self.quality, int) or not (1 <= self.quality <= 95):
            errors.append("quality must be an integer in 1..95")

        if not isinstance(self.monitor, int) or self.monitor < 0:
            errors.append("monitor must be a non-negative integer")

        if not isinstance(self.max_width, int) or self.max_width < 0:
            errors.append("max_width must be a non-negative integer")

        if not isinstance(self.max_height, int) or self.max_height < 0:
            errors.append("max_height must be a non-negative integer")

        if self.capture_backend not in {"auto", "gdi"}:
            errors.append("capture_backend must be auto or gdi")
        if not isinstance(self.affinity_enabled, bool):
            errors.append("affinity_enabled must be a boolean")
        if not isinstance(self.affinity_poll_ms, int) or self.affinity_poll_ms < 1:
            errors.append("affinity_poll_ms must be a positive integer")
        if not isinstance(self.affinity_max_failures, int) or self.affinity_max_failures < 1:
            errors.append("affinity_max_failures must be a positive integer")

        if errors:
            raise ConfigError("invalid config: " + "; ".join(errors))

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict (excludes config_path)."""
        d = asdict(self)
        d.pop("config_path", None)
        return d

    def save(self, path: str) -> None:
        """Write this config to ``path`` as pretty JSON."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
