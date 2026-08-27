import asyncio
import os
import re
import smtplib
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Tuple

import av
import cv2
import httpx
import numpy as np
import arm_control
from insta360.rtmp import Client
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --- CONFIGURATION ---
CAMERA_SSID = os.getenv("CAMERA_SSID", "X5 135VYD.OSC")
CAMERA_SSID_PATTERN = os.getenv("CAMERA_SSID_PATTERN", r"\.OSC$")
CAMERA_IP = os.getenv("CAMERA_IP", "192.168.42.1")
WIFI_PROFILE_NAME = os.getenv("WIFI_PROFILE_NAME", "Insta360")
WIFI_INTERFACE = os.getenv("WIFI_INTERFACE", "wlx9cefd5f89420")
CAPTURE_POLL_ATTEMPTS = int(os.getenv("CAPTURE_POLL_ATTEMPTS", "40"))
CAPTURE_DOWNLOAD_RETRIES = int(os.getenv("CAPTURE_DOWNLOAD_RETRIES", "3"))
GMAIL_USERNAME = os.getenv("GMAIL_USERNAME", "olearyd74@gmail.com")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

STATIC_DIR = Path("static").resolve()
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Thread pool for CPU-bound OpenCV operations
cpu_pool = ThreadPoolExecutor(max_workers=4)

# --- GLOBAL THREAD-SAFE STATE ---
class AppState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.status: str = "idle"
        self.message: str = "Waiting to connect..."
        self.progress: int = 0
        self.stream_active: bool = False
        self.codec: Optional[av.CodecContext] = None
        self.map_x: Optional[np.ndarray] = None
        self.map_y: Optional[np.ndarray] = None

    async def update(self, status: str, message: str, progress: int):
        async with self.lock:
            self.status = status
            self.message = message
            self.progress = progress
        print(f"[{progress}%] {message}")

    async def get_state(self) -> dict:
        async with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "progress": self.progress,
                "stream_active": self.stream_active,
            }

state = AppState()
rtmp_client = Client()

frame_queue_cropped: asyncio.Queue = asyncio.Queue(maxsize=3)
frame_queue_equirec: asyncio.Queue = asyncio.Queue(maxsize=3)


# --- EQUIRECTANGULAR PROJECTION (LUT) ---
def generate_equirectangular_maps(w: int, h: int, config: Optional[dict] = None) -> Tuple[np.ndarray, np.ndarray]:
    if config is None:
        config = {
            "fov_deg": 194.0,
            "yaw_offset_deg": 0.0,
            "cx1_offset": 0.0, "cy1_offset": 0.0,
            "cx2_offset": 0.0, "cy2_offset": 0.0,
            "radius_scale": 1.0
        }

    out_w, out_h = w, w // 2
    u, v = np.meshgrid(
        np.linspace(0.0, 1.0, out_w, dtype=np.float32),
        np.linspace(0.0, 1.0, out_h, dtype=np.float32)
    )

    yaw_rad = np.radians(config["yaw_offset_deg"], dtype=np.float32)
    max_theta = np.radians(config["fov_deg"], dtype=np.float32) / 2.0

    theta = (u - 0.5) * (2.0 * np.pi) + yaw_rad
    phi = (0.5 - v) * np.pi

    x = np.cos(phi) * np.cos(theta)
    y = np.cos(phi) * np.sin(theta)
    z = np.sin(phi)

    lens_radius = (w / 4.0) * config["radius_scale"]
    cx1, cy1 = (w / 4.0) + config["cx1_offset"], (h / 2.0) + config["cy1_offset"]
    cx2, cy2 = (3.0 * w / 4.0) + config["cx2_offset"], (h / 2.0) + config["cy2_offset"]

    front_mask = x >= 0
    fisheye_x = np.zeros_like(u)
    fisheye_y = np.zeros_like(v)

    # Front Lens
    theta_f = np.arccos(np.clip(x[front_mask], -1.0, 1.0))
    r_f = lens_radius * (theta_f / max_theta)
    alpha_f = np.arctan2(z[front_mask], y[front_mask])
    fisheye_x[front_mask] = cx1 + r_f * np.cos(alpha_f)
    fisheye_y[front_mask] = cy1 - r_f * np.sin(alpha_f)

    # Rear Lens
    theta_r = np.arccos(np.clip(-x[~front_mask], -1.0, 1.0))
    r_r = lens_radius * (theta_r / max_theta)
    alpha_r = np.arctan2(z[~front_mask], -y[~front_mask])
    fisheye_x[~front_mask] = cx2 + r_r * np.cos(alpha_r)
    fisheye_y[~front_mask] = cy2 - r_r * np.sin(alpha_r)

    return fisheye_x, fisheye_y


def _process_frame_sync(img_matrix: np.ndarray, map_x: np.ndarray, map_y: np.ndarray) -> Tuple[bytes, bytes]:
    """CPU-bound task: crops front lens and generates equirectangular remap."""
    h, w, _ = img_matrix.shape
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 60]

    # 1. Cropped Front Lens
    lens_w = w // 2
    front_lens = img_matrix[:, 0:lens_w]
    cx, cy = lens_w // 2, h // 2
    crop_w = int(lens_w * 0.60)
    crop_h = int(crop_w * 0.75)
    y1, y2 = cy - (crop_h // 2), cy + (crop_h // 2)
    x1, x2 = cx - (crop_w // 2), cx + (crop_w // 2)
    _, jpeg_cropped = cv2.imencode('.jpg', front_lens[y1:y2, x1:x2], encode_param)

    # 2. Equirectangular Projection
    equi_frame = cv2.remap(img_matrix, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    _, jpeg_equi = cv2.imencode('.jpg', equi_frame, encode_param)

    return jpeg_cropped.tobytes(), jpeg_equi.tobytes()


@rtmp_client.on_video_stream(wait=True)
async def process_live_frame(**kwargs):
    if not state.stream_active or state.codec is None:
        return

    content = kwargs.get('content') or kwargs.get('data') or kwargs.get('payload') or kwargs.get('buffer')
    if not content:
        return

    try:
        packets = state.codec.parse(content)
        loop = asyncio.get_running_loop()

        for packet in packets:
            frames = state.codec.decode(packet)
            for frame in frames:
                if not state.stream_active:
                    return

                img_matrix = frame.to_ndarray(format='bgr24')
                h, w, _ = img_matrix.shape

                # Cache LUT grids
                if state.map_x is None or state.map_y is None or state.map_x.shape[1] != w:
                    state.map_x, state.map_y = generate_equirectangular_maps(w, h)

                # Offload OpenCV remap + encoding off the async loop
                jpeg_crop_bytes, jpeg_equi_bytes = await loop.run_in_executor(
                    cpu_pool, _process_frame_sync, img_matrix, state.map_x, state.map_y
                )

                # Push to cropped queue
                if frame_queue_cropped.full():
                    try:
                        frame_queue_cropped.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await frame_queue_cropped.put(jpeg_crop_bytes)

                # Push to equirectangular queue
                if frame_queue_equirec.full():
                    try:
                        frame_queue_equirec.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await frame_queue_equirec.put(jpeg_equi_bytes)

    except Exception as e:
        print(f"RTMP Decode Error: {e}")


def _start_rtmp_sync():
    state.codec = av.CodecContext.create('h264', 'r')
    rtmp_client.open()
    rtmp_client.start_preview_stream()

def _stop_rtmp_sync():
    try:
        rtmp_client.close()
    except Exception:
        pass


async def start_rtmp_stream():
    if state.stream_active:
        return
    state.stream_active = True
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _start_rtmp_sync)


async def stop_rtmp_stream():
    state.stream_active = False
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _stop_rtmp_sync)

    for q in (frame_queue_cropped, frame_queue_equirec):
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break


async def video_generator(queue: asyncio.Queue):
    frame_delay = 1.0 / 20.0
    while state.stream_active:
        try:
            frame_bytes = await asyncio.wait_for(queue.get(), timeout=1.0)
            yield (
                b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
            )
            await asyncio.sleep(frame_delay)
        except asyncio.TimeoutError:
            continue
        except Exception:
            break


# --- SYSTEM RUNNERS & WIFI LOGIC ---
async def run_cmd(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    )

def find_camera_ssid(scan_output: str) -> Optional[str]:
    """Prefer the configured SSID, else the first AP matching CAMERA_SSID_PATTERN."""
    ssids = [line.strip() for line in scan_output.splitlines() if line.strip()]
    if CAMERA_SSID in ssids:
        return CAMERA_SSID
    for ssid in ssids:
        if re.search(CAMERA_SSID_PATTERN, ssid, re.IGNORECASE):
            return ssid
    return None


async def sync_profile_ssid(ssid: str) -> None:
    """Repoint the pinned profile when the camera advertises a different SSID."""
    res = await run_cmd(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", WIFI_PROFILE_NAME], timeout=5.0)
    if res.returncode == 0 and res.stdout.strip() == ssid:
        return
    mod = await run_cmd(
        ["sudo", "-n", "nmcli", "connection", "modify", WIFI_PROFILE_NAME, "802-11-wireless.ssid", ssid],
        timeout=10.0,
    )
    if mod.returncode != 0:
        print(f"[!] Could not repoint {WIFI_PROFILE_NAME} to {ssid}: {mod.stderr.strip()}")
    else:
        print(f"[*] Repointed {WIFI_PROFILE_NAME} to SSID {ssid}")


async def robust_wifi_connect(interface: str, profile_name: str, ssid: str, max_retries: int = 3) -> bool:
    for attempt in range(1, max_retries + 1):
        await state.update("connecting", f"Connecting to AP profile (Attempt {attempt}/{max_retries})...", 60 + (attempt * 5))
        await run_cmd(["nmcli", "device", "disconnect", interface], timeout=5.0)
        await asyncio.sleep(0.5)

        await run_cmd(["nmcli", "device", "wifi", "rescan", "ifname", interface], timeout=6.0)
        await asyncio.sleep(1.0)

        connect_res = await run_cmd(
            ["nmcli", "--wait", "12", "connection", "up", profile_name, "ifname", interface],
            timeout=15.0
        )
        if connect_res.returncode == 0:
            return True

        print(f"[!] Handshake attempt {attempt} failed: {connect_res.stderr.strip()}")

        if attempt == 2:
            fallback_res = await run_cmd(
                ["nmcli", "--wait", "12", "device", "wifi", "connect", ssid, "ifname", interface],
                timeout=15.0
            )
            if fallback_res.returncode == 0:
                return True

        await asyncio.sleep(1.5)

    return False


# --- FASTAPI APP & LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await stop_rtmp_stream()
    # Release the arm/base lease on shutdown; nothing else drops it.
    try:
        arm_control.arm_release()
    except Exception as e:
        print(f"[selfie] arm release on shutdown failed: {e}")
    cpu_pool.shutdown(wait=False)

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --- ENDPOINTS ---

@app.get("/")
async def serve_frontend():
    index_path = Path("index.html").resolve()
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="index.html not found in project root.")
    return FileResponse(index_path, media_type="text/html")


@app.get("/stream")
async def stream_feed_cropped():
    """Initializes the RTMP stream and serves the cropped MJPEG feed."""
    try:
        await start_rtmp_stream()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start live stream: {e}")
    return StreamingResponse(video_generator(frame_queue_cropped), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/stream/equirec")
async def stream_feed_equirec():
    """Initializes the RTMP stream and serves the equirectangular MJPEG feed."""
    try:
        await start_rtmp_stream()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start live stream: {e}")
    return StreamingResponse(video_generator(frame_queue_equirec), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/status")
async def get_status():
    return await state.get_state()


@app.post("/connect")
async def connect_camera():
    await state.update("connecting", "Initializing Wi-Fi interface...", 5)
    await run_cmd(["nmcli", "device", "set", WIFI_INTERFACE, "managed", "yes"], timeout=5.0)
    await run_cmd(["nmcli", "device", "disconnect", WIFI_INTERFACE], timeout=5.0)

    camera_ssid = None
    scan_passes = 4
    for i in range(scan_passes):
        await state.update("connecting", f"Scanning for camera AP ({i+1}/{scan_passes})...", 10 + i * 12)
        try:
            rescan_res = await run_cmd(["nmcli", "device", "wifi", "rescan", "ifname", WIFI_INTERFACE], timeout=15.0)
            if rescan_res.returncode != 0:
                print(f"[!] Rescan pass {i+1} failed: {rescan_res.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"[!] Rescan pass {i+1} timed out after 15s")

        # rescan returns immediately; a full scan takes ~11s to populate
        await asyncio.sleep(12.0)
        res = await run_cmd(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", WIFI_INTERFACE])
        camera_ssid = find_camera_ssid(res.stdout)
        if camera_ssid:
            break

    if not camera_ssid:
        await state.update("error", "SSID not found. Verify camera Wi-Fi is enabled.", 0)
        raise HTTPException(status_code=504, detail="Camera SSID scan timed out.")

    await sync_profile_ssid(camera_ssid)
    connected = await robust_wifi_connect(WIFI_INTERFACE, WIFI_PROFILE_NAME, camera_ssid, max_retries=3)
    if not connected:
        await state.update("error", "Wi-Fi Handshake failed.", 0)
        raise HTTPException(status_code=502, detail="Failed to negotiate Wi-Fi connection with camera.")

    # DHCP Verification
    await state.update("connecting", "Verifying network route...", 80)
    for _ in range(8):
        route_check = await run_cmd(["ip", "route", "show", "dev", WIFI_INTERFACE], timeout=3.0)
        if "192.168.42." in route_check.stdout or CAMERA_IP in route_check.stdout:
            break
        await asyncio.sleep(0.5)

    # OSC API Health Check
    await state.update("connecting", "Polling Camera OSC API...", 90)
    async with httpx.AsyncClient(timeout=1.5) as client:
        ready = False
        for _ in range(10):
            try:
                r = await client.get(f"http://{CAMERA_IP}/osc/info")
                if r.status_code == 200:
                    ready = True
                    break
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.5)

    if not ready:
        await run_cmd(["nmcli", "connection", "down", WIFI_PROFILE_NAME], timeout=5.0)
        await state.update("error", "Camera connected but HTTP OSC server is unreachable.", 0)
        raise HTTPException(status_code=502, detail="OSC API unreachable.")

    await state.update("connected", "Camera successfully connected and ready.", 100)
    return {"status": "success"}


@app.post("/position-arm")
async def position_arm():
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, arm_control.move_arm_to_selfie)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Arm positioning failed: {e}")


def _post_process_capture(raw_path: Path, processed_path: Path, logo_path: Path) -> None:
    img = cv2.imread(str(raw_path))
    if img is None:
        raise ValueError("Failed to load captured raw image.")

    h, w, _ = img.shape
    lens_w = w // 2
    cx, cy = lens_w // 2, h // 2
    crop_w = int(lens_w * 0.60)
    crop_h = int(crop_w * 0.75)
    cropped = img[cy - crop_h // 2 : cy + crop_h // 2, cx - crop_w // 2 : cx + crop_w // 2]

    border_width = 200
    canvas = cv2.copyMakeBorder(cropped, 0, border_width, 0, 0, cv2.BORDER_CONSTANT, value=(149, 53, 0))

    if logo_path.exists():
        logo = cv2.imread(str(logo_path), cv2.IMREAD_UNCHANGED)
        if logo is not None and logo.shape[2] == 4:
            max_h = int(border_width * 0.8)
            if logo.shape[0] > max_h:
                scale = max_h / logo.shape[0]
                logo = cv2.resize(logo, (int(logo.shape[1] * scale), max_h), interpolation=cv2.INTER_AREA)

            l_bgr = logo[:, :, :3]
            alpha = (logo[:, :, 3].astype(np.float32) / 255.0)[:, :, None]

            lh, lw, _ = l_bgr.shape
            ch, cw, _ = canvas.shape
            margin = int(border_width * 0.1)
            y1, y2 = ch - lh - margin, ch - margin
            x1, x2 = cw - lw - margin, cw - margin

            roi = canvas[y1:y2, x1:x2].astype(np.float32)
            blended = (l_bgr.astype(np.float32) * alpha) + (roi * (1.0 - alpha))
            canvas[y1:y2, x1:x2] = blended.astype(np.uint8)

    cv2.imwrite(str(processed_path), canvas)


async def download_capture(client: httpx.AsyncClient, file_url: str) -> bytes:
    """Fetch the captured image, retrying transient network failures."""
    last_error: Optional[Exception] = None
    for attempt in range(1, CAPTURE_DOWNLOAD_RETRIES + 1):
        try:
            resp = await client.get(file_url, timeout=90.0)
            if resp.status_code == 200:
                return resp.content
            last_error = httpx.RequestError(f"HTTP {resp.status_code} fetching image")
        except httpx.RequestError as e:
            last_error = e
        print(f"[!] Image download attempt {attempt}/{CAPTURE_DOWNLOAD_RETRIES} failed: {last_error}")
        await asyncio.sleep(1.5)
    raise last_error


@app.post("/capture")
async def capture_image():
    await stop_rtmp_stream()
    await asyncio.sleep(0.5)

    async with httpx.AsyncClient(timeout=10.0) as client:
        stage = "takePicture"
        try:
            resp = await client.post(f"http://{CAMERA_IP}/osc/commands/execute", json={"name": "camera.takePicture"})
            data = resp.json()
            if data.get("state") == "error":
                raise HTTPException(status_code=400, detail="Camera rejected takePicture command.")

            cmd_id = data["id"]
            file_url = None

            stage = "status poll"
            for _ in range(CAPTURE_POLL_ATTEMPTS):
                await asyncio.sleep(0.5)
                st_resp = await client.post(f"http://{CAMERA_IP}/osc/commands/status", json={"id": cmd_id})
                st_data = st_resp.json()
                if st_data.get("state") == "done":
                    file_url = st_data.get("results", {}).get("fileUrl")
                    break
                if st_data.get("state") == "error":
                    raise HTTPException(status_code=400, detail="Camera reported an error during capture.")

            if not file_url:
                raise HTTPException(status_code=504, detail="Capture timed out on camera storage.")

            stage = "image download"
            raw_bytes = await download_capture(client, file_url)

        except httpx.RequestError as e:
            print(f"[!] Capture failed during {stage}: {type(e).__name__}: {e}")
            raise HTTPException(status_code=502, detail=f"OSC network error during {stage}: {e}")

    file_id = uuid.uuid4().hex[:8]
    raw_file = STATIC_DIR / f"raw_{file_id}.jpg"
    final_file = STATIC_DIR / f"selfie_{file_id}.jpg"
    logo_file = STATIC_DIR / "logo.png"

    raw_file.write_bytes(raw_bytes)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(cpu_pool, _post_process_capture, raw_file, final_file, logo_file)
    raw_file.unlink(missing_ok=True)

    return {"status": "success", "file_name": final_file.name, "url": f"/static/{final_file.name}"}


@app.post("/email")
async def trigger_email(email: str = Query(...), filename: str = Query(...)):
    target_path = (STATIC_DIR / Path(filename).name).resolve()
    if not target_path.is_relative_to(STATIC_DIR) or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Invalid image file requested.")

    if not GMAIL_PASSWORD:
        raise HTTPException(status_code=500, detail="Mail credentials not configured.")

    msg = EmailMessage()
    msg["Subject"] = "Your Robot Selfie!"
    msg["From"] = GMAIL_USERNAME
    msg["To"] = email
    msg.set_content("Here is your photo attached!")
    msg.add_attachment(target_path.read_bytes(), maintype="image", subtype="jpeg", filename=target_path.name)

    def _send():
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_USERNAME, GMAIL_PASSWORD)
            server.send_message(msg)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _send)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dispatch email: {e}")

    return {"status": "success"}


@app.post("/disconnect")
async def disconnect_camera(home_arm: bool = False):
    await stop_rtmp_stream()

    arm_error = None
    if home_arm:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, arm_control.move_arm_home)
            await loop.run_in_executor(None, arm_control.arm_release)
        except Exception as e:
            arm_error = str(e)

    # Trigger power off via OSC
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"http://{CAMERA_IP}/osc/commands/execute",
                json={"name": "camera.setOptions", "parameters": {"options": {"sleepDelay": 15, "offDelay": 30}}}
            )
    except Exception:
        pass

    await run_cmd(["nmcli", "connection", "down", WIFI_PROFILE_NAME], timeout=5.0)
    await state.update("idle", "Camera disconnected.", 0)

    if arm_error:
        return {"status": "warning", "message": f"Camera disconnected; arm release issue: {arm_error}"}
    return {"status": "success", "message": "Camera disconnected."}