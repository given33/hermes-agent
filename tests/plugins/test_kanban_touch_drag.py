import json
import unittest
from pathlib import Path


KANBAN_BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "kanban"
    / "dashboard"
    / "dist"
    / "index.js"
)


class KanbanTouchDragTests(unittest.TestCase):
    def test_touch_drag_requires_long_press_and_cancels_scroll_gesture(self):
        bundle = KANBAN_BUNDLE.read_text(encoding="utf-8")

        self.assertIn("TOUCH_DRAG_HOLD_MS", bundle)
        self.assertIn("TOUCH_SCROLL_CANCEL_PX", bundle)
        self.assertIn("clearTimeout(holdTimer)", bundle)
        self.assertIn("if (!dragging) return", bundle)

    def test_manifest_cache_busts_touch_fix(self):
        manifest = json.loads(
            (KANBAN_BUNDLE.parent.parent / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(manifest["version"], "1.0.1")
        self.assertEqual(manifest["entry"], "dist/index.js?v=1.0.1")


if __name__ == "__main__":
    unittest.main()
