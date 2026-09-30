"""Save frame keeps the 360 frame the preview last sent, byte for byte, and refuses when none is fresh."""

import asyncio
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from sandbox import setUpModule, tearDownModule  # noqa: F401
import main

FRAME = b'\xff\xd8 a 360 frame as sent \xff\xd9'


class SaveFrameTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.frames = Path(folder.name) / 'frames'
        for p in (patch.object(main, 'FRAMES_DIR', self.frames), patch.dict(main.SENT_FRAMES, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.request = Mock(client=Mock(host='198.51.100.49'))

    def test_the_generator_records_each_frame_it_sends(self):
        q = queue.Queue()
        q.put(FRAME)
        with patch.object(main.stream_manager, 'mjpeg_queue_equirec', q, create=True):
            chunk = asyncio.run(main.mjpeg_generator('mjpeg_queue_equirec').__anext__())
        self.assertIn(FRAME, chunk)
        self.assertEqual(main.SENT_FRAMES['mjpeg_queue_equirec'][0], FRAME)

    def test_the_last_sent_360_frame_is_saved_unchanged(self):
        main.SENT_FRAMES['mjpeg_queue_equirec'] = (FRAME, time.monotonic())
        main.SENT_FRAMES['mjpeg_queue_cropped'] = (b'the selfie crop', time.monotonic())
        answer = main.save_frame(self.request)
        saved = Path(answer['path'])
        self.assertEqual(saved.parent, self.frames)
        self.assertEqual(saved.read_bytes(), FRAME)

    def test_no_frame_or_a_stale_one_is_refused_and_nothing_is_written(self):
        for sent in (None, (FRAME, time.monotonic() - main.FRAME_FRESH_S - 1)):
            main.SENT_FRAMES.clear()
            if sent:
                main.SENT_FRAMES['mjpeg_queue_equirec'] = sent
            with self.assertRaises(HTTPException) as refused:
                main.save_frame(self.request)
            self.assertEqual(refused.exception.status_code, 409)
            self.assertFalse(self.frames.exists())


if __name__ == '__main__':
    unittest.main()
