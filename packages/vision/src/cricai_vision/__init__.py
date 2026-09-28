"""Vision layer: pixels → structured measurements.

Heavy model providers (MediaPipe, YOLO) live behind adapters; the math and
pipeline logic here is deterministic and fully testable on synthetic scenes.
"""

__version__ = "0.1.0"
