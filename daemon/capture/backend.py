"""Capture backend interfaces and shared frame helpers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CaptureBackend(ABC):
    @abstractmethod
    def capture(self) -> Any:
        """Return a captured image."""
        raise NotImplementedError
