"""Input manifest, policy and playback boundary for the LIVE scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Algorithm(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"
    RANDOM_REPEAT = "RANDOM_REPEAT"
    SHUFFLE_NO_REPEAT = "SHUFFLE_NO_REPEAT"


class SwitchMode(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    AFTER_CURRENT = "AFTER_CURRENT"


class Status(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Video:
    id: str
    available: bool = True


@dataclass(frozen=True)
class Product:
    id: str
    videos: tuple[Video, ...]


@dataclass(frozen=True)
class Config:
    product_algorithm: Algorithm = Algorithm.SEQUENTIAL
    video_algorithm: Algorithm = Algorithm.SEQUENTIAL
    switch_mode: SwitchMode = SwitchMode.IMMEDIATE


class PlaybackAdapter(Protocol):
    """Adapter must forward the token on video-ended/error callbacks.

    Calls are serialized by the scheduler. Implementations must not synchronously
    call back into the scheduler from these methods.
    """

    def play(self, product: Product, video: Video, token: int) -> None: ...
    def pause(self) -> None: ...
    def resume(self, token: int) -> None: ...
    def stop(self) -> None: ...
