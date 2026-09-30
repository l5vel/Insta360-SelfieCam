"""Bounded preview lifecycle with decoding and camera SDK isolated in a child."""
import ipaddress
import multiprocessing as mp
import os
import queue
import signal
import time
import threading
from functools import wraps


def _safe_q_put(q, data):
    try:
        q.put_nowait(data)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            return
        try:
            q.put_nowait(data)
        except queue.Full:
            pass


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class UnifiedStreamManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.context = mp.get_context('spawn')
        self.worker = video_processing_worker
        self.processing_process = None
        self.destination = None
        self.last_error = None
        self._new_channels()

    def _new_channels(self):
        self.mjpeg_queue_cropped = self.context.Queue(3)
        self.mjpeg_queue_equirec = self.context.Queue(3)
        self.errors = self.context.Queue(3)
        self.ready = self.context.Event()
        self.stopping = self.context.Event()
        self.last_frame = self.context.Value('d', 0)

    @synchronized
    def start(self, srt_ip='', srt_port=7003):
        if srt_ip:
            ipaddress.IPv4Address(srt_ip)
        if not 1 <= srt_port <= 65535:
            raise ValueError('Invalid SRT port.')
        if self.processing_process and not self.error() and self.destination == (srt_ip, srt_port):
            return
        self.stop()
        self.last_error = None
        self.destination = (srt_ip, srt_port)
        self.processing_process = self.context.Process(
            target=self.worker,
            args=(self.mjpeg_queue_cropped, self.mjpeg_queue_equirec, self.errors,
                  self.ready, self.stopping, self.last_frame, srt_ip, srt_port), daemon=True)
        self.processing_process.start()

    start_pipeline = start

    @synchronized
    def error(self):
        try:
            self.last_error = self.errors.get_nowait()
        except queue.Empty:
            pass
        if self.last_error:
            return self.last_error
        if self.processing_process and not self.processing_process.is_alive():
            return f'Preview process exited ({self.processing_process.exitcode}).'
        if self.ready.is_set() and time.monotonic() - self.last_frame.value > 10:
            return 'No camera video received for 10 seconds.'
        return None

    def wait_ready(self, deadline):
        end = min(deadline.end, time.monotonic() + 15)
        while True:
            deadline.remaining(1)
            error = self.error()
            if error:
                raise RuntimeError(f'Preview failed: {error}. Wi-Fi connection retained.')
            if self.ready.is_set():
                return
            if time.monotonic() >= end:
                raise RuntimeError('Preview produced no image within 15 seconds. Wi-Fi connection retained.')
            self.ready.wait(.1)

    @synchronized
    def stop(self):
        process = self.processing_process
        if process:
            self.stopping.set()
            process.join(3)
            # Child starts a session before creating FFmpeg. Kill the whole group
            # even when the SDK's shutdown stalls, so FFmpeg cannot be orphaned.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.is_alive():
                process.terminate()
            process.join(1)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.is_alive():
                process.kill()
            process.join(1)
            process.close()
            self.processing_process = None
        for q in (self.mjpeg_queue_cropped, self.mjpeg_queue_equirec, self.errors):
            q.close()
            q.cancel_join_thread()
        self._new_channels()
        self.last_error = None


PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 480
PREVIEW_FPS = 10


def _apply_crop(image, output_size=(PREVIEW_WIDTH, PREVIEW_HEIGHT)):
    import cv2
    h, w = image.shape[:2]
    cx, cy = w // 4, h // 2
    half_w = max(1, int((w // 2) * .60) // 2)
    half_h = max(1, int(half_w * .75))
    crop = image[max(0, cy-half_h):min(h, cy+half_h), max(0, cx-half_w):cx+half_w]
    return cv2.resize(crop, output_size)


def video_processing_worker(cropped_q, equi_q, errors, ready, stopping, last_frame, srt_ip, srt_port):
    os.setsid()
    import select
    import subprocess
    import av
    import cv2
    from insta360.rtmp import Client
    raw = queue.Queue(60)
    client = None
    ffmpeg = None
    for q in (cropped_q, equi_q):
        q.cancel_join_thread()
    try:
        codec = av.CodecContext.create('h264', 'r')
        client = Client()
        async def ingest(**kwargs):
            content = kwargs.get('content') or kwargs.get('data') or kwargs.get('payload') or kwargs.get('buffer')
            if content:
                _safe_q_put(raw, content)
        client.on_video_stream(wait=True)(ingest)
        if srt_ip:
            ffmpeg = subprocess.Popen([
                '/usr/bin/ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                '-s', '1152x864', '-r', '24', '-i', '-', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-tune', 'zerolatency', '-f', 'mpegts', f'srt://{srt_ip}:{srt_port}?mode=caller&connect_timeout=3000'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.set_blocking(ffmpeg.stdin.fileno(), False)
        client.open()
        client.start_preview_stream()
        maps = None
        shape = None
        next_preview_at = 0.0
        while not stopping.is_set():
            try:
                content = raw.get(timeout=.2)
            except queue.Empty:
                continue
            for packet in codec.parse(content):
                try:
                    frames = codec.decode(packet)
                except av.error.InvalidDataError:
                    continue  # A dropped H264 packet can recover at the next keyframe.
                for frame in frames:
                    matrix = frame.to_ndarray(format='bgr24')
                    frame_time = time.monotonic()
                    last_frame.value = frame_time

                    # Keep the outbound SRT feed at its existing size and frame rate.
                    if ffmpeg:
                        cropped_full = _apply_crop(matrix, (1152, 864))
                        data = memoryview(cropped_full.tobytes())
                        write_end = time.monotonic() + 2
                        while data and not stopping.is_set():
                            if ffmpeg.poll() is not None or time.monotonic() > write_end:
                                raise RuntimeError('SRT encoder stopped or receiver is unavailable.')
                            if select.select([], [ffmpeg.stdin], [], .1)[1]:
                                try:
                                    data = data[os.write(ffmpeg.stdin.fileno(), data):]
                                except BlockingIOError:
                                    pass

                    if frame_time < next_preview_at:
                        continue
                    next_preview_at = frame_time + 1 / PREVIEW_FPS

                    cropped = _apply_crop(matrix)
                    if shape != matrix.shape:
                        shape = matrix.shape
                        maps = generate_equirectangular_maps(shape[1], shape[0])
                    equi = cv2.remap(matrix, *maps, cv2.INTER_LINEAR)
                    equi = cv2.resize(equi, (640, 320))
                    for output, img in ((cropped_q, cropped), (equi_q, equi)):
                        ok, jpeg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 60])
                        if not ok:
                            raise RuntimeError('JPEG encoding failed.')
                        _safe_q_put(output, jpeg.tobytes())
                    ready.set()
    except Exception as exc:
        errors.put(f'{type(exc).__name__}: {exc}')
    finally:
        if ffmpeg:
            ffmpeg.terminate()
            try:
                ffmpeg.wait(timeout=1)
            except subprocess.TimeoutExpired:
                ffmpeg.kill()
                ffmpeg.wait(timeout=1)
        if client:
            try:
                if client.preview_stream_started:
                    client.stop_preview_stream()
            finally:
                client.close()


def generate_equirectangular_maps(w, h, config=None):
    """
    Production-ready LUT Generator for Dual-Fisheye to Equirectangular conversion.
    Includes tuning parameters to fix seam alignment, FOV scaling, and optical centers.
    """
    import numpy as np

    if config is None:
        config = {
            "fov_deg": 194,         # Typical dual-fisheye lenses have > 180 deg FOV
            "yaw_offset_deg": 0.0,    # Overall horizontal rotation
            "cx1_offset": 0.0,        # Front lens X center offset
            "cy1_offset": 0.0,        # Front lens Y center offset
            "cx2_offset": 0.0,        # Rear lens X center offset
            "cy2_offset": 0.0,        # Rear lens Y center offset
            "radius_scale": 1.0       # Fine-tune radius scaling
        }

    out_w, out_h = w, w // 2
    u, v = np.meshgrid(np.linspace(0, 1, out_w), np.linspace(0, 1, out_h))

    yaw_offset_rad = np.radians(config["yaw_offset_deg"])
    max_theta = np.radians(config["fov_deg"]) / 2.0

    # Longitude (theta) and Latitude (phi)
    theta = (u - 0.5) * 2 * np.pi + yaw_offset_rad
    phi = (0.5 - v) * np.pi

    # 3D Cartesian coordinates on a unit sphere
    x = np.cos(phi) * np.cos(theta)  # Forward/Backward
    y = np.cos(phi) * np.sin(theta)  # Left/Right
    z = np.sin(phi)                  # Up/Down

    # Lens centers with configurable offsets
    lens_radius = (w / 4) * config["radius_scale"]
    cx1 = (w / 4) + config["cx1_offset"]
    cy1 = (h / 2) + config["cy1_offset"]
    cx2 = (3 * w / 4) + config["cx2_offset"]
    cy2 = (h / 2) + config["cy2_offset"]

    front_mask = x >= 0

    fisheye_x = np.zeros_like(u, dtype=np.float32)
    fisheye_y = np.zeros_like(v, dtype=np.float32)

    # --- FRONT LENS MAPPING (x >= 0) ---
    theta_front = np.arccos(np.clip(x[front_mask], -1.0, 1.0))
    r_front = lens_radius * (theta_front / max_theta)
    alpha_front = np.arctan2(z[front_mask], y[front_mask])

    fisheye_x[front_mask] = cx1 + r_front * np.cos(alpha_front)
    fisheye_y[front_mask] = cy1 - r_front * np.sin(alpha_front)

    # --- REAR LENS MAPPING (x < 0) ---
    theta_rear = np.arccos(np.clip(-x[~front_mask], -1.0, 1.0))
    r_rear = lens_radius * (theta_rear / max_theta)
    alpha_rear = np.arctan2(z[~front_mask], -y[~front_mask])

    fisheye_x[~front_mask] = cx2 + r_rear * np.cos(alpha_rear)
    fisheye_y[~front_mask] = cy2 - r_rear * np.sin(alpha_rear)

    return fisheye_x, fisheye_y
