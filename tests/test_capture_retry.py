"""download_capture must survive a transient network failure.

Needs the app's dependencies (httpx, av, cv2, ...), which live only in the service venv:
    uv run --with pytest --with httpx --with fastapi --with numpy pytest -q
Skipped cleanly when they are absent.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

main = pytest.importorskip("main", reason="service venv not active")
httpx = pytest.importorskip("httpx")


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code, self.content = status_code, content


class _Client:
    """Replays a scripted sequence; exceptions are raised, responses returned."""

    def __init__(self, seq):
        self.seq, self.calls = list(seq), 0

    async def get(self, url, timeout=None):
        item = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def test_returns_content_without_retrying_on_success():
    c = _Client([_Resp(200, b"JPEGDATA")])
    assert asyncio.run(main.download_capture(c, "u")) == b"JPEGDATA"
    assert c.calls == 1


def test_recovers_after_a_transient_timeout():
    c = _Client([httpx.ReadTimeout("slow"), _Resp(200, b"OK2")])
    assert asyncio.run(main.download_capture(c, "u")) == b"OK2"
    assert c.calls == 2


def test_raises_after_exhausting_attempts():
    c = _Client([httpx.ReadTimeout("always")])
    with pytest.raises(httpx.RequestError):
        asyncio.run(main.download_capture(c, "u"))
    assert c.calls == main.CAPTURE_DOWNLOAD_RETRIES


def test_non_200_is_retried_not_returned_as_image_bytes():
    c = _Client([_Resp(500, b"<html>error</html>")])
    with pytest.raises(httpx.RequestError):
        asyncio.run(main.download_capture(c, "u"))
