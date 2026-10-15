"""Validated keyboard/mouse control messages.

Control is deliberately a small JSON-lines protocol. It contains no shell,
process, clipboard, file, audio, or camera operations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_TEXT_LENGTH = 256
MAX_KEY_LENGTH = 32
MAX_SCROLL_DELTA = 20

ALLOWED_TYPES = frozenset({
    "CONTROL_ENABLE", "CONTROL_DISABLE", "EMERGENCY_STOP",
    "MOUSE_MOVE", "MOUSE_BUTTON_DOWN", "MOUSE_BUTTON_UP",
    "MOUSE_SCROLL", "KEY_DOWN", "KEY_UP", "TYPE_TEXT",
})
ALLOWED_BUTTONS = frozenset({"left", "middle", "right", "x1", "x2"})


class ControlMessageError(ValueError):
    """Raised when a control message is invalid or out of range."""


@dataclass(frozen=True)
class ControlMessage:
    type: str
    data: dict[str, Any]

    def validate(self) -> "ControlMessage":
        if self.type not in ALLOWED_TYPES:
            raise ControlMessageError(f"unsupported control type: {self.type}")
        if self.type == "MOUSE_MOVE":
            for name in ("x", "y"):
                value = self.data.get(name)
                if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                    raise ControlMessageError(f"{name} must be between 0 and 1")
        elif self.type in {"MOUSE_BUTTON_DOWN", "MOUSE_BUTTON_UP"}:
            if self.data.get("button") not in ALLOWED_BUTTONS:
                raise ControlMessageError("invalid mouse button")
        elif self.type == "MOUSE_SCROLL":
            value = self.data.get("delta")
            if not isinstance(value, int) or not -MAX_SCROLL_DELTA <= value <= MAX_SCROLL_DELTA:
                raise ControlMessageError("scroll delta is out of range")
        elif self.type in {"KEY_DOWN", "KEY_UP"}:
            key = self.data.get("key")
            if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY_LENGTH:
                raise ControlMessageError("invalid key")
        elif self.type == "TYPE_TEXT":
            text = self.data.get("text")
            if not isinstance(text, str) or not text or len(text) > MAX_TEXT_LENGTH:
                raise ControlMessageError("text is empty or too long")
        return self

    def encode(self) -> bytes:
        self.validate()
        return (json.dumps({"type": self.type, **self.data}, separators=(",", ":")) + "\n").encode()


def decode_control_message(line: bytes | str) -> ControlMessage:
    try:
        raw = json.loads(line.decode() if isinstance(line, bytes) else line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlMessageError("invalid control JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
        raise ControlMessageError("control message must be an object with type")
    msg = ControlMessage(raw.pop("type"), raw)
    return msg.validate()


def make_control(type_: str, **data: Any) -> bytes:
    return ControlMessage(type_, data).encode()
