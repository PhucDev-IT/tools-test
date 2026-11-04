"""Example wiring for an Agent host; no LIVE Session or OBS code is imported.

The host resolves video.id to a verified local cache path before calling play.
Its media-ended/error callbacks must report the token that play/resume received.
Persist ``scheduler.snapshot()`` after state changes with the host's own store.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .engine import LiveScheduler
from .types import Product, Video


class MediaPlayer(Protocol):
    def play_file(self, path: str) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...


class CachedPlaybackAdapter:
    def __init__(self, player: MediaPlayer, resolve_cached_path: Callable[[str], str]):
        self.player = player
        self.resolve_cached_path = resolve_cached_path
        self.current_token: int | None = None

    def play(self, product: Product, video: Video, token: int) -> None:
        path = self.resolve_cached_path(video.id)
        self.player.play_file(path)
        self.current_token = token

    def pause(self) -> None:
        self.player.pause()

    def resume(self, token: int) -> None:
        self.player.resume()
        self.current_token = token

    def stop(self) -> None:
        self.player.stop()
        self.current_token = None


def confirmed_manual_pin(scheduler: LiveScheduler, product_id: str,
                         disable_auto_pin: Callable[[], None]) -> dict:
    """Called only for a confirmed user pin, never for unknown observer state.

    The host owns Auto Pin policy. Disabling it here ensures an observer event
    cannot quietly turn it back on when scheduler auto playback is resumed.
    """
    disable_auto_pin()
    return scheduler.manual_switch(product_id)
