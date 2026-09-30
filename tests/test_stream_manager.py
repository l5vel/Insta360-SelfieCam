import queue
import unittest
import numpy as np
from stream_manager import _apply_crop, generate_equirectangular_maps, _safe_q_put, UnifiedStreamManager
from sandbox import setUpModule, tearDownModule  # noqa: F401


class PreviewTests(unittest.TestCase):
    def test_crop_and_projection_produce_valid_images(self):
        image = np.zeros((720, 1440, 3), dtype=np.uint8)
        image[:, :720] = (20, 30, 40)
        crop = _apply_crop(image)
        self.assertEqual(crop.shape, (480, 640, 3))
        np.testing.assert_array_equal(crop[0, 0], [20, 30, 40])
        full_resolution_crop = _apply_crop(image, (1152, 864))
        self.assertEqual(full_resolution_crop.shape, (864, 1152, 3))
        x, y = generate_equirectangular_maps(1440, 720)
        self.assertEqual(x.shape, (720, 1440))
        self.assertTrue(np.isfinite(x).all() and np.isfinite(y).all())

    def test_full_queue_keeps_latest(self):
        q = queue.Queue(1)
        _safe_q_put(q, b'old')
        _safe_q_put(q, b'new')
        self.assertEqual(q.get_nowait(), b'new')

    def test_stop_is_idempotent_without_starting_hardware(self):
        manager = UnifiedStreamManager()
        manager.stop()
        manager.stop()
        self.assertIsNone(manager.error())
        self.assertIsNone(manager.processing_process)

    def test_invalid_srt_address_fails_before_start(self):
        manager = UnifiedStreamManager()
        with self.assertRaises(ValueError):
            manager.start('bad?mode=listener')
        self.assertIsNone(manager.processing_process)
        manager.stop()


def synthetic_camera_worker(*args):
    """Exercise the real decoder/queues without opening a camera socket."""
    import asyncio
    import sys
    import threading
    import types
    import av
    from fractions import Fraction
    import stream_manager

    class SyntheticClient:
        preview_stream_started = False
        def on_video_stream(self, **kwargs):
            def register(callback):
                self.callback = callback
            return register
        def open(self):
            pass
        def start_preview_stream(self):
            self.preview_stream_started = True
            def send():
                encoder = av.CodecContext.create('libx264', 'w')
                encoder.width, encoder.height = 320, 160
                encoder.pix_fmt = 'yuv420p'
                encoder.time_base = Fraction(1, 24)
                encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}
                for _ in range(4):
                    frame = av.VideoFrame.from_ndarray(np.full((160, 320, 3), 80, dtype=np.uint8), format='bgr24')
                    for packet in encoder.encode(frame):
                        asyncio.run(self.callback(content=bytes(packet)))
                for packet in encoder.encode(None):
                    asyncio.run(self.callback(content=bytes(packet)))
            self.thread = threading.Thread(target=send)
            self.thread.start()
        def stop_preview_stream(self):
            self.preview_stream_started = False
        def close(self):
            self.thread.join(1)
    sys.modules['insta360.rtmp'] = types.SimpleNamespace(Client=SyntheticClient)
    stream_manager.video_processing_worker(*args)


class PreviewProcessTests(unittest.TestCase):
    def test_real_decoder_delivers_first_frame_and_stops_without_srt(self):
        import threading
        from camera_connection import Deadline
        manager = UnifiedStreamManager()
        manager.worker = synthetic_camera_worker
        self.addCleanup(manager.stop)
        manager.start()
        manager.wait_ready(Deadline(10, threading.Event()))
        cropped = manager.mjpeg_queue_cropped.get(timeout=2)
        equirec = manager.mjpeg_queue_equirec.get(timeout=2)
        self.assertTrue(cropped.startswith(b'\xff\xd8'))
        self.assertTrue(equirec.startswith(b'\xff\xd8'))
        import cv2
        self.assertEqual(cv2.imdecode(np.frombuffer(cropped, dtype=np.uint8), cv2.IMREAD_COLOR).shape, (480, 640, 3))
        self.assertEqual(cv2.imdecode(np.frombuffer(equirec, dtype=np.uint8), cv2.IMREAD_COLOR).shape, (320, 640, 3))
        original = manager.processing_process
        manager.start()
        self.assertIs(manager.processing_process, original)
        manager.stop()
        self.assertIsNone(manager.processing_process)
