# LIVE scheduler engine

`LiveScheduler` accepts a prepared `Product`/`Video` manifest. IDs are opaque;
the engine never downloads files or calls TikTok, OBS, or a LIVE Session API.
Construct one scheduler for an Agent and pass a `PlaybackAdapter` that plays
verified local files. `integration_example.py` shows the boundary.

```python
from sarae_agent.scheduler import Config, LiveScheduler, Product, Video

products = [Product("product-1", (Video("cache-video-1"), Video("cache-video-2")))]
scheduler = LiveScheduler("agent-1", playback_adapter)
scheduler.configure(products, Config())
status = scheduler.start()
# Schedule a timer with status["token"] if desired. A stale timer is ignored.
scheduler.next(token=status["token"])
# Forward media events with the token associated with that playback.
scheduler.on_video_ended(playback_token)
```

`next(mode=AFTER_CURRENT)` marks a pending switch; the next valid end event
performs it. `manual_switch` turns off auto playback and clears pending work.
Use it for both a Web user switch and a confirmed manual TikTok pin. Resume
auto playback only on an explicit `resume()` command. The host must also turn
off its Auto Pin policy on a confirmed manual pin; scheduler resume does not
turn Auto Pin on. Duplicate or stale callbacks are ignored by token.

Snapshot with `snapshot()` and JSON serialization. `restore(snapshot, products,
adapter)` returns a PAUSED scheduler; an explicit `resume()` reacquires the
Agent and restarts the selected video. Persist after every state change. An
in-process registry prevents two active schedulers with the same Agent ID.
Across processes, the host still needs its existing Agent/session lock.

Engine completion here does not mean LIVE Session integration is complete.
