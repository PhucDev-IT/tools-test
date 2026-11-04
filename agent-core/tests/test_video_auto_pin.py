"""Auto-pin follows confirmed product playback and rejects stale queued actions."""
import threading
import unittest
from types import SimpleNamespace

from sarae_agent.runtime import AgentRuntime


class PinSink:
    def __init__(self):
        self.items = []
        self.auto_enabled = True

    def pin_status(self):
        return {"auto_enabled": self.auto_enabled}

    def set_video_auto(self, enabled, products):
        self.auto_enabled = enabled

    def video_product(self, product_id, session_id):
        return {"name": str(product_id)}

    def queue_video_pin(self, product_id, current, guard, session_id):
        self.items.append((product_id, current, guard))


class VideoAutoPinTests(unittest.TestCase):
    def test_auto_pin_setting_survives_idle_playback(self):
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.engine = SimpleNamespace(lock=threading.RLock(), active=False, session_id=None,
                                         product_id=None, transition_count=0)
        runtime.auto_pin = PinSink()
        runtime._auto_pin_guard = threading.Lock()
        runtime._auto_pin_last = None
        runtime._maybe_pin_current_video()
        self.assertTrue(runtime.auto_pin.auto_enabled)
        self.assertEqual(runtime.auto_pin.items, [])

    def test_switch_queues_once_and_stale_video_is_rejected(self):
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.engine = SimpleNamespace(lock=threading.RLock(), active=True, session_id=4,
                                         product_id=1, transition_count=1)
        runtime.auto_pin = PinSink()
        runtime._auto_pin_guard = threading.Lock()
        runtime._auto_pin_last = None
        runtime._maybe_pin_current_video()
        runtime._maybe_pin_current_video()
        self.assertEqual(len(runtime.auto_pin.items), 1)
        old_current = runtime.auto_pin.items[0][1]
        self.assertTrue(old_current())
        runtime.engine.product_id = 2
        runtime.engine.transition_count = 2
        self.assertFalse(old_current())
        runtime._maybe_pin_current_video()
        self.assertEqual([item[0] for item in runtime.auto_pin.items], [1, 2])


if __name__ == "__main__":
    unittest.main()
