import asyncio
import logging
import os
import queue
import smtplib
import threading
import time
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import arm_control
import station
from camera_connection import CameraConnectionManager, CameraError
from stream_manager import UnifiedStreamManager

# --- CONFIGURATION ---
GMAIL_USERNAME, GMAIL_USERNAME_UNSET = station.get("email", "sender")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

# Clients poll these twice a second; their successful GETs would bury the camera log.
POLLED_PATHS = ("/status", "/stream", "/stream/equirec")
# With the arm out and no request for this long, the station disconnects the camera and brings the arm home.
IDLE_SHUTOFF_S = 120.0
IDLE_CHECK_S = 5.0


class QuietPolls(logging.Filter):
    """Drop successful GETs of the polled paths from uvicorn's access log; errors stay."""

    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5 and args[1] == "GET" and str(args[2]).split("?")[0] in POLLED_PATHS:
            try:
                return int(args[4]) >= 400
            except (TypeError, ValueError):
                return True
        return True


logging.getLogger("uvicorn.access").addFilter(QuietPolls())


def _who(request: Request) -> str:
    return request.client.host if request.client else ""


stream_manager = UnifiedStreamManager()
controller = CameraConnectionManager(stream_manager)

@asynccontextmanager
async def lifespan(app):
    controller.start()
    stop = threading.Event()
    threading.Thread(target=_watch_idle, args=(stop,), name="idle-shutoff", daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        await asyncio.to_thread(controller.close)

app = FastAPI(lifespan=lifespan)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_last_activity = time.monotonic()


@app.middleware("http")
async def note_activity(request: Request, call_next):
    """Every POST is someone using the station, so the idle shutoff counts from the latest one."""
    global _last_activity
    if request.method == "POST":
        _last_activity = time.monotonic()
    return await call_next(request)


# --- IMAGE PROCESSING ---
def crop_front_lens(image_path: str):
    img = cv2.imread(image_path)
    if img is None:
        raise Exception(f"Could not read image at {image_path} for cropping.")

    h, w, _ = img.shape
    lens_w = w // 2
    front_lens = img[:, 0:lens_w]
    cx, cy = lens_w // 2, h // 2
    crop_w = int(lens_w * 0.60)
    crop_h = int(crop_w * 0.75)
    y1, y2 = cy - (crop_h // 2), cy + (crop_h // 2)
    x1, x2 = cx - (crop_w // 2), cx + (crop_w // 2)
    cv2.imwrite(image_path, front_lens[y1:y2, x1:x2])

def apply_border_and_logo(
    image_path: str,
    logo_path: str,
    border_width: int = 200,
    border_color: tuple = (0, 53, 149)
) -> None:
    img = cv2.imread(image_path)
    if img is None:
        raise Exception(f"Could not read image at {image_path} for border overlay.")

    bgr_border_color = (border_color[2], border_color[1], border_color[0])
    canvas = cv2.copyMakeBorder(
        img, 0, border_width, 0, 0,
        cv2.BORDER_CONSTANT, value=bgr_border_color
    )

    if os.path.exists(logo_path):
        logo = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
        if logo is not None and logo.shape[2] == 4:
            max_logo_height = int(border_width * 0.8)
            if logo.shape[0] > max_logo_height:
                scale_factor = max_logo_height / logo.shape[0]
                new_width = int(logo.shape[1] * scale_factor)
                logo = cv2.resize(logo, (new_width, max_logo_height), interpolation=cv2.INTER_AREA)

            logo_bgr = logo[:, :, 0:3]
            alpha_channel = logo[:, :, 3]
            alpha_factor = alpha_channel.astype(float) / 255.0
            alpha_factor = np.dstack([alpha_factor, alpha_factor, alpha_factor])

            logo_h, logo_w, _ = logo_bgr.shape
            canvas_h, canvas_w, _ = canvas.shape

            margin = int(border_width * 0.1)
            y1 = canvas_h - logo_h - margin
            y2 = canvas_h - margin
            x1 = canvas_w - logo_w - margin
            x2 = canvas_w - margin

            roi = canvas[y1:y2, x1:x2].astype(float)
            logo_fg = logo_bgr.astype(float)
            blended = cv2.multiply(logo_fg, alpha_factor) + cv2.multiply(roi, 1.0 - alpha_factor)
            canvas[y1:y2, x1:x2] = blended.astype(np.uint8)

    cv2.imwrite(image_path, canvas)

# --- API ROUTES ---
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

async def mjpeg_generator(queue_name):
    """Yields MJPEG stream boundaries from the multiprocessing queue."""
    while True:
        try:
            q = getattr(stream_manager, queue_name)
            frame_bytes = await asyncio.to_thread(q.get, timeout=1.0)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        except (queue.Empty, ValueError, OSError):
            await asyncio.sleep(0.1)

@app.get("/stream")
async def stream_feed_cropped():
    # Auto-resume the RTMP pipeline if it was paused by a capture
    if not stream_manager.processing_process or not stream_manager.processing_process.is_alive():
        srt_ip, srt_port = stream_manager.destination or ('', 7003)
        stream_manager.start(srt_ip=srt_ip, srt_port=srt_port)

    return StreamingResponse(
        mjpeg_generator("mjpeg_queue_cropped"),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/stream/equirec")
async def stream_feed_equirec():
    # Auto-resume the RTMP pipeline if it was paused by a capture
    if not stream_manager.processing_process or not stream_manager.processing_process.is_alive():
        srt_ip, srt_port = stream_manager.destination or ('', 7003)
        stream_manager.start(srt_ip=srt_ip, srt_port=srt_port)

    return StreamingResponse(
        mjpeg_generator("mjpeg_queue_equirec"),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/status")
def get_status(operation_id: str | None = None):
    try:
        return controller.status(operation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown camera operation.")

@app.post("/connect", status_code=202)
def connect_camera(request: Request, client_ip: str = "", ensure: bool = False):
    try:
        return {"operation_id": controller.connect(client_ip, client=_who(request), ensure=ensure)}
    except CameraError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@app.post("/position-arm")
def position_arm(request: Request, takeover: bool = False):
    try:
        arm_control.move_arm_to_selfie(takeover=takeover)
        return {"status": "success"}
    except arm_control.ArmLeaseConflict as exc:
        arm_control.LOG.warning("position-arm from %s: the arm is held by %s", _who(request), exc.owner)
        raise HTTPException(status_code=409, detail={"kind": "arm_lease_conflict", "owner": exc.owner})
    except Exception as e:
        arm_control.LOG.warning("position-arm from %s failed: %s", _who(request), e)
        raise HTTPException(status_code=500, detail=f"Arm positioning failed: {e}")

@app.post("/activity", status_code=204)
def keep_active():
    """The page's sign that someone is still using it; the middleware records it."""


@app.post("/home-arm")
def home_arm():
    try:
        homed = arm_control.move_arm_home()
        return {"status": "success", "homed": homed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Arm return failed: {e}")

@app.post("/capture")
def capture_image(request: Request):
    try:
        # Blocks until the raw dual-fisheye image is downloaded
        with arm_control.selfie_pose_guard():
            result = controller.capture(client=_who(request))

        # Post-process the downloaded high-res image
        image_path = "static/latest.jpg"
        logo_path = "static/logo.png"

        if os.path.exists(image_path):
            crop_front_lens(image_path)
            apply_border_and_logo(image_path, logo_path)

        return result
    except arm_control.ArmNotPosed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except CameraError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Image processing failed: {exc}")

@app.post("/email")
def trigger_email(email: str, image: str):
    if not GMAIL_USERNAME:
        raise HTTPException(status_code=503, detail=GMAIL_USERNAME_UNSET)
    if not GMAIL_PASSWORD:
        raise HTTPException(status_code=503, detail="GMAIL_APP_PASSWORD is not configured.")
    clean_path = image.split("?")[0].lstrip("/")
    if Path(clean_path).resolve() != Path("static/latest.jpg").resolve():
        raise HTTPException(status_code=400, detail="Invalid image path.")
    if not os.path.exists(clean_path):
        raise HTTPException(status_code=404, detail="Image not found.")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Your Selfie!", GMAIL_USERNAME, email
    msg.set_content("Here is your photo!")

    try:
        with open(clean_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="image", subtype="jpeg", filename="selfie.jpg")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_USERNAME, GMAIL_PASSWORD)
            server.send_message(msg)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def home_and_release_arm():
    arm_control.move_arm_home()
    arm_control.arm_release()


def check_idle(now):
    """Shut the station off once the arm has been out with no request and no camera work for IDLE_SHUTOFF_S."""
    global _last_activity
    if not arm_control.deployed():
        return
    if not controller.status().get("done", True):
        _last_activity = now
        return
    if now - _last_activity < IDLE_SHUTOFF_S:
        return
    arm_control.LOG.warning("no one has used the station for %.0f s with the arm out; disconnecting the camera "
                            "and bringing the arm home along its poses", now - _last_activity)
    _last_activity = now
    controller.disconnect(home_and_release_arm, client="the 2-minute idle shutoff")


def _watch_idle(stop):
    while not stop.wait(IDLE_CHECK_S):
        try:
            check_idle(time.monotonic())
        except Exception:
            arm_control.LOG.exception("idle shutoff check failed")

@app.post("/disconnect", status_code=202)
def disconnect_camera(request: Request, home_arm: bool = False):
    try:
        return {"operation_id": controller.disconnect(home_and_release_arm if home_arm else None,
                                                      client=_who(request))}
    except CameraError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
