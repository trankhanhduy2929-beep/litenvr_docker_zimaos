import os
import asyncio
import ipaddress
import json
import time
import base64
import hmac
import shutil
import socket
import hashlib
import sqlite3
import secrets
import mimetypes
import threading
import subprocess
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
import uuid
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import anyio
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

# Đảm bảo trình duyệt nhận đúng MIME cho HLS (nếu thiếu, live view không phát được)
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

NVR_USER = os.environ.get("NVR_USER", "admin")
NVR_PASS = os.environ.get("NVR_PASS", "admin")
AUTH_ENABLED = os.environ.get("NVR_AUTH", "on").lower() not in ("off", "0", "false", "no")
APP_VERSION = (os.environ.get("LITENVR_VERSION") or "1.9.23").strip() or "1.9.23"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_CAND = os.path.join(BASE_DIR, "static")
STATIC_DIR = _STATIC_CAND if os.path.isdir(_STATIC_CAND) else os.path.abspath("static")

STORAGE_DIR = "/storage"
CONFIG_PATH = os.path.join(STORAGE_DIR, "config.json")
RECORDINGS_DIR = os.path.join(STORAGE_DIR, "recordings")
SNAPSHOTS_DIR = os.path.join(STORAGE_DIR, "snapshots")
DB_PATH = os.path.join(STORAGE_DIR, "nvr.db")
HLS_DIR = "/dev/shm/hls"
PLAYBACK_HLS_DIR = os.path.join(STORAGE_DIR, "playback_hls")
BUFFER_DIR = "/dev/shm/buffer"
THUMBS_DIR = os.path.join(STORAGE_DIR, "thumbnails")
LEGACY_THUMBS_DIR = os.path.join(BUFFER_DIR, "thumbs")
THUMB_CACHE_MAX_MB = 256
PLAYBACK_CACHE_DIR = os.path.join(STORAGE_DIR, "playback_cache")
MQTT_SNAPSHOT_DIR = os.path.join(BUFFER_DIR, "mqtt")
MQTT_DISCOVERY_FILE = os.path.join(STORAGE_DIR, "mqtt_discovery.json")
PLAYBACK_CACHE_MB = max(64, int(os.environ.get("PLAYBACK_CACHE_MB", "2048") or "2048"))
PLAYBACK_CACHE_TTL_SEC = max(300, int(os.environ.get("PLAYBACK_CACHE_TTL_SEC", "3600") or "3600"))
PLAYBACK_TRANSCODE_MAX = max(1, min(4, int(os.environ.get("PLAYBACK_TRANSCODE_MAX", "2") or "2")))
_PLAYBACK_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
PLAYBACK_PRESET = (os.environ.get("PLAYBACK_PRESET", "ultrafast") or "").strip()
if PLAYBACK_PRESET not in _PLAYBACK_PRESETS:
    PLAYBACK_PRESET = "ultrafast"
PLAYBACK_CRF = max(18, min(30, int(os.environ.get("PLAYBACK_CRF", "23") or "23")))

# Google Drive (24/7 backup) — các URL có thể override qua env để test bằng mock server
DRIVE_OAUTH_BASE = os.environ.get("NVR_DRIVE_OAUTH_BASE", "https://oauth2.googleapis.com").rstrip("/")
DRIVE_API_BASE = os.environ.get("NVR_DRIVE_API_BASE", "https://www.googleapis.com/drive/v3").rstrip("/")
DRIVE_UPLOAD_BASE = os.environ.get("NVR_DRIVE_UPLOAD_BASE", "https://www.googleapis.com/upload/drive/v3").rstrip("/")
DRIVE_SCOPE = os.environ.get("NVR_DRIVE_SCOPE", "https://www.googleapis.com/auth/drive.file")
DRIVE_TICK = float(os.environ.get("NVR_DRIVE_TICK", "20"))
DRIVE_CHUNK = int(os.environ.get("NVR_DRIVE_CHUNK", str(16 * 1024 * 1024)))
DRIVE_RETRY_BASE = float(os.environ.get("NVR_DRIVE_RETRY_BASE", "60"))
DRIVE_MAX_PARALLEL = 64

for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, HLS_DIR, PLAYBACK_HLS_DIR, BUFFER_DIR, THUMBS_DIR, PLAYBACK_CACHE_DIR, MQTT_SNAPSHOT_DIR]:
    os.makedirs(d, exist_ok=True)

# ---- Giám sát tài nguyên (CPU / RAM / ổ đĩa) cho dashboard ----
_cpu_prev = {"idle": 0, "total": 0, "init": False}
stats_history: deque = deque(maxlen=360)   # ~6 phút nếu lấy mẫu mỗi 1 giây
_stats_lock = threading.Lock()
_cpu_lock = threading.Lock()
latest_stats: dict = {}

def read_cpu_percent() -> float:
    with _cpu_lock:
        try:
            with open("/proc/stat", "r") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
        except Exception:
            return 0.0
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        if not _cpu_prev["init"]:
            _cpu_prev.update(idle=idle, total=total, init=True)
            return 0.0
        didle = idle - _cpu_prev["idle"]
        dtotal = total - _cpu_prev["total"]
        _cpu_prev.update(idle=idle, total=total)
        return round(100.0 * (1 - didle / dtotal), 1) if dtotal > 0 else 0.0

def read_mem() -> dict:
    info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
    except Exception:
        return {"mem_total_mb": 0.0, "mem_used_mb": 0.0, "mem_percent": 0.0}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    return {
        "mem_total_mb": round(total / 1024, 1),
        "mem_used_mb": round(used / 1024, 1),
        "mem_percent": round(used / total * 100, 1) if total else 0.0,
    }

def read_disk() -> dict:
    try:
        s = os.statvfs(STORAGE_DIR)
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - free
        return {
            "disk_total_gb": round(total / 1e9, 2),
            "disk_used_gb": round(used / 1e9, 2),
            "disk_free_gb": round(free / 1e9, 2),
            "disk_percent": round(used / total * 100, 1) if total else 0.0,
        }
    except Exception:
        return {"disk_total_gb": 0.0, "disk_used_gb": 0.0, "disk_free_gb": 0.0, "disk_percent": 0.0}


def _smart_info(device: str = "") -> dict:
    """Sức khoẻ SMART qua smartctl nếu có (container thường không có sẵn)."""
    if shutil.which("smartctl") is None:
        return {"available": False, "note": "smartctl không khả dụng trong container"}
    dev = device or os.environ.get("NVR_DISK_DEVICE", "")
    if not dev:
        return {"available": False, "note": "chưa cấu hình NVR_DISK_DEVICE"}
    try:
        r = subprocess.run(["smartctl", "-H", "-A", dev], capture_output=True, text=True, timeout=8)
        txt = (r.stdout or "") + (r.stderr or "")
        health = "PASSED" if "PASSED" in txt else ("FAILED" if "FAILED" in txt else "unknown")
        temp = None
        for line in txt.splitlines():
            if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                nums = [x for x in re.findall(r"\d+", line.split("  ")[-1]) if x]
                if nums:
                    temp = int(nums[0])
                    break
        return {"available": True, "health": health, "temperature_c": temp}
    except Exception as e:
        return {"available": False, "note": mask_secrets(str(e))}


def disk_health() -> dict:
    """Sức khoẻ nơi lưu bản ghi: dung lượng, inode, quyền ghi, SMART (nếu có)."""
    out: Dict[str, Any] = {"path": STORAGE_DIR, "writable": False, "smart": {"available": False}}
    try:
        s = os.statvfs(STORAGE_DIR)
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - free
        inodes = s.f_files
        inodes_used = inodes - s.f_favail
        out.update({
            "total_gb": round(total / 1e9, 2),
            "used_gb": round(used / 1e9, 2),
            "free_gb": round(free / 1e9, 2),
            "percent": round(used / total * 100, 1) if total else 0.0,
            "inodes_total": inodes,
            "inodes_used": inodes_used,
            "inodes_percent": round(inodes_used / inodes * 100, 1) if inodes else 0.0,
            "writable": os.access(STORAGE_DIR, os.W_OK),
        })
    except Exception as e:
        out["error"] = mask_secrets(str(e))
    out["smart"] = _smart_info()
    return out

_ff_cpu_prev: Dict[str, tuple] = {}
def read_cam_cpu() -> Dict[str, float]:
    """%CPU của tiến trình ffmpeg từng camera (đọc /proc/<pid>/stat)."""
    out: Dict[str, float] = {}
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except Exception:
        ticks = 100
    now = time.time()
    for cam_id, proc in list(ffmpeg_processes.items()):
        try:
            with open(f"/proc/{proc.pid}/stat", "r") as f:
                parts = f.read().split()
            cpu_ticks = int(parts[13]) + int(parts[14])
        except Exception:
            continue
        prev = _ff_cpu_prev.get(cam_id)
        _ff_cpu_prev[cam_id] = (now, cpu_ticks)
        if prev:
            dt = now - prev[0]
            dcpu = (cpu_ticks - prev[1]) / ticks
            out[cam_id] = round(100.0 * dcpu / dt, 1) if dt > 0 else 0.0
    return out

def collect_stats() -> dict:
    s = {"t": int(time.time()), "cpu_percent": read_cpu_percent(), "cam_cpu": read_cam_cpu()}
    s.update(read_mem())
    s.update(read_disk())
    s["cam_health"] = health_snapshot()
    return s

def stats_sampler():
    # Lấy mẫu đầu để có baseline CPU (lần đầu luôn trả 0)
    with _stats_lock:
        latest_stats.update(collect_stats())
    while True:
        time.sleep(1)
        sample = collect_stats()
        with _stats_lock:
            latest_stats.clear()
            latest_stats.update(sample)
            stats_history.append(sample)

@asynccontextmanager
async def lifespan(app: FastAPI):
    _shutdown.clear()
    start_go2rtc()
    for cam_id, cam in list(CONFIG.get("cameras", {}).items()):
        if schedule_record_mode(cam) != "off":
            start_camera_worker(cam_id)
    threading.Thread(target=motion_buffer_manager, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    threading.Thread(target=ram_janitor, daemon=True).start()
    threading.Thread(target=drive_worker, daemon=True).start()
    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=hass_motion_listener, daemon=True).start()
    threading.Thread(target=mqtt_worker, daemon=True).start()
    threading.Thread(target=stats_sampler, daemon=True).start()
    threading.Thread(target=live_supervisor, daemon=True).start()
    threading.Thread(target=stream_health_supervisor, daemon=True).start()
    threading.Thread(target=camera_offline_supervisor, daemon=True).start()
    threading.Thread(target=disk_alert_supervisor, daemon=True).start()
    threading.Thread(target=go2rtc_monitor, daemon=True).start()
    threading.Thread(target=_license_worker, daemon=True).start()
    try:
        yield
    finally:
        _shutdown.set()
        mqtt_shutdown()
        with worker_lock:
            camera_ids = list(set(worker_tokens) | set(ffmpeg_processes))
        for cam_id in camera_ids:
            await asyncio.to_thread(stop_camera_worker, cam_id)
        with _snapshot_lock:
            snapshots = list(_snapshot_processes.values())
        for proc in snapshots:
            await asyncio.to_thread(_terminate_process, proc)
        stop_go2rtc()

app = FastAPI(title="LiteNVR Pro for ZimaOS", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)

@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
    return resp

@app.middleware("http")
async def hls_no_cache(request: Request, call_next):
    path = request.url.path
    cam_id = None
    if path.startswith("/hls/"):
        parts = path.split("/")
        if path.startswith("/hls/_play/"):
            cam_id = parts[3] if len(parts) > 3 else None
        elif len(parts) > 2:
            cam_id = parts[2]
            ensure_live(cam_id)
            if path.endswith("/live.m3u8") and cam_id in CONFIG.get("cameras", {}):
                playlist = os.path.join(HLS_DIR, cam_id, "live.m3u8")
                deadline = time.monotonic() + 8
                while not os.path.isfile(playlist) and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
    resp = await call_next(request)
    if path.startswith("/hls/"):
        resp.headers["Cache-Control"] = "no-store"
        if resp.status_code == 404:
            # Segment 404 thường gặp khi ffmpeg đang khởi động lại; log thưa để
            # vẫn thấy lỗi thật mà không spam.
            now = time.time()
            key = f"_hls404:{cam_id}"
            st = cam_status.setdefault(cam_id or "?", {})
            if now - st.get(key, 0) > 60:
                st[key] = now
                log_event("warn", f"Không tìm thấy tệp HLS ({path.rsplit('/', 1)[-1]})", cam_id or "")
    return resp

PUBLIC_PATHS = ("/health", "/login", "/api/login", "/favicon.ico", "/manifest.webmanifest")

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Xác thực (phiên cookie hoặc Basic) + phân quyền tài khoản con + chống CSRF."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not AUTH_ENABLED:
        request.state.user = root_user()
        request.state.auth_source = "off"
        return await call_next(request)
    user, source = resolve_auth(request)
    if user is None:
        accept = request.headers.get("accept", "")
        if path == "/" and "text/html" in accept:
            return RedirectResponse("login", status_code=302)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Chưa đăng nhập"}, status_code=401)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LiteNVR"'}, content="Unauthorized")
    request.state.user = user
    request.state.auth_source = source
    request.state.session_token = request.cookies.get(SESSION_COOKIE, "") if source == "session" else ""
    if source == "session" and request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if request.headers.get("X-NVR-CSRF") != "1":
            origin = request.headers.get("origin", "") or request.headers.get("referer", "")
            host = request.headers.get("host", "")
            if not _same_origin(origin, host):
                return JSONResponse({"detail": "Thiếu mã chống CSRF (X-NVR-CSRF)"}, status_code=403)
    if user.get("role") != "admin":
        need = path_permission(request)
        if need == "__admin__":
            return JSONResponse({"detail": "Chỉ quản trị viên được phép"}, status_code=403)
        if need == "view":
            if not any(user["perms"].get(p) for p in ("live", "playback", "events")):
                return JSONResponse({"detail": "Tài khoản không được cấp quyền này"}, status_code=403)
        elif need == "events":
            if not (user["perms"].get("events") or user["perms"].get("playback")):
                return JSONResponse({"detail": "Tài khoản không được cấp quyền này"}, status_code=403)
        elif need and not user["perms"].get(need):
            return JSONResponse({"detail": "Tài khoản không được cấp quyền này"}, status_code=403)
        cam = cam_path_segment(path)
        if cam and path.startswith("/snapshots/"):
            cam = _snapshot_owner(cam) or cam
        if cam and user.get("cameras") and cam not in user["cameras"]:
            return JSONResponse({"detail": "Không có quyền với camera này"}, status_code=403)
    return await call_next(request)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    allow_iframe = os.environ.get("NVR_IFRAME", "off").lower() in ("on", "1", "true", "yes")
    if not allow_iframe:
        resp.headers["X-Frame-Options"] = "DENY"
    if not request.url.path.startswith(("/hls/", "/recordings/", "/snapshots/")):
        frame = "frame-ancestors *;" if allow_iframe else "frame-ancestors 'none';"
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "worker-src 'self' blob:; connect-src 'self'; " + frame + " "
            "base-uri 'self'; form-action 'self'",
        )
    return resp

AI_PROVIDERS = {
    "gemini": ("Google Gemini", "gemini-3.8-flash",
               "https://generativelanguage.googleapis.com/v1beta"),
    "openai": ("OpenAI / ChatGPT", "gpt-6-luna", "https://api.openai.com/v1"),
    "groq": ("Groq", "qwen/qwen3.8-27b", "https://api.groq.com/openai/v1"),
    "custom": ("OpenAI-compatible (custom)", "", ""),
}

AI_RETIRED_MODELS = {
    "gemini": {
        "gemini-2.0-flash": "gemini-3.8-flash",
        "gemini-2.0-flash-001": "gemini-3.8-flash",
        "gemini-2.0-flash-lite": "gemini-3.8-flash",
        "gemini-2.0-flash-lite-001": "gemini-3.8-flash",
        "gemini-2.0-flash-exp": "gemini-3.8-flash",
    },
    "groq": {"meta-llama/llama-4-scout-17b-16e-instruct": "qwen/qwen3.8-27b"},
}

DEFAULT_CONFIG = {
    "retention_days": 7,
    "max_storage_gb": 0,       # 0 = Không giới hạn theo GB
    "max_disk_percent": 85,    # Tự xóa file cũ nhất nếu ổ cứng đầy quá 85%
    "min_free_percent": 3,     # Tạm dừng ghi hình mới khi ổ trống thấp hơn mức này (%)
    "post_motion_sec": 60,     # Thời gian ghi tiếp sau khi có chuyển động (giây)
    "ram_buffer": True,        # True: đệm motion trong RAM; False: ghi đệm ra đĩa
    "cameras": {},
    "homeassistant": {
        "enabled": False,
        "base_url": "",       # VD: http://homeassistant.local:8123
        "token": "",          # Long-Lived Access Token của Home Assistant
        "entities": {}        # cam_id -> [entity_id cảm biến chuyển động, ...]
    },
    "mqtt": {
        "enabled": False,
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "tls": False,
        "discovery_prefix": "homeassistant",
        "base_topic": "litenvr",
        "snapshot_interval": 15
    },
    "notify": {
        "telegram_enabled": False,
        "telegram_token": "",
        "telegram_chat_id": "",
        "webhook_url": "",
        "public_url": "",             # địa chỉ LiteNVR để tạo link xem lại trong tin nhắn
        "with_photo": True,
        "telegram_clip": False,
        "telegram_clip_sec": 8,
        "disk_alert_enabled": False,
        "disk_warn_percent": 85,
        "disk_crit_percent": 95,
        "camera_offline_alert": True
    },
    "license": {
        "enforce": True,
        "server_url": (os.environ.get("LITENVR_LICENSE_URL")
                       or "https://litenvr-license.vercel.app").rstrip("/"),
        "key": "",
        "token": "",
        "plan": "",
        "expires_at": "",
        "installation_id": "",
        "last_check": 0,
        "last_error": "",
        "grace_days": 7,
        "checked_version": ""
    },
    "ai": {
        "enabled": False,
        "provider": "gemini",
        "api_key": "",
        "model": "gemini-3.8-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "prompt": (
            "Mô tả ngắn gọn bằng tiếng Việt nội dung ảnh camera an ninh: người, "
            "xe cộ, động vật, đồ vật, màu quần áo, hành động và hướng di chuyển "
            "nếu thấy. Tối đa 60 từ, chỉ mô tả điều nhìn thấy, không suy đoán danh tính."
        ),
        "timeout": 30
    },
    "gdrive": {
        "enabled": False,
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "folder_name": "LiteNVR",     # Thư mục gốc trên Drive (tự tạo)
        "root_folder_id": "",
        "cameras": [],                # [] = tất cả camera; hoặc danh sách cam_id
        "bandwidth_kbps": 0,          # 0 = không giới hạn tốc độ tải lên
        "parallel_uploads": 1,        # Số file tải lên Google Drive cùng lúc
        "max_gb": 0,                  # 0 = chỉ dọn khi Drive thật sự gần đầy
        "min_free_mb": 2000,          # Dọn file cũ khi Drive còn trống dưới mức này
        "since_ts": 0                 # Chỉ sao lưu bản ghi mới hơn mốc này (0 = mọi bản ghi)
    },
    "backup": {
        "enabled": False,
        "kind": "folder",             # folder = thư mục đã mount; ftp; smb = SMB/Samba trực tiếp
        "folder_path": "",            # đường dẫn thư mục đích (mount SMB/NFS trong container)
        "ftp_host": "", "ftp_port": 21, "ftp_user": "", "ftp_pass": "", "ftp_dir": "/",
        "ftp_tls": False,             # True = FTPS (FTP qua TLS)
        "smb_host": "", "smb_port": 445, "smb_share": "", "smb_user": "", "smb_pass": "",
        "smb_domain": "", "smb_dir": "",
        "smb_cleanup_enabled": False,  # Tự dọn bản ghi LiteNVR cũ trên SMB
        "smb_retention_days": 0,       # 0 = không xoá theo số ngày
        "smb_max_gb": 0,               # 0 = không giới hạn dung lượng bản ghi LiteNVR
        "smb_min_free_gb": 2,          # Dọn khi share còn trống dưới mức này (0 = tắt)
        "mode": "copy",               # copy = giữ ở HDD; move = xoá sau khi sao lưu
        "cameras": [],                # danh sách cam_id được sao lưu ngoài (rỗng = không camera nào)
        "cameras_explicit": False,    # False = cấu hình cũ chưa phân biệt "rỗng = tất cả"
        "since_ts": 0                 # chỉ sao lưu bản ghi mới hơn mốc này
    }
}

def _normalize_config(cfg: dict) -> dict:
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    ha = cfg.setdefault("homeassistant", {})
    for k, v in DEFAULT_CONFIG["homeassistant"].items():
        ha.setdefault(k, v)
    mq = cfg.setdefault("mqtt", {})
    for k, v in DEFAULT_CONFIG["mqtt"].items():
        mq.setdefault(k, v)
    try:
        mq["port"] = max(1, min(65535, int(mq.get("port") or 1883)))
    except (TypeError, ValueError):
        mq["port"] = 1883
    try:
        mq["snapshot_interval"] = max(5, min(300, int(mq.get("snapshot_interval") or 15)))
    except (TypeError, ValueError):
        mq["snapshot_interval"] = 15
    mq["enabled"] = bool(mq.get("enabled"))
    mq["tls"] = bool(mq.get("tls"))
    nt = cfg.setdefault("notify", {})
    for k, v in DEFAULT_CONFIG["notify"].items():
        nt.setdefault(k, v)
    ai_cfg = cfg.setdefault("ai", {})
    for k, v in DEFAULT_CONFIG["ai"].items():
        ai_cfg.setdefault(k, v)
    if str(ai_cfg.get("provider") or "").lower() not in AI_PROVIDERS:
        ai_cfg["provider"] = "gemini"
    try:
        ai_cfg["timeout"] = max(5, min(120, int(ai_cfg.get("timeout") or 30)))
    except (TypeError, ValueError):
        ai_cfg["timeout"] = 30
    ai_cfg["enabled"] = bool(ai_cfg.get("enabled"))
    lic = cfg.setdefault("license", {})
    for k, v in DEFAULT_CONFIG["license"].items():
        lic.setdefault(k, v)
    if not str(lic.get("installation_id") or "").strip():
        lic["installation_id"] = secrets.token_hex(16)
    lic["enforce"] = bool(lic.get("enforce", True))
    try:
        lic["grace_days"] = max(1, min(30, int(lic.get("grace_days") or 7)))
    except (TypeError, ValueError):
        lic["grace_days"] = 7
    cfg.pop("motion_detect", None)
    # Di trú: cảm biến chuyển động HA từ cấu hình chung sang từng camera.
    legacy_entities = ha.get("entities") or {}
    for cam_id, ent_list in legacy_entities.items():
        cam = cfg.get("cameras", {}).get(cam_id)
        if not isinstance(cam, dict):
            continue
        merged: Dict[str, dict] = {}
        for ent in (cam.get("hass_entities") or []):
            e = _norm_hass_entity(ent)
            if e:
                merged[e["entity_id"]] = e
        for ent in (ent_list or []):
            e = _norm_hass_entity(ent)
            if e and e["entity_id"] not in merged:
                merged[e["entity_id"]] = e
        cam["hass_entities"] = [merged[k] for k in sorted(merged)]
    gd = cfg.setdefault("gdrive", {})
    for k, v in DEFAULT_CONFIG["gdrive"].items():
        gd.setdefault(k, v)
    if not isinstance(gd.get("cameras"), list):
        gd["cameras"] = []
    bk = cfg.setdefault("backup", {})
    had_explicit_cams = bool(bk.get("cameras_explicit"))
    for k, v in DEFAULT_CONFIG["backup"].items():
        bk.setdefault(k, v)
    if not isinstance(bk.get("cameras"), list):
        bk["cameras"] = []
    known_cams = sorted(str(c) for c in (cfg.get("cameras") or {}))
    if not had_explicit_cams:
        selected = [str(c) for c in bk["cameras"] if str(c) in known_cams]
        kind = str(bk.get("kind") or "")
        backup_active = bool(bk.get("enabled")) and (
            (kind == "folder" and (bk.get("folder_path") or "").strip())
            or (kind == "ftp" and (bk.get("ftp_host") or "").strip())
            or (kind == "smb" and (bk.get("smb_host") or "").strip()
                and (bk.get("smb_share") or "").strip())
        )
        if not selected and backup_active:
            selected = known_cams
        bk["cameras"] = selected
        bk["cameras_explicit"] = True
    else:
        bk["cameras"] = [str(c) for c in bk["cameras"] if str(c) in known_cams]
    # Di trú: camera cũ lưu "copy" nay chuyển sang "auto" để tự dò H.265
    for cam in cfg.get("cameras", {}).values():
        if cam.get("video_codec") in (None, "", "copy"):
            cam["video_codec"] = "auto"
    return cfg

def _norm_hass_entity(ent: Any) -> Optional[dict]:
    """Chuẩn hoá một cảm biến HA: hỗ trợ dạng chuỗi cũ và dạng {entity_id, state, label}."""
    if isinstance(ent, dict):
        eid = str(ent.get("entity_id") or "").strip()
        active = str(ent.get("state") or ent.get("active_state") or "").strip()
        label = str(ent.get("label") or "").strip()
    else:
        eid = str(ent or "").strip()
        active = ""
        label = ""
    if not eid:
        return None
    return {"entity_id": eid, "state": active, "label": label}

def _default_active_state(entity_id: str) -> str:
    """Trạng thái coi là 'có chuyển động' theo loại entity, dùng khi chưa cấu hình."""
    domain = (entity_id or "").split(".", 1)[0].lower()
    if domain in ("device_tracker", "person"):
        return "home"
    if domain in ("binary_sensor", "switch", "input_boolean", "light"):
        return "on"
    return ""

def _hass_motion_triggered(rule: dict, entity_id: str, new_state: Any, prev_state: Any) -> bool:
    """True khi trạng thái CHUYỂN sang giá trị kích hoạt (không tính khi đang đã ở mức đó),
    tránh cảm biến báo liên tục làm ghi hình không ngừng."""
    state = str(new_state).lower()
    prev = str(prev_state).lower()
    active = (rule.get("state") or _default_active_state(entity_id)).lower()
    if active:
        return state == active and prev != active
    return state != prev

def _hass_state_options(entity_id: str) -> list:
    """Các trạng thái thường dùng của một entity để UI cho chọn điều kiện kích hoạt."""
    domain = (entity_id or "").split(".", 1)[0].lower()
    if domain in ("device_tracker", "person"):
        return ["home", "not_home"]
    if domain in ("binary_sensor", "switch", "input_boolean", "light"):
        return ["on", "off"]
    return []

def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = _normalize_config(json.loads(json.dumps(DEFAULT_CONFIG)))
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        had_installation_id = bool((raw.get("license") or {}).get("installation_id"))
        cfg = _normalize_config(raw)
        if not had_installation_id:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        return cfg
    except Exception:
        return _normalize_config(json.loads(json.dumps(DEFAULT_CONFIG)))

_config_lock = threading.Lock()

def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with _config_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)

CONFIG = load_config()

_db_local = threading.local()

def db_conn() -> sqlite3.Connection:
    """Kết nối SQLite tái dùng theo luồng, bật WAL để đọc/ghi song song ổn định
    khi nhiều camera cùng ghi sự kiện (tránh 'database is locked')."""
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA cache_size=-512")
        _db_local.conn = conn
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cam_id TEXT,
            timestamp INTEGER,
            date_str TEXT,
            time_str TEXT,
            label TEXT,
            snapshot TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cam_date ON events(cam_id, date_str)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cam_ts ON events(cam_id, timestamp)")
    cols = {row[1] for row in c.execute("PRAGMA table_info(events)").fetchall()}
    if "starred" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
    if "ai_description" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN ai_description TEXT")
    if "ai_model" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN ai_model TEXT")
    if "ai_ts" not in cols:
        c.execute("ALTER TABLE events ADD COLUMN ai_ts INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_starred ON events(starred, timestamp)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS locks (
            path TEXT PRIMARY KEY,
            cam_id TEXT,
            created INTEGER,
            note TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS drive_uploads (
            path TEXT PRIMARY KEY,
            cam_id TEXT,
            name TEXT,
            ts REAL,
            size INTEGER,
            status TEXT,
            error TEXT,
            drive_id TEXT,
            tries INTEGER DEFAULT 0,
            next_try REAL DEFAULT 0,
            updated REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_drive_status ON drive_uploads(status, next_try)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_drive_ts ON drive_uploads(ts)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS backup_done (
            path TEXT PRIMARY KEY,
            cam_id TEXT,
            size INTEGER,
            ts REAL,
            dest TEXT NOT NULL DEFAULT '',
            deleted INTEGER NOT NULL DEFAULT 0,
            deleted_ts REAL
        )
    """)
    backup_cols = {row[1] for row in c.execute("PRAGMA table_info(backup_done)").fetchall()}
    if "dest" not in backup_cols:
        c.execute("ALTER TABLE backup_done ADD COLUMN dest TEXT NOT NULL DEFAULT ''")
    if "deleted" not in backup_cols:
        c.execute("ALTER TABLE backup_done ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    if "deleted_ts" not in backup_cols:
        c.execute("ALTER TABLE backup_done ADD COLUMN deleted_ts REAL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_backup_dest ON backup_done(dest, deleted, ts)")
    conn.commit()

init_db()

def locked_paths() -> set:
    """Tập hợp các bản ghi được khoá (cam_id/fname) — không bị tự động xoá."""
    try:
        c = db_conn().cursor()
        c.execute("SELECT path FROM locks")
        return {row[0] for row in c.fetchall()}
    except Exception:
        return set()

def lock_recording(cam_id: str, fname: str, note: str = "") -> bool:
    fname = os.path.basename(fname)
    fp = os.path.join(RECORDINGS_DIR, os.path.basename(cam_id), fname)
    if not os.path.isfile(fp):
        return False
    conn = db_conn()
    conn.execute("INSERT OR REPLACE INTO locks (path, cam_id, created, note) VALUES (?, ?, ?, ?)",
                 (f"{cam_id}/{fname}", cam_id, int(time.time()), note))
    conn.commit()
    return True

def unlock_recording(cam_id: str, fname: str) -> bool:
    conn = db_conn()
    cur = conn.execute("DELETE FROM locks WHERE path=?", (f"{cam_id}/{os.path.basename(fname)}",))
    conn.commit()
    return cur.rowcount > 0

# ================= Bảo mật: tài khoản con, phiên đăng nhập, phân quyền =================
# Mô hình: tài khoản gốc (NVR_USER/NVR_PASS trong docker-compose) luôn là admin toàn
# quyền và không lưu trong DB. Tài khoản con lưu trong bảng users với vai trò
# admin/user + bộ quyền chi tiết (live, playback, export, ptz, lock, events) và danh
# sách camera được phép (rỗng = tất cả).

SESSION_COOKIE = "litenvr_session"
SESSION_TTL = 7 * 86400          # 7 ngày, tự gia hạn khi dùng
SESSION_MAX_TTL = 30 * 86400     # tối đa 30 ngày kể từ lúc đăng nhập
PBKDF2_ITERS = 200_000
PERM_KEYS = ("live", "playback", "export", "ptz", "lock", "events")
DEFAULT_PERMS = {"live": True, "playback": True, "export": False, "ptz": False, "lock": False, "events": True}
ALL_PERMS = {k: True for k in PERM_KEYS}

# Chống dò mật khẩu: 8 lần sai trong 15 phút -> khoá IP 5 phút
LOGIN_FAIL_WINDOW = 900
LOGIN_FAIL_MAX = 8
LOGIN_LOCK_SEC = 300
_login_fails: Dict[str, list] = {}
_login_fails_lock = threading.Lock()

# Sự kiện bảo mật gần nhất giữ trong RAM để hiển thị nhanh (bảng auth_events là bản ghi chính)
security_events: deque = deque(maxlen=200)


def init_security():
    conn = db_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            pass_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            perms TEXT NOT NULL DEFAULT '{}',
            cameras TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            totp_secret TEXT NOT NULL DEFAULT '',
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            created INTEGER,
            last_login INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created INTEGER,
            expires INTEGER,
            ip TEXT DEFAULT ''
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            username TEXT,
            ip TEXT,
            action TEXT,
            detail TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_auth_events_ts ON auth_events(ts)")
    conn.commit()


def hash_password(password: str, salt: str = "") -> tuple:
    """PBKDF2-HMAC-SHA256; trả (salt, hash hex)."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS)
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    try:
        _, got = hash_password(password, salt)
        return secrets.compare_digest(got, expected_hash)
    except Exception:
        return False


def mask_secrets(text: str) -> str:
    """Che thông tin đăng nhập nhúng trong URL RTSP/HTTP và API key."""
    text = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^@/\s]+)@", r"\1***:***@", text or "")
    text = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s]+/user:)[^;/\s]+(;pwd:)[^/\s?#]+",
        r"\1***\2***",
        text,
    )
    text = re.sub(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1***", text, flags=re.I)
    return re.sub(r"\b(sk-[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,}|AIza[0-9A-Za-z_-]{20,})\b",
                  "***", text)


def get_user(username: str) -> Optional[sqlite3.Row]:
    try:
        c = db_conn().cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        row = c.fetchone()
        if row is not None:
            return row
    except Exception:
        pass
    return None


def _row_to_user(row: sqlite3.Row) -> dict:
    try:
        perms = json.loads(row["perms"] or "{}")
    except Exception:
        perms = {}
    try:
        cameras = json.loads(row["cameras"] or "[]")
    except Exception:
        cameras = []
    full_perms = dict(DEFAULT_PERMS)
    if row["role"] == "admin":
        full_perms = dict(ALL_PERMS)
    else:
        for k in PERM_KEYS:
            if k in perms:
                full_perms[k] = bool(perms[k])
    return {
        "username": row["username"],
        "role": row["role"],
        "perms": full_perms,
        "cameras": [str(x) for x in cameras if str(x)],
        "is_root": False,
        "enabled": bool(row["enabled"]),
        "totp_enabled": bool(row["totp_enabled"]),
        "created": row["created"] or 0,
        "last_login": row["last_login"] or 0,
    }


def root_user() -> dict:
    return {
        "username": NVR_USER,
        "role": "admin",
        "perms": dict(ALL_PERMS),
        "cameras": [],
        "is_root": True,
        "enabled": True,
        "totp_enabled": False,
        "created": 0,
        "last_login": 0,
    }


def audit(action: str, username: str = "", ip: str = "", detail: str = ""):
    entry = {"t": int(time.time()), "username": username or "-", "ip": ip or "-", "action": action, "detail": detail}
    security_events.append(entry)
    try:
        conn = db_conn()
        conn.execute("INSERT INTO auth_events (ts, username, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
                     (entry["t"], entry["username"], entry["ip"], action, detail))
        conn.commit()
    except Exception:
        pass
    if action in ("login_fail", "login_locked", "user_create", "user_delete", "totp_disable", "user_update"):
        log_event("warn", f"Bảo mật: {action} ({username or '-'} từ {ip or '-'})")


def purge_sessions():
    try:
        conn = db_conn()
        conn.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
        conn.commit()
    except Exception:
        pass


def create_session(username: str, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn = db_conn()
    conn.execute("INSERT INTO sessions (token_hash, username, created, expires, ip) VALUES (?, ?, ?, ?, ?)",
                 (hashlib.sha256(token.encode()).hexdigest(), username, now, now + SESSION_TTL, ip))
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.commit()
    return token


def destroy_session(token: str):
    if not token:
        return
    try:
        conn = db_conn()
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        conn.commit()
    except Exception:
        pass


def session_user(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        c = db_conn().cursor()
        th = hashlib.sha256(token.encode()).hexdigest()
        c.execute("SELECT username, expires, created FROM sessions WHERE token_hash=?", (th,))
        row = c.fetchone()
        if not row:
            return None
        now = int(time.time())
        if row[1] < now:
            destroy_session(token)
            return None
        if secrets.compare_digest(row[0], NVR_USER):
            user = root_user()  # tài khoản gốc không lưu DB nhưng vẫn có thể tạo phiên
        else:
            urow = get_user(row[0])
            if urow is None or not urow["enabled"]:
                destroy_session(token)
                return None
            user = _row_to_user(urow)
        if row[1] - now < SESSION_TTL - 3600:  # tự gia hạn, tối đa 1 lần/giờ
            try:
                conn = db_conn()
                new_exp = min(now + SESSION_TTL, int(row[2] or 0) + SESSION_MAX_TTL)
                if new_exp > row[1]:
                    conn.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (new_exp, th))
                    conn.commit()
            except Exception:
                pass
        return user
    except Exception:
        return None


def login_locked(ip: str) -> int:
    """Trả số giây còn bị khoá (0 = không khoá)."""
    now = time.time()
    with _login_fails_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
        _login_fails[ip] = fails
        if len(fails) >= LOGIN_FAIL_MAX:
            remaining = int(fails[-1] + LOGIN_LOCK_SEC - now)
            if remaining > 0:
                return remaining
            _login_fails[ip] = []
    return 0


def note_login_fail(ip: str):
    with _login_fails_lock:
        _login_fails.setdefault(ip, []).append(time.time())


def clear_login_fails(ip: str):
    with _login_fails_lock:
        _login_fails.pop(ip, None)


# ---- TOTP (Google Authenticator / Authy...) thuần Python, không cần thư viện ngoài ----
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, at: float = 0) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_verify(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6 or not secret:
        return False
    now = time.time()
    return any(secrets.compare_digest(totp_code(secret, now + drift), code) for drift in (-30, 0, 30))


def totp_uri(username: str, secret: str) -> str:
    label = urllib.parse.quote(f"LiteNVR:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer=LiteNVR&digits=6&period=30"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""

def _same_origin(origin: str, host: str) -> bool:
    """So khớp origin/referer với Host theo scheme + hostname + port (không substring)."""
    try:
        u = urllib.parse.urlsplit(origin or "")
        if not u.scheme or not u.hostname or not host:
            return False
        h = urllib.parse.urlsplit("//" + host)
        if not h.hostname:
            return False
        default = 443 if u.scheme == "https" else 80
        return u.hostname == h.hostname and (u.port or default) == (h.port or default)
    except Exception:
        return False


def resolve_auth(request: Request) -> tuple:
    """Trả (user_dict | None, nguồn xác thực)."""
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        user = session_user(token)
        if user:
            return user, "session"
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        ip = client_ip(request)
        if login_locked(ip):
            return None, ""
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")
            if secrets.compare_digest(user, NVR_USER) and secrets.compare_digest(pw, NVR_PASS):
                clear_login_fails(ip)
                return root_user(), "basic"
            row = get_user(user)
            if row is not None and row["enabled"] and verify_password(pw, row["salt"], row["pass_hash"]):
                if row["totp_enabled"]:
                    audit("login_fail", user, ip, "Basic Auth bị chặn vì tài khoản bật 2FA")
                    return None, ""
                clear_login_fails(ip)
                return _row_to_user(row), "basic"
            note_login_fail(ip)
            audit("login_fail", user, ip, "Basic Auth sai mật khẩu")
        except Exception:
            pass
    return None, ""


def cam_path_segment(path: str) -> Optional[str]:
    if path.startswith("/hls/_play/"):
        rest = path[len("/hls/_play/"):]
        return rest.split("/", 1)[0] or None
    for prefix in ("/hls/", "/recordings/", "/snapshots/", "/api/live/", "/api/status/",
                   "/api/timeline/", "/api/play/", "/api/drive/play/", "/api/smb/play/",
                   "/api/export/", "/api/thumb/", "/api/ptz/", "/api/locks/"):
        if path.startswith(prefix):
            rest = path[len(prefix):]
            seg = rest.split("/", 1)[0]
            return seg or None
    return None


def path_permission(request: Request) -> Optional[str]:
    """Quyền cần có cho request ('__admin__' = chỉ admin, None = chỉ cần đăng nhập)."""
    path, method = request.url.path, request.method
    if path in ("/api/me", "/api/logout", "/api/client_log", "/api/version") or path.startswith("/api/me/"):
        return None
    if method == "GET" and path == "/api/cameras":
        return "view"
    if method == "GET" and re.fullmatch(r"/api/cameras/[^/]+/snapshot\.jpg", path):
        return "view"
    if path in ("/api/motion", "/api/ai/detect"):
        return "live"
    if path.startswith("/api/drive/play/"):
        return "playback"  # xem lại bản ghi đã sao lưu lên Drive
    if path.startswith("/api/users") or path.startswith("/api/config") or path.startswith("/api/storage") \
            or path.startswith("/api/hass") or path.startswith("/api/notify") or path.startswith("/api/logs") \
            or path.startswith("/api/system") or path.startswith("/api/onvif") or path.startswith("/api/audit") \
            or path.startswith("/api/security") or path.startswith("/api/drive") \
            or path.startswith("/api/ai") or path.startswith("/api/license"):
        return "__admin__"
    if path.startswith("/api/cameras"):
        return "__admin__"  # POST/DELETE/bulk đổi cấu hình camera
    if path.startswith("/hls/_play/"):
        return "playback"
    if path.startswith(("/api/live/", "/hls/")):
        return "live"
    if path.startswith(("/api/stats", "/api/status/")):
        return "live"
    if path.startswith(("/api/events",)):
        return "events"
    if path.startswith(("/api/play/", "/api/smb/play/", "/api/timeline/", "/api/recording_days/",
                        "/api/thumb/", "/recordings/", "/api/latest_snapshots")):
        return "playback"
    if path.startswith("/snapshots/"):
        return "view"
    if path.startswith("/api/export/"):
        return "export"
    if path.startswith("/api/ptz/"):
        return "ptz"
    if path.startswith("/api/locks"):
        return "lock"
    if path.startswith("/api/"):
        return "__admin__"  # API lạ: mặc định chỉ admin
    return None  # trang tĩnh / tài nguyên khác: chỉ cần đăng nhập


def assert_cam_access(request: Request, cam_id: str):
    u = getattr(request.state, "user", None) or {}
    if u.get("role") == "admin" or not u.get("cameras"):
        return
    if cam_id not in u["cameras"]:
        raise HTTPException(status_code=403, detail="Không có quyền với camera này")


ADMIN_PERM_SET = {k: True for k in PERM_KEYS}


def _clean_perms(raw: Any) -> dict:
    out = dict(DEFAULT_PERMS)
    if isinstance(raw, dict):
        for k in PERM_KEYS:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def _valid_username(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", name or ""))


init_security()


ffmpeg_processes: Dict[str, subprocess.Popen] = {}
worker_tokens: Dict[str, threading.Event] = {}
worker_lock = threading.Lock()
_shutdown = threading.Event()
FFMPEG_THREADS = max(1, min(8, int(os.environ.get("FFMPEG_THREADS", "2") or "2")))
LIVE_TRANSCODE_THREADS = 1
_snapshot_lock = threading.Lock()
_ha_snapshot_lock = threading.Lock()
_snapshot_slots = threading.BoundedSemaphore(2)
_snapshot_active: Dict[str, str] = {}
_snapshot_processes: Dict[str, subprocess.Popen] = {}
_playback_lock = threading.Lock()
_playback_processes: Dict[int, tuple] = {}
# Camera đang có người xem live (timestamp hoạt động gần nhất)
cam_viewers: Dict[str, float] = {}
viewer_lock = threading.Lock()
LIVE_IDLE_STOP_SEC = int(os.environ.get("LIVE_IDLE_STOP_SEC", "30"))
# Chiều cao tối đa cho live HLS khi phải chuyển mã (0 = giữ nguyên gốc).
# Hạ độ phân giải live giúp CPU giảm mạnh với camera 2K/4K (bản ghi vẫn giữ nguyên gốc).
LIVE_MAX_HEIGHT = int(os.environ.get("LIVE_MAX_HEIGHT", "720") or "0")
# Live H.265/HEVC phải decode + encode; cap thấp để giảm RAM/CPU. Có thể nâng
# bằng biến môi trường LIVE_TRANSCODE_MAX_HEIGHT (0 = không cap).
LIVE_TRANSCODE_MAX_HEIGHT = int(os.environ.get("LIVE_TRANSCODE_MAX_HEIGHT", "576") or "0")
# HLS độ trễ thấp: segment 1 giây và playlist ngắn để trình duyệt bám sát live edge.
# Tăng LIVE_HLS_TIME/LIVE_HLS_LIST nếu mạng Wi-Fi yếu hoặc camera có GOP dài.
LIVE_HLS_TIME = max(1, min(10, int(os.environ.get("LIVE_HLS_TIME", "1") or "1")))
LIVE_HLS_LIST = max(3, min(20, int(os.environ.get("LIVE_HLS_LIST", "4") or "4")))
# Theo dõi cảnh báo đầy ổ (tránh spam log)
_disk_warn = {"t": 0.0}
_disk_paused = {"low": False}


def disk_low() -> bool:
    """True khi ổ chứa bản ghi hoặc tmpfs HLS/buffer gần đầy -> tạm dừng ghi hình."""
    try:
        pct = float(CONFIG.get("min_free_percent", 3) or 0)
        if pct <= 0:
            return False
        for path in (STORAGE_DIR, BUFFER_DIR, HLS_DIR):
            try:
                total, used, _ = shutil.disk_usage(path)
            except OSError:
                continue
            if total and ((total - used) / total) * 100 < pct:
                return True
        return False
    except Exception:
        return False
# Lưu thời điểm kết thúc ghi hình chuyển động của từng camera (Unix timestamp)
motion_active_until: Dict[str, float] = {}
# Ghi trước sự kiện chuyển động để clip không bị mất đầu (giây).
MOTION_PREROLL_SEC = 10
MOTION_BUFFER_SEGMENT_SEC = 15
MOTION_BUFFER_KEEP_SEC = max(30, MOTION_PREROLL_SEC + MOTION_BUFFER_SEGMENT_SEC)
_motion_buffer_lock = threading.Lock()
# Chẩn đoán trạng thái camera (cho UI/log)
cam_status: Dict[str, dict] = {}
# Sức khoẻ luồng theo camera (fps, lần nhận khung gần nhất, đứng/không)
cam_health: Dict[str, dict] = {}
# Trạng thái mất kết nối camera (chỉ báo khi camera cần chạy ghi hình)
_cam_offline: Dict[str, dict] = {}
_cam_offline_lock = threading.Lock()
# Cache codec đã dò được: cam_id -> (url_key, info, thời điểm dò)
# TTL ngắn để nếu NVR/go2rtc đổi codec trên cùng URL thì LiteNVR tự dò lại.
CODEC_CACHE_TTL_SEC = 300
codec_cache: Dict[str, tuple] = {}

# Nhật ký sự kiện toàn ứng dụng (cho tab Log trên UI)
APP_LOG: deque = deque(maxlen=300)
_log_lock = threading.Lock()

def log_event(level: str, msg: str, cam_id: str = ""):
    """Ghi 1 dòng nhật ký (info/warn/error) để UI hiển thị và biết đường sửa."""
    with _log_lock:
        APP_LOG.append({
            "t": int(time.time()),
            "level": level,
            "cam_id": cam_id,
            "msg": msg,
        })

H264_BROWSER_SAFE_PIXFMT = {"yuv420p", "yuvj420p"}

def detect_video_codec(cam_id: str, cam_info: dict):
    """Dò codec + profile + pix_fmt của luồng RTSP (chỉ cache khi thành công).
    Trả về dict {'codec','profile','pix_fmt'} hoặc {} nếu không dò được."""
    _, feed_url = cam_source_urls(cam_id, cam_info)
    key = (feed_url, cam_info.get("rtsp_transport", "tcp"), _go2rtc.get("running", False))
    cached = codec_cache.get(cam_id)
    if (cached and len(cached) >= 3 and cached[0] == key
            and time.time() - cached[2] < CODEC_CACHE_TTL_SEC):
        return cached[1]
    cmd = [
        "ffprobe", "-v", "error",
        *_input_args(feed_url, cam_info),
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,pix_fmt,width,height",
        "-of", "default=nw=1",
        feed_url,
    ]
    info = {}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout or ""
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().lower()
    except Exception:
        info = {}
    if info.get("codec_name"):
        codec_cache[cam_id] = (key, info, time.time())
        return info
    return {}

def _is_browser_safe_h264(codec: str, pix_fmt: str, profile: str = "") -> bool:
    """H.264 mà Chrome/Edge/Firefox giải mã được: profile thường + màu 8-bit 4:2:0.
    Các biến thể 4:2:2/4:4:4/10-bit (một số camera Hanet dùng) gây hình rác/đen."""
    if codec not in ("h264", "avc1", "avc"):
        return False
    if pix_fmt and pix_fmt not in H264_BROWSER_SAFE_PIXFMT:
        return False
    prof = (profile or "").lower()
    if "4:4:4" in prof or "4:2:2" in prof or "10" in prof:
        return False
    return True

def resolve_video_codec(cam_id: str, cam_info: dict):
    """Trả về 'copy' hoặc 'h264'. Chế độ 'auto' dò codec thật của camera."""
    requested = cam_info.get("video_codec", "auto")
    if requested in ("copy", "h264"):
        return requested
    with worker_lock:
        token = worker_tokens.get(cam_id)
    info = detect_video_codec(cam_id, cam_info)
    codec = info.get("codec_name", "")
    with worker_lock:
        if worker_tokens.get(cam_id) is token and (token is None or not token.is_set()):
            st = cam_status.setdefault(cam_id, {})
            st["detected_codec"] = codec
            st["detected_profile"] = info.get("profile", "")
            st["detected_pix_fmt"] = info.get("pix_fmt", "")
            st["detect_failed"] = not bool(codec)
    return "copy" if _is_browser_safe_h264(codec, info.get("pix_fmt", ""), info.get("profile", "")) else "h264"

# ---------------- ONVIF (tự dò camera & lấy URL RTSP) ----------------
ONVIF_DEVICE_PATH = "/onvif/device_service"
# Cổng HTTP/ONVIF thường gặp: Dahua/Hikvision (80), Tapo (2020), Hanet/Reolink (8000),
# một số hãng dùng 8080/8899/5000.
ONVIF_SCAN_PORTS = (80, 2020, 8000, 8080, 8899, 5000)
ONVIF_MAX_SCAN_HOSTS = 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _find_text(root, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return ""

def _find_all(root, name: str):
    return [el for el in root.iter() if _local(el.tag) == name]

def _onvif_security(username: str, password: str) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    return (
        '<s:Header><wsse:Security '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f'<wsse:UsernameToken><wsse:Username>{xml_escape(username)}</wsse:Username>'
        f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        f'<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
    )

def _onvif_post(url: str, action: str, body: str, username: str = "", password: str = "", timeout: float = 6.0):
    envelope = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:ptz="http://www.onvif.org/ver20/ptz/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'{_onvif_security(username, password)}'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    ).encode("utf-8")
    req = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": "application/soap+xml; charset=utf-8",
        "SOAPAction": action,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # Một số camera yêu cầu HTTP Digest/Basic cho ONVIF (ngoài WS-Security).
        www = (e.headers.get("WWW-Authenticate") or "").lower()
        if e.code in (401, 403) and username and "digest" in www:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, url, username, password)
            opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
        elif e.code in (401, 403) and username and ("basic" in www or not www):
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            req.add_unredirected_header("Authorization", "Basic " + token)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        else:
            raise
    return ET.fromstring(raw)


def _onvif_device_url(host_or_url: str, port: int = 80) -> str:
    """Chuẩn hoá host/IP/XAddr thành URL device_service đầy đủ."""
    raw = (host_or_url or "").strip()
    if not raw:
        raise ValueError("Thiếu địa chỉ camera ONVIF")
    if "://" not in raw:
        raw = "http://" + raw
    p = urllib.parse.urlsplit(raw)
    if not p.hostname:
        raise ValueError("Địa chỉ camera ONVIF không hợp lệ")
    scheme = p.scheme or "http"
    pport = p.port or int(port or 80)
    hostpart = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    path = p.path or ONVIF_DEVICE_PATH
    if path == "/":
        path = ONVIF_DEVICE_PATH
    return urllib.parse.urlunsplit((scheme, f"{hostpart}:{pport}", path, p.query, ""))


def _onvif_port_from_url(url: str, default: int = 80) -> int:
    try:
        return int(urllib.parse.urlsplit(url).port or default)
    except Exception:
        return default


def _onvif_base_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _with_rtsp_auth(url: str, user: str, password: str) -> str:
    if not user or not url or "@" in url.split("://", 1)[-1]:
        return url
    scheme, rest = url.split("://", 1)
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(password or "", safe="")
    return f"{scheme}://{u}:{p}@{rest}"


def _parse_discovery_response(data: bytes, source_ip: str) -> Optional[dict]:
    """Đọc một ProbeMatch WS-Discovery thành thông tin thiết bị."""
    try:
        root = ET.fromstring(data)
    except Exception:
        return None
    addrs = _find_text(root, "XAddrs").split()
    xaddr = addrs[0] if addrs else ""
    scopes = _find_text(root, "Scopes")
    name = ""
    for part in scopes.split():
        if "/name/" in part:
            name = urllib.parse.unquote(part.split("/name/", 1)[1])
    if not name:
        name = _find_text(root, "Address")
    host = source_ip
    if xaddr:
        try:
            p = urllib.parse.urlsplit(xaddr if "://" in xaddr else "http://" + xaddr)
            host = p.hostname or source_ip
        except Exception:
            pass
    if not xaddr:
        xaddr = f"http://{host}:80{ONVIF_DEVICE_PATH}"
    return {
        "ip": host,
        "host": host,
        "port": _onvif_port_from_url(xaddr),
        "xaddr": xaddr,
        "name": name or host,
        "scopes": scopes,
    }


def onvif_discover(timeout: float = 3.0) -> List[dict]:
    """WS-Discovery multicast. Gửi lặp 3 lần để giảm mất gói UDP."""
    mid = f"uuid:{uuid.uuid4()}"
    msg = (
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>{mid}</w:MessageID>'
        '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    ).encode()
    found: Dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        try:
            s.bind(("", 0))
        except OSError:
            pass
        end = time.time() + max(0.5, min(timeout, 8))
        next_send = 0.0
        while time.time() < end:
            now = time.time()
            if now >= next_send:
                try:
                    s.sendto(msg, ("239.255.255.250", 3702))
                except OSError:
                    pass
                next_send = now + 1.0
            s.settimeout(max(0.1, min(0.4, end - now)))
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            dev = _parse_discovery_response(data, addr[0])
            if not dev:
                continue
            dev["source"] = "multicast"
            key = (dev.get("xaddr") or dev["ip"]).lower()
            found[key] = dev
    finally:
        s.close()
    return sorted(found.values(), key=lambda x: (x["ip"], x["port"]))


def _onvif_private_target(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _parse_scan_targets(spec: str) -> tuple:
    """Nhận CIDR / dải IP nội bộ, trả (danh sách host, nhãn dải)."""
    text = (spec or "").strip()
    if not text:
        raise ValueError("Chưa nhập dải IP cần quét")
    if "-" in text and "/" not in text:
        left, right = [x.strip() for x in text.split("-", 1)]
        try:
            a = ipaddress.ip_address(left)
            b = ipaddress.ip_address(right)
        except ValueError:
            raise ValueError("Dải IP không hợp lệ")
        if a.version != 4 or b.version != 4 or int(b) < int(a):
            raise ValueError("Dải IP không hợp lệ")
        total = int(b) - int(a) + 1
        if total > ONVIF_MAX_SCAN_HOSTS:
            raise ValueError(f"Dải IP quá lớn (tối đa {ONVIF_MAX_SCAN_HOSTS} địa chỉ)")
        hosts = [str(ipaddress.ip_address(int(a) + i)) for i in range(total)]
        label = f"{a}-{b}"
    else:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError("Dải IP không hợp lệ (VD: 192.168.1.0/24)")
        if net.version != 4:
            raise ValueError("Chỉ hỗ trợ IPv4")
        if net.num_addresses > ONVIF_MAX_SCAN_HOSTS + 2:
            raise ValueError(f"Dải IP quá lớn (tối đa {ONVIF_MAX_SCAN_HOSTS} địa chỉ)")
        hosts = [str(ip) for ip in net.hosts()] or [str(net.network_address)]
        label = str(net)
    if not hosts or any(not _onvif_private_target(h) for h in hosts):
        raise ValueError("Chỉ quét địa chỉ mạng nội bộ (192.168.x.x, 10.x.x.x, 172.16-31.x.x)")
    return hosts, label


def _onvif_scan_target(host: str, port: int, timeout: float) -> Optional[dict]:
    """Thử một IP:cổng bằng SOAP GetSystemDateAndTime (không cần tài khoản)."""
    try:
        with socket.create_connection((host, port), timeout=min(0.5, timeout)):
            pass
    except OSError:
        return None
    url = f"http://{host}:{port}{ONVIF_DEVICE_PATH}"
    body = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        '<s:Body><tds:GetSystemDateAndTime/></s:Body></s:Envelope>'
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/soap+xml; charset=utf-8",
        "SOAPAction": "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
    })
    raw, status, www = b"", 0, ""
    try:
        with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as r:
            status = r.status
            raw = r.read(65536)
    except urllib.error.HTTPError as e:
        status = e.code
        www = (e.headers.get("WWW-Authenticate") or "").lower()
        try:
            raw = e.read(65536)
        except Exception:
            raw = b""
    except Exception:
        return None
    text = raw.decode("utf-8", "replace").lower()
    if status == 200:
        if "getsystemdateandtime" not in text and "envelope" not in text:
            return None
    elif status in (401, 403):
        # Chỉ nhận challenge Basic/Digest hoặc SOAP; tránh web server 401/403 thường.
        if not (("basic" in www or "digest" in www)
                or any(k in text for k in ("soap", "onvif", "envelope", "fault"))):
            return None
    elif status in (400, 405, 415, 500) and any(k in text for k in ("soap", "onvif", "envelope", "fault")):
        pass
    else:
        return None
    name = ""
    if status == 200:
        try:
            root = _onvif_post(
                url, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                "<tds:GetDeviceInformation/>", timeout=max(1.0, timeout),
            )
            manufacturer = _find_text(root, "Manufacturer")
            model = _find_text(root, "Model")
            name = (manufacturer + " " + model).strip()
        except Exception:
            pass
    return {
        "ip": host,
        "host": host,
        "port": port,
        "xaddr": url,
        "name": name or host,
        "source": "scan",
        "auth_required": status != 200,
    }


def onvif_scan_network(subnet: str, timeout: float = 0.8, ports: Optional[List[int]] = None) -> dict:
    """Quét unicast dải IP nội bộ. Hoạt động cả khi Docker bridge chặn multicast."""
    hosts, label = _parse_scan_targets(subnet)
    timeout = max(0.3, min(float(timeout or 0.8), 2.0))
    ports = list(ports or ONVIF_SCAN_PORTS)
    tasks = [(h, p) for h in hosts for p in ports]
    found = []
    with ThreadPoolExecutor(max_workers=min(96, max(1, len(tasks)))) as ex:
        futures = {ex.submit(_onvif_scan_target, h, p, timeout): (h, p) for h, p in tasks}
        for fut in as_completed(futures):
            try:
                dev = fut.result()
            except Exception:
                dev = None
            if dev:
                found.append(dev)
    found.sort(key=lambda d: (ipaddress.ip_address(d["ip"]), d["port"]))
    return {"devices": found, "subnet": label, "scanned": len(hosts), "ports": ports}


def _onvif_profile_meta(prof) -> dict:
    token = prof.attrib.get("token", "")
    pname = ""
    for child in list(prof):
        if _local(child.tag) == "Name" and child.text:
            pname = child.text.strip()
            break
    width = height = 0
    encoding = ""
    for el in prof.iter():
        tag = _local(el.tag)
        if tag == "Resolution":
            for c in el:
                if _local(c.tag) == "Width":
                    width = int(c.text or 0)
                elif _local(c.tag) == "Height":
                    height = int(c.text or 0)
        elif tag == "Encoding" and el.text:
            encoding = el.text.strip()
    return {"token": token, "name": pname, "width": width, "height": height, "encoding": encoding}


def _onvif_pick_profiles(profiles: List[dict]) -> tuple:
    """Chọn luồng chính/phụ theo tên, rồi đến độ phân giải thực tế."""
    usable = [p for p in profiles if p.get("uri")]
    if not usable:
        return None, None

    def area(p):
        return int(p.get("width") or 0) * int(p.get("height") or 0)

    def pick(keys):
        for p in usable:
            low = (p.get("name", "") + " " + p.get("token", "")).lower()
            if any(k in low for k in keys):
                return p
        return None

    main = pick(("main", "primary", "profile_1", "profile1"))
    if main is None:
        main = max(usable, key=area)
    sub = pick(("sub", "second", "minor", "profile_2", "profile2"))
    if sub is main:
        sub = None
    if sub is None:
        others = [p for p in usable if p is not main]
        if others:
            sub = min(others, key=area) if any(area(p) for p in others) else others[0]
    return main, sub


def _onvif_guess_brand(*parts) -> str:
    text = " ".join(str(x or "") for x in parts).lower()
    rules = (
        (("reolink",), "reolink"),
        (("uniview", "unv "), "uniview"),
        (("foscam",), "foscam"),
        (("amcrest",), "amcrest"),
        (("imou", "dahua", "kbvision", "kb vision"), "imou"),
        (("hikvision", "ezviz", "hilook", "annke"), "ezviz"),
        (("hanet",), "hanet"),
        (("tapo", "tp-link", "tp link"), "tapo"),
        (("yoosee",), "yoosee"),
        (("xiaomi", "thingino", "yi "), "xiaomi"),
    )
    for keys, brand in rules:
        if any(k in text for k in keys):
            return brand
    return "onvif"


def onvif_probe(host: str, port: int = 80, username: str = "", password: str = "", xaddr: str = "") -> dict:
    """Lấy tên thiết bị + URL RTSP main/sub qua ONVIF (GetProfiles/GetStreamUri)."""
    device = _onvif_device_url(xaddr or host, port)
    p = urllib.parse.urlsplit(device)
    actual_host = p.hostname or host
    actual_port = int(p.port or 80)
    base = _onvif_base_url(device)
    try:
        with socket.create_connection((actual_host, actual_port), timeout=3.0):
            pass
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"Không kết nối được ONVIF {actual_host}:{actual_port} ({e})")

    manufacturer = model = name = ""
    try:
        root = _onvif_post(device, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                           "<tds:GetDeviceInformation/>", username, password)
        manufacturer = _find_text(root, "Manufacturer")
        model = _find_text(root, "Model")
        name = (manufacturer + " " + model).strip()
    except Exception as e:
        log_event("warn", f"ONVIF GetDeviceInformation lỗi ({actual_host}): {e}")

    def get_profiles(url):
        return _onvif_post(url, "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                           "<trt:GetProfiles/>", username, password)

    media_urls = []
    try:
        caps = _onvif_post(device, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                           "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
                           username, password)
        for el in _find_all(caps, "Media"):
            for child in el.iter():
                if _local(child.tag) == "XAddr" and child.text:
                    media_urls.append(child.text.strip())
                    break
            if media_urls:
                break
    except Exception as e:
        log_event("warn", f"ONVIF GetCapabilities lỗi ({actual_host}): {e}")
    for path in ("/onvif/media_service", "/onvif/Media", "/onvif/media", "/onvif/services"):
        media_urls.append(base + path)
    seen = set()
    unique_urls = []
    for u in media_urls:
        if u and u not in seen:
            seen.add(u)
            unique_urls.append(u)
    media_urls = unique_urls

    root = None
    media_url = ""
    last_error = None
    for candidate in media_urls:
        try:
            root = get_profiles(candidate)
            media_url = candidate
            break
        except Exception as e:
            last_error = e
    if root is None:
        raise HTTPException(status_code=502, detail=f"Không gọi được ONVIF Media: {last_error or 'không có phản hồi'}")

    profiles = []
    for prof in _find_all(root, "Profiles"):
        meta = _onvif_profile_meta(prof)
        token = meta.pop("token", "")
        if not token:
            continue
        uri = ""
        try:
            body = (
                "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{xml_escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
            )
            r = _onvif_post(media_url, "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
                            body, username, password)
            uri = _find_text(r, "Uri")
        except Exception as e:
            log_event("warn", f"ONVIF GetStreamUri lỗi profile {token}: {e}")
        profiles.append({**meta, "token": token, "uri": _with_rtsp_auth(uri, username, password) if uri else ""})

    main, sub = _onvif_pick_profiles(profiles)
    return {
        "host": actual_host,
        "port": actual_port,
        "onvif_port": actual_port,
        "xaddr": device,
        "name": name or actual_host,
        "manufacturer": manufacturer,
        "model": model,
        "brand": _onvif_guess_brand(manufacturer, model),
        "profiles": profiles,
        "main_rtsp_url": main["uri"] if main else "",
        "sub_rtsp_url": sub["uri"] if sub else "",
    }

# ---------------- ONVIF PTZ ----------------
def _cam_onvif_creds(cam_info: dict) -> tuple:
    """(host, username, password) suy ra từ URL RTSP của camera."""
    p = urllib.parse.urlparse((cam_info.get("rtsp_url") or "").strip())
    return (p.hostname or "", urllib.parse.unquote(p.username or ""),
            urllib.parse.unquote(p.password or ""))

def _onvif_ptz_url(host: str, port: int, username: str, password: str) -> str:
    """Tìm XAddr dịch vụ PTZ qua GetCapabilities (mặc định /onvif/ptz_service)."""
    device = f"http://{host}:{int(port)}/onvif/device_service"
    fallback = f"http://{host}:{int(port)}/onvif/ptz_service"
    try:
        caps = _onvif_post(device, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                           "<tds:GetCapabilities><tds:Category>PTZ</tds:Category></tds:GetCapabilities>",
                           username, password)
        for el in _find_all(caps, "PTZ"):
            for child in el.iter():
                if _local(child.tag) == "XAddr" and child.text and child.text.strip():
                    return child.text.strip()
    except Exception as e:
        log_event("warn", f"ONVIF GetCapabilities(PTZ) lỗi: {e}")
    return fallback

def _onvif_get_profile_token(media_url: str, username: str, password: str) -> str:
    root = _onvif_post(media_url, "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                       "<trt:GetProfiles/>", username, password)
    for prof in _find_all(root, "Profiles"):
        token = prof.attrib.get("token", "")
        if token:
            return token
    return ""

def onvif_ptz_move(cam_id: str, action: str, speed: float = 0.6, duration: float = 0.5,
                   preset: str = "") -> dict:
    """Điều khiển PTZ qua ONVIF: ContinuousMove + Stop, hoặc GotoPreset."""
    cam_info = CONFIG.get("cameras", {}).get(cam_id) or {}
    if not cam_info:
        raise HTTPException(status_code=404, detail="Không tìm thấy camera")
    host, user, pw = _cam_onvif_creds(cam_info)
    if not host:
        raise HTTPException(status_code=400, detail="URL RTSP không có host để suy ra ONVIF")
    port = int(cam_info.get("onvif_port") or 80)
    ptz_url = _onvif_ptz_url(host, port, user, pw)
    base = f"http://{host}:{int(port)}"
    try:
        token = _onvif_get_profile_token(base + "/onvif/media_service", user, pw)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Không kết nối được ONVIF của camera ({host}:{port}): {e}")
    if not token:
        raise HTTPException(status_code=502, detail="Camera không trả về ProfileToken ONVIF")

    def move(x: float, y: float, z: float):
        body = (
            "<ptz:ContinuousMove><ptz:ProfileToken>" + xml_escape(token) + "</ptz:ProfileToken>"
            "<ptz:Velocity><tt:PanTilt x=\"" + f"{x:.2f}" + "\" y=\"" + f"{y:.2f}" + "\"/>"
            "<tt:Zoom x=\"" + f"{z:.2f}" + "\"/></ptz:Velocity></ptz:ContinuousMove>"
        )
        _onvif_post(ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove", body, user, pw)

    def stop():
        body = "<ptz:Stop><ptz:ProfileToken>" + xml_escape(token) + "</ptz:ProfileToken>"
        body += "<ptz:PanTilt>true</ptz:PanTilt><ptz:Zoom>true</ptz:Zoom></ptz:Stop>"
        _onvif_post(ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/Stop", body, user, pw)

    s = max(0.1, min(abs(float(speed or 0.6)), 1.0))
    moves = {
        "up": (0, s, 0), "down": (0, -s, 0), "left": (-s, 0, 0), "right": (s, 0, 0),
        "upleft": (-s, s, 0), "upright": (s, s, 0), "downleft": (-s, -s, 0), "downright": (s, -s, 0),
        "zoomin": (0, 0, s), "zoomout": (0, 0, -s),
    }
    try:
        if action in ("home", "preset") and action == "home":
            _onvif_post(ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/GotoHomePosition",
                        "<ptz:GotoHomePosition><ptz:ProfileToken>" + xml_escape(token) + "</ptz:ProfileToken></ptz:GotoHomePosition>",
                        user, pw)
            return {"status": "ok", "action": action}
        if action == "preset":
            if not preset:
                raise HTTPException(status_code=400, detail="Thiếu token preset")
            _onvif_post(ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/GotoPreset",
                        "<ptz:GotoPreset><ptz:ProfileToken>" + xml_escape(token) + "</ptz:ProfileToken>"
                        "<ptz:PresetToken>" + xml_escape(preset) + "</ptz:PresetToken></ptz:GotoPreset>",
                        user, pw)
            return {"status": "ok", "action": action, "preset": preset}
        if action == "stop":
            stop()
            return {"status": "ok", "action": "stop"}
        if action not in moves:
            raise HTTPException(status_code=400, detail=f"Hành động PTZ không hợp lệ: {action}")
        x, y, z = moves[action]
        move(x, y, z)
        time.sleep(max(0.1, min(float(duration or 0.5), 10.0)))
        stop()
        return {"status": "ok", "action": action, "duration": duration}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PTZ lỗi: {e}")

def onvif_ptz_presets(cam_id: str) -> list:
    cam_info = CONFIG.get("cameras", {}).get(cam_id) or {}
    if not cam_info:
        raise HTTPException(status_code=404, detail="Không tìm thấy camera")
    host, user, pw = _cam_onvif_creds(cam_info)
    if not host:
        raise HTTPException(status_code=400, detail="URL RTSP không có host để suy ra ONVIF")
    port = int(cam_info.get("onvif_port") or 80)
    ptz_url = _onvif_ptz_url(host, port, user, pw)
    try:
        token = _onvif_get_profile_token(f"http://{host}:{int(port)}/onvif/media_service", user, pw)
    except Exception:
        return []
    if not token:
        return []
    try:
        root = _onvif_post(ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/GetPresets",
                           "<ptz:GetPresets><ptz:ProfileToken>" + xml_escape(token) + "</ptz:ProfileToken></ptz:GetPresets>",
                           user, pw)
    except Exception:
        return []
    out = []
    for el in _find_all(root, "Preset"):
        out.append({"token": el.attrib.get("token", ""), "name": (_find_text(el, "Name") or "").strip()})
    return out

def _is_rtsp(url: str) -> bool:
    return (url or "").lower().startswith(("rtsp://", "rtsps://"))

def _ffmpeg_base_args(threads: Optional[int] = None) -> List[str]:
    n = FFMPEG_THREADS if threads is None else max(1, int(threads))
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-filter_threads", str(n),
            "-filter_complex_threads", str(n)]


def _input_args(url: str, cam_info: dict, threads: Optional[int] = None) -> List[str]:
    n = FFMPEG_THREADS if threads is None else max(1, int(threads))
    args = ["-threads", str(n)]
    if _is_rtsp(url):
        args += [
            "-rtsp_transport", cam_info.get("rtsp_transport", "tcp"),
            "-rtsp_flags", "prefer_tcp",
            "-timeout", "10000000",
        ]
        if str(cam_info.get("audio_mode", "aac")).lower() == "none":
            # Camera/NVR có audio AAC lỗi AU headers: không yêu cầu audio để
            # FFmpeg không chờ/parse audio và HLS/ghi hình không bị treo.
            args += ["-allowed_media_types", "video"]
    return args


def _terminate_process(proc, timeout: float = 3):
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)

def buffer_root() -> str:
    """Thư mục đệm cho chế độ Motion. Mặc định RAM (/dev/shm) cho nhanh;
    đặt ram_buffer=false để ghi ra đĩa (RAM nhỏ / ổn định hơn)."""
    return BUFFER_DIR if CONFIG.get("ram_buffer", True) else os.path.join(STORAGE_DIR, "buffer")

# ---------------- go2rtc (restream nội bộ: 1 phiên/camera chia nhiều nguồn) ----------------
GO2RTC_MODE = os.environ.get("GO2RTC", "auto").lower()   # auto | on | off
GO2RTC_BIN = os.environ.get("GO2RTC_BIN", "go2rtc")
GO2RTC_RTSP = "rtsp://127.0.0.1:8554"
GO2RTC_API = "127.0.0.1:1984"
try:
    _webrtc_port = int(os.environ.get("WEBRTC_PORT", "8556") or "8556")
except ValueError:
    _webrtc_port = 8556
GO2RTC_WEBRTC_PORT = max(1024, min(65535, _webrtc_port))
GO2RTC_WEBRTC_HOST = os.environ.get("WEBRTC_PUBLIC_HOST", "").strip()
GO2RTC_CONFIG = os.path.join(STORAGE_DIR, "go2rtc.yaml")
_go2rtc = {"proc": None, "running": False, "error": "", "enabled": False}

try:
    import yaml as _yaml
except Exception:
    _yaml = None

def go2rtc_available() -> bool:
    return bool(shutil.which(GO2RTC_BIN))

def _g2_name(cam_id: str) -> str:
    """Tên stream an toàn cho go2rtc (không dấu cách/ký tự đặc biệt)."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", cam_id)

# Camera/NVR có thể trả 404 cho luồng phụ (URL cũ, đủ phiên, H.265 lỗi tham số).
# Ghi nhớ lỗi trong 10 phút để tự dùng luồng chính thay vì lặp lại lỗi.
STREAM_FAIL_RETRY_SEC = 600
_stream_fail: Dict[str, dict] = {}
_stream_fail_lock = threading.Lock()

def _cam_source_sig(cam_info: dict):
    return ((cam_info.get("rtsp_url") or "").strip(),
            (cam_info.get("sub_rtsp_url") or "").strip())

def _stream_fail_until(cam_id: str, cam_info: dict, kind: str) -> float:
    sig = _cam_source_sig(cam_info)
    now = time.time()
    with _stream_fail_lock:
        rec = _stream_fail.get(cam_id)
        if not rec or rec.get("sig") != sig:
            return 0.0
        until = float(rec.get(kind) or 0)
        if until <= now:
            rec.pop(kind, None)
            rec.pop(f"{kind}_reason", None)
            if not any(rec.get(k) for k in ("sub", "restream")):
                _stream_fail.pop(cam_id, None)
            return 0.0
        return until

def _substream_usable(cam_id: str, cam_info: dict) -> bool:
    return _stream_fail_until(cam_id, cam_info, "sub") <= 0

def _restream_usable(cam_id: str, cam_info: dict) -> bool:
    return _stream_fail_until(cam_id, cam_info, "restream") <= 0

def _mark_stream_failure(cam_id: str, cam_info: dict, kind: str, reason: str = ""):
    if kind not in ("sub", "restream"):
        return
    sig = _cam_source_sig(cam_info)
    now = time.time()
    with _stream_fail_lock:
        rec = _stream_fail.get(cam_id)
        if not rec or rec.get("sig") != sig:
            rec = {"sig": sig}
        previous = float(rec.get(kind) or 0)
        rec[kind] = now + STREAM_FAIL_RETRY_SEC
        rec[f"{kind}_reason"] = reason
        _stream_fail[cam_id] = rec
    if previous > now:
        return
    if kind == "sub":
        log_event("warn", "Luồng phụ lỗi; tự chuyển sang luồng chính cho live/ghi hình", cam_id)
    else:
        log_event("warn", "Restream go2rtc lỗi; tạm nối trực tiếp camera", cam_id)

def _clear_stream_failures(cam_id: str):
    with _stream_fail_lock:
        _stream_fail.pop(cam_id, None)

def _line_hits_source(line: str, url: str) -> bool:
    low = (line or "").lower()
    if not low:
        return False
    markers = ("400", "404", "401", "403", "server returned 5",
               "connection refused", "timed out", "no route")
    if url and url in (line or ""):
        return any(marker in low for marker in markers)
    if "method describe failed" in low:
        return any(marker in low for marker in ("400", "404", "401", "403", "5"))
    return False

def _g2_live_name(cam_id: str, cam_info: dict) -> str:
    """Stream go2rtc cho live/WebRTC: ưu tiên luồng phụ (nhẹ CPU/băng thông)."""
    name = _g2_name(cam_id)
    main = (cam_info.get("rtsp_url") or "").strip()
    sub = (cam_info.get("sub_rtsp_url") or "").strip()
    use_sub = bool(sub and sub != main and _substream_usable(cam_id, cam_info))
    return f"{name}_sub" if use_sub else name

def _go2rtc_streams() -> Dict[str, str]:
    """Tên stream go2rtc -> URL gốc. Camera có luồng phụ tạo thêm <cam>_sub."""
    streams = {}
    for cam_id, cam in CONFIG.get("cameras", {}).items():
        name = _g2_name(cam_id)
        url = (cam.get("rtsp_url") or "").strip()
        if url:
            streams[name] = url
        sub = (cam.get("sub_rtsp_url") or "").strip()
        if sub and sub != url:
            streams[f"{name}_sub"] = sub
    return streams

def write_go2rtc_config():
    """Ghi cấu hình go2rtc. GIỮ NGUYÊN stream người dùng tự thêm
    (vd hanet_nhatren, san) — chỉ thêm/cập nhật stream theo camera LiteNVR."""
    streams: Dict[str, Any] = {}
    existing: Dict[str, Any] = {}
    if _yaml and os.path.exists(GO2RTC_CONFIG):
        try:
            with open(GO2RTC_CONFIG, "r", encoding="utf-8") as f:
                existing = _yaml.safe_load(f) or {}
            if isinstance(existing.get("streams"), dict):
                streams.update(existing["streams"])
        except Exception:
            existing = {}
    streams.update(_go2rtc_streams())
    lines = [
        "log:",
        "  level: error",
        "api:",
        f'  listen: "{GO2RTC_API}"',
        "rtsp:",
        '  listen: "127.0.0.1:8554"',
        "webrtc:",
        f'  listen: ":{GO2RTC_WEBRTC_PORT}"',
        "streams:",
    ]
    for name, url in streams.items():
        try:
            lines.append(f"  {name}: {json.dumps(url, ensure_ascii=False)}")
        except Exception:
            continue
    for key, val in existing.items():
        if key in ("log", "api", "rtsp", "webrtc", "streams"):
            continue
        if _yaml:
            try:
                lines.append(_yaml.safe_dump({key: val}, allow_unicode=True, sort_keys=False).rstrip())
            except Exception:
                pass
    with open(GO2RTC_CONFIG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def _port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except Exception:
        return False

def start_go2rtc():
    """Chạy go2rtc nội bộ (nếu có binary) để chia 1 phiên RTSP cho nhiều client."""
    if GO2RTC_MODE == "off":
        _go2rtc["enabled"] = False
        return
    if not go2rtc_available():
        _go2rtc["enabled"] = False
        if GO2RTC_MODE == "on":
            log_event("error", "GO2RTC=on nhưng không tìm thấy binary go2rtc")
        return
    proc = _go2rtc.get("proc")
    if proc and proc.poll() is None:
        return
    if _port_in_use("127.0.0.1", 8554) or _port_in_use("127.0.0.1", 1984):
        _go2rtc["enabled"] = False
        _go2rtc["running"] = False
        _go2rtc["error"] = "Cổng 8554/1984 đang được dùng bởi go2rtc khác"
        log_event("warn", "Cổng 8554/1984 đang có go2rtc khác chạy — không khởi động restream nội bộ, camera sẽ kết nối trực tiếp")
        return
    try:
        write_go2rtc_config()
        proc = subprocess.Popen(
            [GO2RTC_BIN, "-config", GO2RTC_CONFIG],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _go2rtc["proc"] = proc
        _go2rtc["enabled"] = True
        _go2rtc["running"] = True
        _go2rtc["error"] = ""
        log_event("info", f"go2rtc khởi động (restream nội bộ {GO2RTC_RTSP})")
    except Exception as e:
        _go2rtc["running"] = False
        _go2rtc["error"] = str(e)
        log_event("warn", f"Không chạy được go2rtc: {e}")

def _wait_go2rtc_api(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _go2rtc.get("running"):
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:1984/api/streams", timeout=0.6) as resp:
                return 200 <= resp.status < 500
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False

def stop_go2rtc():
    proc = _go2rtc.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
    _go2rtc["proc"] = None
    _go2rtc["running"] = False
    for _ in range(10):
        if not _port_in_use("127.0.0.1", 8554):
            break
        time.sleep(0.2)

def refresh_go2rtc():
    """Cập nhật danh sách stream khi thêm/sửa/xóa camera."""
    if not _go2rtc.get("enabled"):
        return
    write_go2rtc_config()
    stop_go2rtc()
    start_go2rtc()

def go2rtc_monitor():
    while not _shutdown.wait(10):
        if not _go2rtc.get("enabled"):
            continue
        proc = _go2rtc.get("proc")
        if proc and proc.poll() is not None:
            log_event("warn", "go2rtc dừng bất thường, đang khởi động lại")
            _go2rtc["running"] = False
            start_go2rtc()

def cam_source_urls(cam_id: str, cam_info: dict):
    """URL đầu vào cho ffmpeg. Nếu go2rtc đang chạy, dùng restream nội bộ
    (1 phiên/camera cho mọi nhu cầu ghi + live + snapshot)."""
    main_url = cam_info.get("rtsp_url", "")
    sub_url = cam_info.get("sub_rtsp_url") or main_url
    if sub_url != main_url and not _substream_usable(cam_id, cam_info):
        sub_url = main_url
    if _go2rtc.get("running") and (cam_info.get("rtsp_url") or "").strip():
        name = _g2_name(cam_id)
        streams = _go2rtc_streams()
        if name in streams:
            parsed = urllib.parse.urlparse(cam_info["rtsp_url"])
            paths = {name, f"{name}_sub"}
            if parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == 8554 \
                    and parsed.path.strip("/") in paths:
                st = cam_status.setdefault(cam_id, {})
                if not st.get("_self_loop_warn"):
                    st["_self_loop_warn"] = True
                    log_event("warn", f"Camera {cam_id} đang trỏ vào chính restream nội bộ — bỏ qua restream để tránh vòng lặp", cam_id)
            elif _restream_usable(cam_id, cam_info):
                want_sub = sub_url != main_url
                main_url = f"{GO2RTC_RTSP}/{name}"
                sub_url = f"{GO2RTC_RTSP}/{name}_sub" if want_sub and f"{name}_sub" in streams else main_url
    return main_url, sub_url

def webrtc_candidate_host(request: Request) -> str:
    raw = (GO2RTC_WEBRTC_HOST or request.headers.get("x-forwarded-host")
           or request.headers.get("host", ""))
    raw = (raw.split(",")[0] or "").strip()
    if not raw:
        return ""
    try:
        host = urllib.parse.urlsplit("//" + raw).hostname or ""
    except Exception:
        return ""
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_DGRAM)
        v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
        v6 = [i[4][0] for i in infos if i[0] == socket.AF_INET6]
        return (v4 or v6 or [""])[0]
    except Exception:
        return ""


def inject_webrtc_candidates(sdp: str, host: str, port: int) -> str:
    if not host or not sdp:
        return sdp
    candidates = [
        f"a=candidate:9101 1 udp 2130706431 {host} {port} typ host generation 0",
        f"a=candidate:9102 1 tcp 2130706430 {host} {port} typ host tcptype passive generation 0",
    ]
    out: List[str] = []
    section: List[str] = []

    def flush():
        if not section:
            return
        if section[0].startswith(("m=video", "m=audio")) and not any(
                host in line and f" {port} typ " in line for line in section):
            end = next((i for i, line in enumerate(section)
                        if line.strip() == "a=end-of-candidates"), len(section))
            if end == len(section):
                while end > 0 and not section[end - 1].strip():
                    end -= 1
            section[end:end] = candidates
        out.extend(section)

    for line in sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            flush()
            section = [line]
        else:
            section.append(line)
    flush()
    return "\r\n".join(line for line in out if line) + "\r\n"


def build_ffmpeg_cmd(cam_id: str, cam_info: dict, video_codec: Optional[str] = None, include_live: bool = True):
    main_url, sub_url = cam_source_urls(cam_id, cam_info)
    audio_mode = cam_info.get("audio_mode", "aac")  # aac | none | copy
    record_mode = cam_info.get("record_mode", "continuous")  # continuous | motion | off
    if video_codec is None:
        video_codec = resolve_video_codec(cam_id, cam_info)  # copy | h264 cho LIVE
    live_transcode = include_live and video_codec == "h264"
    live_threads = LIVE_TRANSCODE_THREADS if live_transcode else FFMPEG_THREADS
    # Bản ghi LUÔN copy codec gốc để nhẹ CPU/RAM (kể cả HEVC). Khi xem lại sẽ tự
    # chuyển mã. Lựa chọn "h264" chỉ ép transcode cho LUỒNG LIVE.

    # H.264 transcode cho LUỒNG LIVE khi camera dùng H.265/HEVC (trình duyệt không phát được HEVC)
    # ultrafast + 1 thread: giảm mạnh CPU/RAM so với veryfast mà vẫn đủ nét cho live.
    h264_args = [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-threads", str(live_threads), "-g", "50", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
    ]
    vcodec_args = h264_args if video_codec == "h264" else ["-c:v", "copy"]

    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    cam_buf_dir = os.path.join(buffer_root(), cam_id)
    cam_hls_dir = os.path.join(HLS_DIR, cam_id)
    for d in [cam_rec_dir, cam_buf_dir, cam_hls_dir]:
        os.makedirs(d, exist_ok=True)

    input_url = sub_url if include_live and record_mode == "off" else main_url
    cmd = [
        *_ffmpeg_base_args(live_threads),
        "-fflags", "+genpts+igndts+discardcorrupt",
        *_input_args(input_url, cam_info, live_threads),
        "-i", input_url,
    ]

    # Nếu có luồng phụ riêng cho Live View thì thêm input số 1
    use_dual_input = include_live and record_mode != "off" and sub_url != main_url
    if use_dual_input:
        cmd.extend([
            "-fflags", "+genpts+igndts+discardcorrupt",
            *_input_args(sub_url, cam_info, live_threads),
            "-i", sub_url
        ])

    # Nhánh 1: Ghi hình (Continuous lưu thẳng HDD, Motion lưu tạm vào RAM buffer 15s/đoạn)
    if record_mode in ("continuous", "motion"):
        cmd.extend(["-map", "0:v:0"])
        if audio_mode == "aac":
            cmd.extend(["-map", "0:a?", "-c:v", "copy", "-c:a", "aac"])
        elif audio_mode == "copy":
            cmd.extend(["-map", "0:a?", "-c:v", "copy", "-c:a", "copy"])
        else:
            cmd.extend(["-c:v", "copy", "-an"])

        seg_time = "60" if record_mode == "continuous" else str(MOTION_BUFFER_SEGMENT_SEC)
        target_dir = cam_rec_dir if record_mode == "continuous" else cam_buf_dir

        cmd.extend([
            "-f", "segment",
            "-segment_time", seg_time,
            "-reset_timestamps", "1",
            "-strftime", "1",
            os.path.join(target_dir, "%Y-%m-%d_%H-%M-%S.mp4")
        ])

    # Báo tiến độ ra stderr (key=value) để giám sát fps và phát hiện luồng đứng.
    cmd.extend(["-progress", "pipe:2", "-stats_period", "5"])

    # Nhánh 2: Phát trực tiếp HLS trên RAM (/dev/shm/hls)
    # include_live=False khi không ai xem (tiết kiệm CPU transcode).
    if include_live:
        live_input_idx = "1:v:0" if use_dual_input else "0:v:0"
        live_args = list(vcodec_args)
        if video_codec == "h264":
            # Ép keyframe đúng nhịp segment để HLS không phải chờ GOP dài của camera.
            live_args += ["-force_key_frames", f"expr:gte(t,n_forced*{LIVE_HLS_TIME})"]
            live_h = LIVE_MAX_HEIGHT
            if LIVE_TRANSCODE_MAX_HEIGHT > 0:
                live_h = min(live_h, LIVE_TRANSCODE_MAX_HEIGHT) if live_h > 0 else LIVE_TRANSCODE_MAX_HEIGHT
            if live_h > 0:
                live_args = ["-vf", f"scale=-2:'min({live_h},ih)'"] + live_args
        live_audio = []
        if audio_mode != "none":
            live_audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
        else:
            live_audio = ["-an"]
        cmd.extend([
            "-map", live_input_idx,
            *live_args, *live_audio,
            "-f", "hls",
            "-hls_time", str(LIVE_HLS_TIME),
            "-hls_init_time", str(LIVE_HLS_TIME),
            "-hls_list_size", str(LIVE_HLS_LIST),
            "-hls_delete_threshold", "1",
            "-hls_flags", "delete_segments+independent_segments+temp_file",
            "-hls_allow_cache", "0",
            os.path.join(cam_hls_dir, "live.m3u8")
        ])
    return cmd

# ---------------- Lịch ghi hình theo giờ/ngày ----------------
def _parse_hm(s: str) -> int:
    parts = str(s or "00:00").split(":")
    h = int(parts[0]) if len(parts) > 0 else 0
    m = int(parts[1]) if len(parts) > 1 else 0
    return max(0, min(h, 23)) * 60 + max(0, min(m, 59))

def schedule_record_mode(cam_info: dict, now: Optional[datetime] = None) -> str:
    """Chế độ ghi hiệu lực: continuous | motion | off.
    Nếu camera bật lịch: ngoài mọi khung giờ -> off. Khung qua đêm (from > to)
    kéo dài qua nửa đêm. days: 0=Thứ 2 ... 6=Chủ nhật (để trống = mọi ngày)."""
    base = cam_info.get("record_mode", "continuous")
    windows = cam_info.get("schedule") or []
    if not cam_info.get("schedule_enabled") or not windows:
        return base
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    weekday = now.weekday()
    for w in windows:
        if not isinstance(w, dict):
            continue
        try:
            days = [int(d) for d in (w.get("days") or [])]
            if days and weekday not in days:
                continue
            f, t = _parse_hm(w.get("from", "00:00")), _parse_hm(w.get("to", "23:59"))
            inside = (f <= cur <= t) if f <= t else (cur >= f or cur <= t)
            if inside:
                return (w.get("mode") or base)
        except Exception:
            continue
    return "off"

def mark_viewer(cam_id: str):
    """Đánh dấu có người đang xem live (heartbeat / tải segment HLS)."""
    with viewer_lock:
        cam_viewers[cam_id] = time.time()

def viewers_active(cam_id: str) -> bool:
    with viewer_lock:
        return (time.time() - cam_viewers.get(cam_id, 0)) <= 15

def _needs_transcode(cam_id: str) -> bool:
    cam = CONFIG.get("cameras", {}).get(cam_id, {})
    vc = cam.get("video_codec", "auto")
    if vc == "copy":
        return False
    if vc == "h264":
        return True
    st = cam_status.get(cam_id, {})
    codec = st.get("detected_codec", "")
    if not codec:
        return True
    return not _is_browser_safe_h264(codec, st.get("detected_pix_fmt", ""), st.get("detected_profile", ""))

def _clear_hls_playlist(cam_id: str):
    """Xoá playlist cũ để HA/UI không đọc nhầm HLS của phiên ffmpeg trước."""
    try:
        os.remove(os.path.join(HLS_DIR, cam_id, "live.m3u8"))
    except OSError:
        pass


def start_camera_worker(cam_id: str):
    with worker_lock:
        if _shutdown.is_set():
            return
        old = worker_tokens.get(cam_id)
        if old and not old.is_set():
            return  # đang chạy rồi
        if old:
            old.set()
        token = threading.Event()
        worker_tokens[cam_id] = token
        proc = ffmpeg_processes.get(cam_id)
    if not proc or proc.poll() is not None:
        _clear_hls_playlist(cam_id)
    threading.Thread(target=_camera_worker_loop, args=(cam_id, token), daemon=True).start()

_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
                  "out_time", "dup_frames", "drop_frames", "speed", "progress"}
_PROGRESS_FRAME_KEYS = {"frame", "fps", "out_time_us", "out_time_ms", "out_time"}
_PROGRESS_STREAM_Q_RE = re.compile(r"^stream_\d+_\d+_q\s*=")
_REPEATED_LOG_RE = re.compile(r"^last message repeated\b", re.I)
_AUDIO_AU_ERROR_RE = re.compile(r"error parsing au headers", re.I)
_HEVC_SOURCE_WARN_RE = re.compile(
    r"(?:\b(?:pps|sps|vps)\s+id\s+out\s+of\s+range\b"
    r"|\berror\s+parsing\s+nal\s+unit\b"
    r"|\bcould\s+not\s+find\s+ref\b)",
    re.I,
)


def _is_audio_au_warning(line: str) -> bool:
    """Lỗi gói AAC/AU của camera: FFmpeg bỏ gói audio nhưng video vẫn chạy."""
    return bool(_AUDIO_AU_ERROR_RE.search(line or ""))


def _is_hevc_source_warning(line: str) -> bool:
    """Tham số H.265 (PPS/SPS/NAL) không chuẩn từ NVR/go2rtc.

    Nhiều NVR Hikvision gửi PPS không đúng chuẩn nhưng FFmpeg vẫn giải mã và
    phát được video. Đây là cảnh báo nguồn, không phải lỗi làm dừng luồng.
    """
    low = (line or "").lower()
    return "hevc" in low and bool(_HEVC_SOURCE_WARN_RE.search(low))


def _note_progress(line: str, cam_id: str) -> bool:
    """Cập nhật sức khoẻ luồng từ một dòng tiến độ ffmpeg. Trả True nếu là dòng tiến độ."""
    if _PROGRESS_STREAM_Q_RE.match(line.strip()):
        return True
    key, _, val = line.partition("=")
    key = key.strip()
    if key not in _PROGRESS_KEYS:
        return False
    h = cam_health.setdefault(cam_id, {})
    if key in _PROGRESS_FRAME_KEYS:
        h["last_frame_at"] = time.time()
        if key == "frame":
            try:
                if int(val) > 0:
                    h["got_frame"] = True
            except ValueError:
                pass
    if key == "fps":
        try:
            h["fps"] = float(val)
        except ValueError:
            pass
    elif key == "out_time_ms":
        try:
            h["out_time_ms"] = int(val)
        except ValueError:
            pass
    elif key == "progress":
        h["state"] = val.strip()
    return True


def health_snapshot() -> Dict[str, dict]:
    now = time.time()
    out: Dict[str, dict] = {}
    for cid, h in cam_health.items():
        last = h.get("last_frame_at")
        out[cid] = {
            "fps": h.get("fps"),
            "stalled": bool(h.get("stalled")),
            "state": h.get("state"),
            "age_sec": round(now - last, 1) if last else None,
        }
    return out


# Tự khởi động lại ffmpeg nếu RSS vượt ngưỡng (chống rò rỉ/4K transcode quá nặng).
# 0 = tắt. Chỉ áp dụng sau thời gian grace để tránh restart lúc ffmpeg mới khởi động.
CAMERA_MAX_RSS_MB = max(0, int(os.environ.get("CAMERA_MAX_RSS_MB", "640") or "0"))
CAMERA_RSS_GRACE_SEC = max(10, int(os.environ.get("CAMERA_RSS_GRACE_SEC", "60") or "60"))

def _auto_disable_audio_on_stall(cam_id: str, health: dict) -> bool:
    """Tắt âm thanh khi luồng đứng trước khung hình đầu tiên.

    Một số NVR/go2rtc gửi audio RTSP hỏng làm ffmpeg chờ vô hạn ở bước map
    audio, không bao giờ ghi khung hình. Video-only vẫn chạy bình thường.
    """
    if health.get("got_frame"):
        return False
    cam = (CONFIG.get("cameras") or {}).get(cam_id)
    if not isinstance(cam, dict):
        return False
    if str(cam.get("audio_mode") or "aac").strip().lower() == "none":
        return False
    cam["audio_mode"] = "none"
    saved = True
    try:
        save_config(CONFIG)
    except Exception as e:
        saved = False
        log_event("warn", f"Không lưu được cấu hình tắt âm thanh: {e}", cam_id)
    log_event("warn", "Luồng đứng trước khi có khung hình; tự tắt âm thanh để video chạy tiếp"
              + ("" if saved else " (chưa lưu được cấu hình)"), cam_id)
    return True

def stream_health_supervisor():
    """Theo dõi tiến độ ffmpeg mỗi camera; khởi động lại nếu luồng đứng hoặc RAM quá cao."""
    stall_sec = 20
    while not _shutdown.is_set():
        try:
            now = time.time()
            with worker_lock:
                procs = list(ffmpeg_processes.items())
            for cam_id, proc in procs:
                h = cam_health.setdefault(cam_id, {})
                last = h.get("last_frame_at")
                stalled = bool(proc.poll() is None and last and now - last > stall_sec)
                h["stalled"] = stalled
                if stalled and not h.get("restart_attempted"):
                    h["restart_attempted"] = True
                    if not _auto_disable_audio_on_stall(cam_id, h):
                        log_event("warn", f"Luồng đứng >{stall_sec}s (không có khung mới), khởi động lại", cam_id)
                    _restart_camera_process(cam_id, proc)
                elif not stalled:
                    h["restart_attempted"] = False
                    if CAMERA_MAX_RSS_MB and proc.poll() is None:
                        started = float(h.get("started_at") or 0)
                        if started and now - started >= CAMERA_RSS_GRACE_SEC:
                            rss_mb = _proc_rss_kb(proc.pid) / 1024.0
                            if rss_mb > CAMERA_MAX_RSS_MB:
                                h["rss_high"] = h.get("rss_high", 0) + 1
                                if h["rss_high"] >= 3:
                                    h["rss_high"] = 0
                                    log_event("warn",
                                              f"RAM ffmpeg {rss_mb:.0f} MB > {CAMERA_MAX_RSS_MB} MB, "
                                              "khởi động lại để giải phóng", cam_id)
                                    _restart_camera_process(cam_id, proc)
                            else:
                                h["rss_high"] = 0
        except Exception as e:
            log_event("warn", f"stream_health_supervisor: {e}")
        _shutdown.wait(10)


CAMERA_OFFLINE_ALERT_SEC = int(os.environ.get("CAMERA_OFFLINE_ALERT_SEC", "60") or "60")


def camera_offline_state(cam_id: str) -> dict:
    """Trạng thái mất kết nối hiện tại của camera (dùng chung cho API và cảnh báo)."""
    info = (CONFIG.get("cameras") or {}).get(cam_id) or {}
    expected = bool(info) and schedule_record_mode(info) != "off"
    with _cam_offline_lock:
        rec = dict(_cam_offline.get(cam_id) or {})
    down_since = float(rec.get("down_since") or 0)
    offline = bool(expected and down_since and time.time() - down_since >= 20)
    return {"expected": expected, "offline": offline,
            "down_since": int(down_since) if down_since else 0,
            "alerted": bool(rec.get("alerted"))}


def camera_offline_supervisor():
    """Báo Telegram/webhook khi camera cần ghi hình mất kết nối quá lâu, báo lại khi hồi phục."""
    while not _shutdown.is_set():
        try:
            now = time.time()
            cfg = CONFIG.get("notify") or {}
            alerts_on = bool(cfg.get("camera_offline_alert", True))
            with worker_lock:
                procs = dict(ffmpeg_processes)
            for cam_id, info in list((CONFIG.get("cameras") or {}).items()):
                expected = schedule_record_mode(info) != "off"
                proc = procs.get(cam_id)
                alive = bool(proc and proc.poll() is None and cam_status.get(cam_id, {}).get("running"))
                with _cam_offline_lock:
                    rec = _cam_offline.setdefault(cam_id, {"down_since": 0, "alerted": False})
                    if not expected:
                        rec["down_since"] = 0
                        rec["alerted"] = False
                        continue
                    if alive:
                        was_alerted = bool(rec.get("alerted"))
                        down_since = float(rec.get("down_since") or 0)
                        rec["down_since"] = 0
                        rec["alerted"] = False
                        if was_alerted and alerts_on:
                            name = info.get("name", cam_id)
                            mins = max(1, int((now - down_since) / 60))
                            threading.Thread(
                                target=send_alert,
                                args=(f"✅ {name}: đã kết nối lại sau {mins} phút.",),
                                daemon=True).start()
                        continue
                    if not rec.get("down_since"):
                        rec["down_since"] = now
                    down_for = now - float(rec["down_since"])
                    if alerts_on and not rec.get("alerted") and down_for >= CAMERA_OFFLINE_ALERT_SEC:
                        rec["alerted"] = True
                        name = info.get("name", cam_id)
                        err = (cam_status.get(cam_id, {}).get("last_error") or "").strip()
                        text = (f"⚠️ {name}: mất kết nối hơn {max(1, int(down_for / 60))} phút.")
                        if err:
                            text += f"\nLỗi: {mask_secrets(err)[:300]}"
                        log_event("warn", f"Camera mất kết nối > {int(down_for)}s, đã gửi cảnh báo", cam_id)
                        threading.Thread(target=send_alert, args=(text,), daemon=True).start()
        except Exception as e:
            log_event("warn", f"camera_offline_supervisor: {mask_secrets(str(e))}")
        _shutdown.wait(10)


def _camera_worker_loop(cam_id: str, token: threading.Event):
    backoff = 2
    st = cam_status.setdefault(cam_id, {})
    st["log"] = st.get("log", deque(maxlen=20))
    while not token.is_set() and not _shutdown.is_set():
        cam_info = CONFIG.get("cameras", {}).get(cam_id)
        if not cam_info:
            break
        sched = schedule_record_mode(cam_info)
        record_needed = sched != "off"
        viewers = viewers_active(cam_id)
        if not record_needed and not viewers:
            st["stop_reason"] = "idle"
            st["last_error"] = ""
            st["last_warning"] = ""
            log_event("info", "Dừng live (không còn người xem)", cam_id)
            break
        resolved = resolve_video_codec(cam_id, cam_info)
        # Khi không ai xem: nếu luồng gốc đã browser-safe thì giữ HLS (rẻ),
        # còn nếu phải transcode thì bỏ nhánh HLS để tiết kiệm CPU.
        include_live = viewers
        if token.is_set() or _shutdown.is_set():
            break
        eff_info = dict(cam_info)
        eff_info["record_mode"] = sched if sched != "off" else "off"
        if include_live:
            _clear_hls_playlist(cam_id)
        main_url, sub_url = cam_source_urls(cam_id, eff_info)
        substream_in_use = include_live and sub_url != main_url
        cmd = build_ffmpeg_cmd(cam_id, eff_info, video_codec=resolved, include_live=include_live)
        started = time.time()
        try:
            with worker_lock:
                if token.is_set() or _shutdown.is_set() or worker_tokens.get(cam_id) is not token:
                    break
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
                ffmpeg_processes[cam_id] = proc
                st["running"] = True
                st["last_start"] = int(started)
                st["last_error"] = ""
                st["last_warning"] = ""
                st["last_exit_code"] = None
                st["last_exit_at"] = 0
                st["stop_reason"] = ""
                st["_sched"] = sched
                st["_live_mode"] = include_live
                cam_health[cam_id] = {"last_frame_at": time.time(), "fps": None,
                                      "stalled": False, "restart_attempted": False,
                                      "started_at": started}
        except Exception as e:
            if token.is_set() or _shutdown.is_set():
                break
            st["running"] = False
            st["last_error"] = str(e)
            st["stop_reason"] = "start_failed"
            log_event("error", f"ffmpeg không khởi động được: {e}", cam_id)
            print(f"[LiteNVR] ffmpeg lỗi khởi động cho {cam_id}: {e}", flush=True)
            if token.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
            continue

        log_event("info", "Bắt đầu luồng (ffmpeg)", cam_id)

        def _hint(line: str) -> str:
            low = line.lower()
            if _is_audio_au_warning(line):
                return ("Lỗi âm thanh RTSP (AU headers) — video vẫn chạy; nếu live vẫn lỗi, "
                        "đặt Âm thanh = Không trong cấu hình camera: " + line)
            if _is_hevc_source_warning(line):
                return ("Nguồn H.265 có tham số PPS/SPS/NAL không chuẩn (thường từ NVR/go2rtc) — "
                        "video có thể vẫn phát bình thường; nếu hình không lên, dùng luồng phụ "
                        "H.264 hoặc luồng H.264 từ NVR: " + line)
            if "400 bad request" in low or "server returned 400" in low:
                return ("Camera/NVR trả 400 Bad Request: sai kênh hoặc đường dẫn RTSP, hoặc luồng phụ "
                        "không tồn tại (Hik: 101/102 = kênh 1, 201/202 = kênh 2...): " + line)
            if ("server returned 404" in low or "404 not found" in low) and _go2rtc.get("running"):
                return ("Nguồn camera lỗi khi lấy qua restream go2rtc (sai URL/tài khoản hoặc cam đang bận): " + line)
            if "5xx" in low or "server returned 5" in low or "453" in low or "454" in low or "461" in low:
                return ("Camera từ chối kết nối RTSP (có thể đã đủ số phiên đồng thời — "
                        "kiểm tra Frigate/go2rtc/app hãng đang chiếm stream): " + line)
            if "401" in low or "unauthorized" in low or "authentication" in low:
                return "Sai tài khoản/mật khẩu RTSP hoặc thiếu xác thực: " + line
            if "connection refused" in low or "timed out" in low or "no route" in low:
                return "Không kết nối được tới camera (IP/port/mạng): " + line
            if "invalid data found" in low or "could not find codec" in low:
                return "Luồng RTSP không hợp lệ hoặc sai đường dẫn: " + line
            return line

        def _drain(p, cid, status, src_main, src_sub):
            warned = False
            audio_warned = False
            hevc_warned = False
            try:
                for line in p.stderr:
                    line = line.strip()
                    if not line or token.is_set():
                        continue
                    if _note_progress(line, cid):
                        continue
                    status["log"].append(line)
                    if _REPEATED_LOG_RE.match(line):
                        continue
                    low = line.lower()
                    if substream_in_use and _line_hits_source(line, src_sub):
                        _mark_stream_failure(cid, eff_info, "sub", "luồng phụ lỗi")
                    elif src_main.startswith(GO2RTC_RTSP) and _line_hits_source(line, src_main):
                        _mark_stream_failure(cid, eff_info, "restream", "restream lỗi")
                    if _is_audio_au_warning(line):
                        status["last_warning"] = _hint(line)
                        if not audio_warned:
                            audio_warned = True
                            log_event("warn", status["last_warning"], cid)
                        continue
                    if _is_hevc_source_warning(line):
                        # Nguồn có thể đã đổi codec trên cùng URL (NVR/go2rtc);
                        # bỏ cache để lần khởi động sau dò lại thay vì copy nhầm.
                        codec_cache.pop(cid, None)
                        status["last_warning"] = _hint(line)
                        if not hevc_warned:
                            hevc_warned = True
                            log_event("warn", status["last_warning"], cid)
                        continue
                    status["last_error"] = _hint(line)
                    if ("5xx" in low or "server returned 5" in low
                            or " 453 " in low or "453 " in low
                            or "500 internal" in low or "503 " in low or "461 " in low):
                        status["rate_limited"] = True
                    if not warned:
                        warned = True
                        log_event("error", status["last_error"], cid)
            except Exception:
                pass

        st["rate_limited"] = False
        drain = threading.Thread(target=_drain, args=(proc, cam_id, st, main_url, sub_url), daemon=True)
        drain.start()
        proc.wait()
        exit_code = proc.returncode
        drain.join(timeout=2)
        if not drain.is_alive() and proc.stderr:
            proc.stderr.close()
        _clear_hls_playlist(cam_id)
        with worker_lock:
            if ffmpeg_processes.get(cam_id) is proc:
                ffmpeg_processes.pop(cam_id, None)
            if worker_tokens.get(cam_id) is token:
                st["running"] = False
                st["last_exit_code"] = exit_code
                st["last_exit_at"] = int(time.time())
                st["stop_reason"] = "exited"
        restart_requested = bool(st.pop("_restart_requested", False))
        immediate_restart = bool(st.pop("_restart_immediate", False))
        if token.is_set() or _shutdown.is_set():
            st["stop_reason"] = "stopped"
            break
        if immediate_restart:
            st["stop_reason"] = "restart"
            log_event("info", "Khởi động lại luồng ngay để áp dụng chế độ live", cam_id)
            continue
        if not restart_requested and not st.get("last_error"):
            st["last_error"] = f"ffmpeg đã dừng (mã {exit_code}); kiểm tra kết nối/cấu hình camera"
        # Backoff:
        #  - vừa chạy ổn trong >10s: chờ ngắn rồi thử lại
        #  - camera trả 5XX (đủ phiên / chống spam → khoá tạm): chờ DÀI để nhả khoá
        #  - lỗi nhanh khác: chờ ngắn
        ran = time.time() - started
        if ran > 10:
            backoff = 5
            log_event("warn", f"Luồng dừng sau {int(ran)}s, thử lại sau {backoff}s", cam_id)
        elif st.get("rate_limited"):
            backoff = min(max(backoff * 2, 15), 120)
            log_event("warn", f"Camera từ chối kết nối (5XX), chờ {backoff}s để nhả khoá", cam_id)
        else:
            backoff = min(max(backoff, 2), 30)
            log_event("warn", f"Kết nối lỗi, thử lại sau {backoff}s", cam_id)
        if token.wait(backoff):
            break
    cleared = False
    with worker_lock:
        if worker_tokens.get(cam_id) is token:
            worker_tokens.pop(cam_id, None)
            st["running"] = False
            cleared = True
    if cleared:
        _clear_hls_playlist(cam_id)


def stop_camera_worker(cam_id: str):
    with worker_lock:
        token = worker_tokens.pop(cam_id, None)
        if token:
            token.set()
        proc = ffmpeg_processes.get(cam_id)
        if proc:
            _terminate_process(proc)
            ffmpeg_processes.pop(cam_id, None)
        st = cam_status.setdefault(cam_id, {})
        st["running"] = False
        st["stop_reason"] = "stopped"
        st["last_error"] = ""
        st["last_warning"] = ""


def _restart_camera_process(cam_id: str, proc, immediate: bool = False):
    with worker_lock:
        if ffmpeg_processes.get(cam_id) is proc:
            st = cam_status.setdefault(cam_id, {})
            st["_restart_requested"] = True
            st["_restart_immediate"] = bool(immediate)
            _terminate_process(proc)

def ensure_live(cam_id: str):
    """Gọi khi có request xem live: đánh dấu viewer và bật/khôi phục luồng nếu cần."""
    cam_info = CONFIG.get("cameras", {}).get(cam_id)
    if not cam_info:
        return
    mark_viewer(cam_id)
    st = cam_status.setdefault(cam_id, {})
    now = time.time()
    if schedule_record_mode(cam_info) == "off":
        if not st.get("running"):
            start_camera_worker(cam_id)
        return
    # Camera có ghi hình: nếu nhánh live đang tắt (tiết kiệm CPU) thì khởi động lại
    if st.get("_live_mode") is False and now - st.get("_mode_ts", 0) > 5:
        st["_mode_ts"] = now
        proc = ffmpeg_processes.get(cam_id)
        if proc and proc.poll() is None:
            threading.Thread(target=_restart_camera_process, args=(cam_id, proc, True), daemon=True).start()

def live_supervisor():
    """Bật/tắt nhánh live theo số người xem để tiết kiệm CPU/RAM."""
    while not _shutdown.is_set():
        try:
            now = time.time()
            low = disk_low()
            if low != _disk_paused["low"]:
                _disk_paused["low"] = low
                log_event("warn" if low else "info",
                          "Ổ đĩa gần đầy: tạm dừng ghi hình mới" if low
                          else "Ổ đĩa đã hồi phục: tiếp tục ghi hình")
            for cam_id, cam in list(CONFIG.get("cameras", {}).items()):
                st = cam_status.get(cam_id, {})
                sched = schedule_record_mode(cam)
                if low and st.get("running") and sched != "off":
                    log_event("warn", "Tạm dừng ghi hình do ổ đĩa/tmpfs gần đầy", cam_id)
                    stop_camera_worker(cam_id)
                    st["_sched"] = sched
                    continue
                # Lịch đổi trạng thái (vào/ra khung giờ) -> khởi động lại worker theo chế độ mới
                if st.get("_sched") != sched:
                    if st.get("running"):
                        log_event("info", f"Lịch ghi hình đổi sang '{sched}', áp dụng lại luồng", cam_id)
                        stop_camera_worker(cam_id)
                    if sched != "off" and not low:
                        start_camera_worker(cam_id)
                    st["_sched"] = sched
                    continue
                if sched == "off":
                    last = cam_viewers.get(cam_id, 0)
                    if viewers_active(cam_id) and not st.get("running"):
                        log_event("info", "Có người xem, khởi động live", cam_id)
                        start_camera_worker(cam_id)
                    elif st.get("running") and last and (now - last) > LIVE_IDLE_STOP_SEC:
                        log_event("info", f"Không có người xem {LIVE_IDLE_STOP_SEC}s, dừng live", cam_id)
                        stop_camera_worker(cam_id)
                elif st.get("running") and st.get("_live_mode") is not None:
                    desired = viewers_active(cam_id)
                    if desired != st.get("_live_mode") and now - st.get("_mode_ts", 0) > 5:
                        st["_mode_ts"] = now
                        log_event("info", "Đổi nhánh live theo người xem (tiết kiệm CPU)", cam_id)
                        proc = ffmpeg_processes.get(cam_id)
                        if proc and proc.poll() is None:
                            _restart_camera_process(cam_id, proc, immediate=True)
                # Hồi phục sau khi ổ đĩa hết đầy: khởi động lại ghi hình cho camera đang cần
                if not low and sched != "off" and not st.get("running"):
                    start_camera_worker(cam_id)
            disk = read_disk()
            thr = float(CONFIG.get("max_disk_percent", 85) or 0)
            if thr and disk["disk_percent"] > thr and now - _disk_warn["t"] > 600:
                _disk_warn["t"] = now
                log_event("warn", f"Ổ đĩa đã dùng {disk['disk_percent']}% (ngưỡng {thr}%): sẽ tự xoá video cũ nhất")
        except Exception as e:
            log_event("warn", f"live_supervisor: {e}")
        _shutdown.wait(5)

def _buffer_segment_start(fpath: str) -> Optional[float]:
    try:
        name = os.path.basename(fpath)[:-4]
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").timestamp()
    except Exception:
        return None

def _buffer_completed_files(cam_id: str) -> List[str]:
    cam_buf_dir = os.path.join(buffer_root(), cam_id)
    if not os.path.isdir(cam_buf_dir):
        return []
    files = sorted(
        [os.path.join(cam_buf_dir, f) for f in os.listdir(cam_buf_dir) if f.endswith(".mp4")],
        key=os.path.getmtime,
    )
    # Bỏ qua file cuối cùng vì FFmpeg đang ghi dở.
    return files[:-1] if len(files) > 1 else []

def _move_buffer_file(cam_id: str, fpath: str) -> bool:
    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    os.makedirs(cam_rec_dir, exist_ok=True)
    fname = os.path.basename(fpath)
    dest = os.path.join(cam_rec_dir, fname)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(fname)
        dest = os.path.join(cam_rec_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
    try:
        os.replace(fpath, dest)
        return True
    except OSError:
        pass
    tmp = dest + f".part-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copy2(fpath, tmp)
        os.replace(tmp, dest)
        os.remove(fpath)
        return True
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        log_event("warn", f"Không chuyển được đệm motion: {e}", cam_id)
        return False

def promote_motion_buffer(cam_id: str, since: Optional[float] = None) -> int:
    """Chuyển đoạn đệm đã ghi xong xuống ổ cứng.
    `since` giới hạn ở các đoạn giao với mốc cần giữ (dùng cho pre-roll sự kiện)."""
    moved = 0
    with _motion_buffer_lock:
        for fpath in reversed(_buffer_completed_files(cam_id)):
            start = _buffer_segment_start(fpath)
            if since is not None and start is not None \
                    and start + MOTION_BUFFER_SEGMENT_SEC <= since:
                break
            if _move_buffer_file(cam_id, fpath):
                moved += 1
    return moved

# Luồng quản lý bộ đệm cho chế độ "Chỉ lưu khi có chuyển động (motion)"
def motion_buffer_manager():
    while True:
        now = time.time()
        for cam_id, cam_info in list(CONFIG.get("cameras", {}).items()):
            if schedule_record_mode(cam_info) != "motion":
                continue
            is_motion_active = now <= motion_active_until.get(cam_id, 0)
            if is_motion_active:
                promote_motion_buffer(cam_id)
                continue
            with _motion_buffer_lock:
                for fpath in _buffer_completed_files(cam_id):
                    try:
                        if now - os.path.getmtime(fpath) > MOTION_BUFFER_KEEP_SEC:
                            os.remove(fpath)
                    except Exception:
                        pass
        time.sleep(2)

# Hàm tính tổng dung lượng thư mục lưu trữ (Bytes), có cache để tránh walk ổ cứng liên tục
_size_cache = {"t": 0.0, "v": 0}
def get_storage_dir_size(force: bool = False):
    now = time.time()
    if not force and now - _size_cache["t"] < 60:
        return _size_cache["v"]
    total_size = 0
    for root, _, files in os.walk(STORAGE_DIR):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass
    _size_cache["t"] = now
    _size_cache["v"] = total_size
    return total_size

def prune_thumbnail_cache(max_mb: int = THUMB_CACHE_MAX_MB) -> int:
    """Giữ cache thumbnail trên đĩa ở mức giới hạn, xoá ảnh cũ nhất trước."""
    limit = max(0, int(max_mb)) * 2 ** 20
    files = []
    total = 0
    for root, _, names in os.walk(THUMBS_DIR):
        for name in names:
            fp = os.path.join(root, name)
            try:
                size = os.path.getsize(fp)
                files.append((os.path.getmtime(fp), size, fp))
                total += size
            except OSError:
                pass
    if total <= limit:
        return 0
    target = int(limit * 0.8)
    removed = 0
    for _, size, fp in sorted(files):
        if total <= target:
            break
        try:
            os.remove(fp)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def release_recording_page_cache(min_age: float = 30, max_age: float = 7200) -> int:
    """Trả page cache của các segment đã ghi xong để cgroup/RAM không phình theo thời gian.
    Chỉ bỏ cache đọc; dữ liệu trên đĩa không đổi."""
    if not hasattr(os, "posix_fadvise"):
        return 0
    now = time.time()
    released = 0
    for cam_id in list(CONFIG.get("cameras", {})):
        cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
        if not os.path.isdir(cam_dir):
            continue
        try:
            names = os.listdir(cam_dir)
        except OSError:
            continue
        for fname in names:
            if not fname.endswith(".mp4"):
                continue
            fp = os.path.join(cam_dir, fname)
            try:
                age = now - os.path.getmtime(fp)
            except OSError:
                continue
            if age < min_age or age > max_age:
                continue
            try:
                fd = os.open(fp, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
                released += 1
            except OSError:
                pass
    return released


def cleanup_tmpfs():
    """Dọn rác tmpfs: HLS/buffer của camera đã xoá, file tạm và buffer sai chế độ."""
    now = time.time()
    shutil.rmtree(LEGACY_THUMBS_DIR, ignore_errors=True)
    cams = CONFIG.get("cameras", {})

    try:
        _prune_playback_hls()
        for name in list(os.listdir(HLS_DIR)):
            path = os.path.join(HLS_DIR, name)
            if not os.path.isdir(path):
                try:
                    if name.endswith((".tmp", ".part")) and now - os.path.getmtime(path) > 120:
                        os.remove(path)
                except OSError:
                    pass
                continue
            if name not in cams:
                shutil.rmtree(path, ignore_errors=True)
                continue
            st = cam_status.get(name, {})
            live = bool(st.get("running") or viewers_active(name))
            try:
                for fname in list(os.listdir(path)):
                    fp = os.path.join(path, fname)
                    try:
                        age = now - os.path.getmtime(fp)
                    except OSError:
                        continue
                    stale_tmp = fname.endswith((".tmp", ".part")) and age > 120
                    if not live and age > 120:
                        os.remove(fp)
                    elif stale_tmp:
                        os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass

    roots = {BUFFER_DIR, buffer_root()}
    for root_dir in roots:
        if not os.path.isdir(root_dir):
            continue
        try:
            names = list(os.listdir(root_dir))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root_dir, name)
            if not os.path.isdir(path):
                continue
            if name not in cams:
                shutil.rmtree(path, ignore_errors=True)
                continue
            if schedule_record_mode(cams[name]) == "motion":
                continue
            try:
                for fname in list(os.listdir(path)):
                    fp = os.path.join(path, fname)
                    try:
                        if now - os.path.getmtime(fp) > 30:
                            os.remove(fp)
                    except OSError:
                        pass
            except OSError:
                pass

    for folder in (SNAPSHOTS_DIR, RECORDINGS_DIR):
        try:
            for root, _, names in os.walk(folder):
                for name in names:
                    if ".part" not in name:
                        continue
                    fp = os.path.join(root, name)
                    try:
                        if now - os.path.getmtime(fp) > 3600:
                            os.remove(fp)
                    except OSError:
                        pass
        except OSError:
            pass


def release_malloc_memory():
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def ram_janitor():
    while not _shutdown.is_set():
        try:
            cleanup_tmpfs()
            release_recording_page_cache(min_age=30, max_age=600)
            release_malloc_memory()
        except Exception as e:
            log_event("warn", f"ram_janitor: {e}")
        _shutdown.wait(60)


# Bộ dọn dẹp ổ cứng thông minh (Theo số ngày + Giới hạn GB + Giới hạn % ổ cứng)
def cam_retention_days(cam_id: str) -> float:
    """Số ngày lưu riêng của camera (0/rỗng = dùng mức chung)."""
    c = CONFIG.get("cameras", {}).get(cam_id) or {}
    try:
        v = float(c.get("retention_days") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else float(CONFIG.get("retention_days", 7))


def run_cleanup_now():
    retention_days = float(CONFIG.get("retention_days", 7))
    max_storage_gb = float(CONFIG.get("max_storage_gb", 0))
    max_disk_percent = float(CONFIG.get("max_disk_percent", 85))
    deleted_count = 0
    locked = locked_paths()
    drive_pending = _drive_pending_paths()
    now = time.time()

    def _protected(fp: str) -> bool:
        try:
            rel = os.path.relpath(fp, RECORDINGS_DIR)
            if not rel.startswith(".."):
                if rel in locked or rel in drive_pending:
                    return True
                if now - os.path.getmtime(fp) < 60:
                    return True  # ffmpeg vừa ghi xong, tránh xoá segment đang mở
            return rel in locked
        except Exception:
            return True

    # 1. Xóa theo số ngày lưu trữ (mỗi camera có thể đặt số ngày riêng)
    for folder in [RECORDINGS_DIR, SNAPSHOTS_DIR]:
        for root, _, files in os.walk(folder):
            rel = os.path.relpath(root, folder)
            cam_id = "" if rel == "." else rel.split(os.sep)[0]
            days = cam_retention_days(cam_id) if cam_id else retention_days
            if days <= 0:
                continue
            cutoff = time.time() - (days * 86400)
            for file in files:
                filepath = os.path.join(root, file)
                try:
                    if os.path.getmtime(filepath) < cutoff and not _protected(filepath):
                        os.remove(filepath)
                        deleted_count += 1
                except Exception:
                    pass

    # 2. Xóa cuốn chiếu file cũ nhất nếu vượt quá giới hạn GB hoặc % ổ cứng
    all_media = []
    for folder in [RECORDINGS_DIR, SNAPSHOTS_DIR]:
        for root, _, files in os.walk(folder):
            for file in files:
                fp = os.path.join(root, file)
                try:
                    if _protected(fp):
                        continue
                    all_media.append((os.path.getmtime(fp), os.path.getsize(fp), fp))
                except Exception:
                    pass

    all_media.sort(key=lambda x: x[0])  # Sắp xếp từ cũ nhất đến mới nhất
    media_bytes = sum(size for _, size, _ in all_media)
    max_bytes = max_storage_gb * (1024 ** 3) if max_storage_gb > 0 else 0

    for mtime, fsize, fp in all_media:
        total, used, _ = shutil.disk_usage(STORAGE_DIR)
        disk_pct = (used / total) * 100 if total else 0
        over_gb = (max_bytes > 0 and media_bytes > max_bytes)
        over_pct = (max_disk_percent > 0 and disk_pct > max_disk_percent)

        if not (over_gb or over_pct):
            break
        try:
            os.remove(fp)
            media_bytes -= fsize
            deleted_count += 1
        except Exception:
            pass

    # 3. Hạn mức dung lượng riêng cho từng camera (max_gb): xoá file cũ nhất của riêng camera đó
    for cam_id, cam in list(CONFIG.get("cameras", {}).items()):
        try:
            quota = float(cam.get("max_gb") or 0)
        except (TypeError, ValueError):
            quota = 0
        if quota <= 0:
            continue
        cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
        if not os.path.isdir(cam_dir):
            continue
        files = []
        cam_bytes = 0
        for fname in os.listdir(cam_dir):
            if not fname.endswith(".mp4"):
                continue
            fp = os.path.join(cam_dir, fname)
            if _protected(fp):
                continue
            try:
                size = os.path.getsize(fp)
                files.append((os.path.getmtime(fp), size, fp))
                cam_bytes += size
            except Exception:
                pass
        quota_bytes = quota * (1024 ** 3)
        files.sort(key=lambda x: x[0])
        for mtime, fsize, fp in files:
            if cam_bytes <= quota_bytes:
                break
            try:
                os.remove(fp)
                cam_bytes -= fsize
                deleted_count += 1
            except Exception:
                pass

    # 4. Dọn sự kiện cũ trong CSDL (giữ lại sự kiện đã đánh dấu ★)
    try:
        conn = db_conn()
        now_ts = int(time.time())
        cams = CONFIG.get("cameras", {})
        for cid in list(cams.keys()):
            days = cam_retention_days(cid)
            if days <= 0:
                continue
            conn.execute("DELETE FROM events WHERE cam_id=? AND starred=0 AND timestamp < ?",
                         (cid, now_ts - int(days * 86400)))
        if retention_days > 0:
            cut = now_ts - int(retention_days * 86400)
            if cams:
                ph = ",".join("?" * len(cams))
                conn.execute(f"DELETE FROM events WHERE starred=0 AND cam_id NOT IN ({ph}) AND timestamp < ?",
                             (list(cams.keys()) + [cut]))
            else:
                conn.execute("DELETE FROM events WHERE starred=0 AND timestamp < ?", (cut,))
        conn.commit()
    except Exception as e:
        log_event("warn", f"cleanup events: {e}")

    deleted_count += prune_thumbnail_cache()
    deleted_count += prune_playback_cache()
    _size_cache["t"] = 0.0
    return deleted_count

def check_corrupt_recordings():
    """Phát hiện file ghi hỏng: 0 byte, hoặc không có header moov/ftyp.
    Tự xoá file 0 byte cũ (>60s) và ghi cảnh báo để người dùng biết."""
    removed = 0
    bad = []
    now = time.time()
    locked = locked_paths()
    for cam_id in list(CONFIG.get("cameras", {})):
        cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
        if not os.path.isdir(cam_dir):
            continue
        for fname in os.listdir(cam_dir):
            if not fname.endswith(".mp4"):
                continue
            if f"{cam_id}/{fname}" in locked:
                continue
            fp = os.path.join(cam_dir, fname)
            try:
                size = os.path.getsize(fp)
                mtime = os.path.getmtime(fp)
            except Exception:
                continue
            if size == 0:
                if now - mtime > 60:
                    try:
                        os.remove(fp)
                        removed += 1
                    except Exception:
                        pass
                continue
            if size < 4096:  # quá nhỏ để có header video
                bad.append(f"{cam_id}/{fname}")
                continue
            try:
                with open(fp, "rb") as f:
                    head = f.read(64)
                if b"moov" not in head and b"ftyp" not in head:
                    bad.append(f"{cam_id}/{fname}")
            except Exception:
                continue
    if removed:
        log_event("warn", f"Đã xoá {removed} file ghi rỗng (0 byte)")
    if bad:
        st = cam_status.setdefault("_system", {})
        if now - st.get("_corrupt_ts", 0) > 1800:
            st["_corrupt_ts"] = now
            log_event("warn", f"Phát hiện {len(bad)} file ghi nghi hỏng (thiếu header) — kiểm tra ổ đĩa/camera: " + ", ".join(bad[:5]))
    return removed, len(bad)

def cleanup_worker():
    while True:
        try:
            run_cleanup_now()
            check_corrupt_recordings()
            purge_sessions()
        except Exception:
            pass
        time.sleep(300)  # Kiểm tra dọn ổ cứng mỗi 5 phút

class CameraItem(BaseModel):
    cam_id: str
    name: str
    brand: str = "generic"
    rtsp_url: str
    sub_rtsp_url: Optional[str] = ""
    rtsp_transport: str = "tcp"
    audio_mode: str = "aac"
    record_mode: str = "continuous"  # continuous | motion | off
    video_codec: str = "auto"        # auto | copy | h264 (h264 = tương thích trình duyệt)
    storage_target: str = "both"     # hdd | drive | both — nơi lưu bản ghi
    storage_smb: Optional[bool] = None  # None = giữ lựa chọn hiện tại; True/False = chọn sao lưu ngoài
    storage_smb_move: bool = False   # Xoá bản gốc sau khi chuyển SMB (khi không giữ ổ gốc)
    room: str = ""                   # Nhóm/phòng để lọc camera
    tags: str = ""                   # Nhãn tự do, cách nhau dấu phẩy
    onvif_port: int = 80             # Cổng ONVIF/PTZ (nếu khác 80)
    retention_days: float = 0        # Số ngày lưu riêng (0 = dùng mức chung toàn hệ thống)
    max_gb: float = 0                # Hạn mức dung lượng riêng (0 = dùng mức chung toàn hệ thống)
    schedule_enabled: bool = False   # Bật lịch ghi hình theo giờ
    schedule: List[Dict[str, Any]] = []  # [{days:[0-6], from:"18:00", to:"06:00", mode:"motion"}]
    hass_entities: List[Any] = []        # Cảm biến HA: [{entity_id, state(kích hoạt), label}] hoặc chuỗi cũ

class StorageSettings(BaseModel):
    retention_days: float
    max_storage_gb: float
    max_disk_percent: float
    post_motion_sec: int
    ram_buffer: bool = True          # True: đệm motion trong RAM; False: ghi ra đĩa
    min_free_percent: float = 3      # Tạm dừng ghi hình khi ổ trống thấp hơn mức này

class EventStar(BaseModel):
    starred: bool = True

class NotifySettings(BaseModel):
    telegram_enabled: bool = False
    telegram_token: str = ""         # Để trống khi lưu = giữ token cũ
    telegram_chat_id: str = ""
    webhook_url: str = ""
    public_url: str = ""
    with_photo: bool = True
    telegram_clip: bool = False
    telegram_clip_sec: int = 8
    disk_alert_enabled: bool = False
    disk_warn_percent: float = 85
    disk_crit_percent: float = 95
    camera_offline_alert: bool = True

class AiSettings(BaseModel):
    enabled: bool = False
    provider: str = "gemini"
    api_key: str = ""                # Để trống khi lưu = giữ key cũ
    model: str = ""
    base_url: str = ""
    prompt: str = ""
    timeout: int = 30

class LicenseActivate(BaseModel):
    key: str = ""
    server_url: str = ""

class PtzCmd(BaseModel):
    action: str                      # up|down|left|right|upleft|upright|downleft|downright|zoomin|zoomout|home|stop|preset
    speed: float = 0.6               # 0..1
    duration: float = 0.5            # giây (ContinuousMove rồi Stop)
    preset: str = ""                 # token preset khi action=preset

class LockReq(BaseModel):
    cam_id: str
    file: str
    note: str = ""

class CameraTestReq(BaseModel):
    rtsp_url: str
    rtsp_transport: str = "tcp"

class OnvifProbeReq(BaseModel):
    host: str
    port: int = 80
    username: str = ""
    password: str = ""
    xaddr: str = ""

class OnvifDeviceReq(BaseModel):
    host: str
    port: int = 80
    xaddr: str = ""
    name: str = ""

class OnvifAutoAddReq(BaseModel):
    devices: List[OnvifDeviceReq] = []
    username: str = ""
    password: str = ""
    record_mode: str = "continuous"
    storage_target: str = "both"
    storage_smb: Optional[bool] = None
    storage_smb_move: bool = False
    room: str = ""
    tags: str = ""

class BulkCamerasReq(BaseModel):
    items: List[CameraItem]

class ClientLogReq(BaseModel):
    level: str = "warn"
    message: str
    cam_id: str = ""

class MotionPayload(BaseModel):
    cam_id: str
    label: str = "Chuyển động (HASS)"

class AiDetectPayload(BaseModel):
    cam_id: str
    objects: List[str] = []      # person, car, truck, ... (tự dịch sang tiếng Việt)
    label: str = ""              # ghi đè nhãn nếu muốn
    snapshot_url: str = ""       # ảnh AI gửi kèm (tùy chọn, http/https)

def _host_load():
    """Tải CPU + RAM của máy chủ (đọc /proc, không cần thư viện ngoài)."""
    load = []
    try:
        with open("/proc/loadavg") as f:
            load = [float(x) for x in f.read().split()[:3]]
    except Exception:
        pass
    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    info = {"load1": load[0] if load else None,
            "load5": load[1] if len(load) > 1 else None,
            "load15": load[2] if len(load) > 2 else None,
            "cpu_count": os.cpu_count() or 0}
    try:
        with open("/proc/uptime") as f:
            info["uptime_sec"] = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        info["uptime_sec"] = None
    if mem_total:
        info["ram_total_gb"] = round(mem_total / (1024**3), 2)
        info["ram_used_gb"] = round((mem_total - mem_avail) / (1024**3), 2)
        info["ram_percent"] = round(((mem_total - mem_avail) / mem_total) * 100, 1)
    return info

def _read_int_file(path: str):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None

def _proc_memory(pid) -> dict:
    values = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in ("VmRSS", "RssAnon", "RssFile", "RssShmem", "VmHWM", "Threads"):
                    values[key] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return values


def _proc_rss_kb(pid) -> int:
    return _proc_memory(pid).get("VmRSS", 0)

_dir_size_cache: Dict[str, tuple] = {}
_dir_size_lock = threading.Lock()

def _dir_size_bytes(d: str, max_age: float = 30.0) -> int:
    now = time.monotonic()
    with _dir_size_lock:
        cached = _dir_size_cache.get(d)
        if cached and now - cached[0] < max_age:
            return cached[1]
    total = 0
    for root, _, files in os.walk(d):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except Exception:
                pass
    with _dir_size_lock:
        _dir_size_cache[d] = (now, total)
    return total

def _mem_breakdown() -> dict:
    """Chẩn đoán RAM trong container: cgroup (gồm tmpfs/page cache), app, tiến trình con."""
    out = {}
    cands = ["/sys/fs/cgroup"]
    try:
        for line in open("/proc/self/cgroup").read().strip().splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[2] and parts[2] != "/":
                if parts[0] == "":  # cgroup v2
                    cands.append("/sys/fs/cgroup" + parts[2])
                elif "memory" in parts[1]:  # cgroup v1
                    cands.append("/sys/fs/cgroup/memory" + parts[2])
    except Exception:
        pass
    cur = mx = None
    stat = {}
    for d in cands:
        cur = _read_int_file(d + "/memory.current")
        if cur is None:
            cur = _read_int_file(d + "/memory.usage_in_bytes")
        if cur is None:
            continue
        mx = _read_int_file(d + "/memory.max")
        if mx is None:
            mx = _read_int_file(d + "/memory.limit_in_bytes")
        try:
            for line in open(d + "/memory.stat"):
                k, _, v = line.partition(" ")
                stat[k] = int(v)
        except Exception:
            stat = {}
        break
    if cur is not None:
        out["container_used_mb"] = round(cur / 2**20, 1)
        out["container_limit_mb"] = round(mx / 2**20, 1) if mx and mx > 0 and mx < 2**60 else 0
        out["cgroup_anon_mb"] = round(stat.get("anon", 0) / 2**20, 1)
        out["cgroup_file_mb"] = round(stat.get("file", 0) / 2**20, 1)
        out["cgroup_shmem_mb"] = round(stat.get("shmem", 0) / 2**20, 1)
    procs, total_child = [], 0
    with worker_lock:
        owners = {p.pid: ("camera", cid) for cid, p in ffmpeg_processes.items()}
    with _snapshot_lock:
        owners.update({p.pid: ("snapshot", cid) for cid, p in _snapshot_processes.items()})
    with _playback_lock:
        owners.update({pid: ("playback", item[1]) for pid, item in _playback_processes.items()})
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        uptime, ticks = 0.0, 100
    self_pid = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                raw = f.read()
            comm = raw.split("(", 1)[1].rsplit(")", 1)[0]
            fields = raw.rsplit(")", 1)[1].split()
            ppid = int(fields[1])
            age_sec = max(0.0, uptime - int(fields[19]) / ticks)
        except Exception:
            continue
        if ppid != self_pid:
            continue
        mem = _proc_memory(pid)
        rss = mem.get("VmRSS", 0)
        total_child += rss
        role, cam_id = owners.get(int(pid), ("other", ""))
        procs.append({"pid": int(pid), "name": comm, "rss_mb": round(rss / 1024, 1),
                      "anon_mb": round(mem.get("RssAnon", 0) / 1024, 1),
                      "peak_rss_mb": round(mem.get("VmHWM", 0) / 1024, 1),
                      "threads": mem.get("Threads", 0), "age_sec": round(age_sec, 1),
                      "role": role, "cam_id": cam_id})
    procs.sort(key=lambda x: -x["rss_mb"])
    app_mem = _proc_memory(self_pid)
    out["app_rss_mb"] = round(app_mem.get("VmRSS", 0) / 1024, 1)
    out["app_anon_mb"] = round(app_mem.get("RssAnon", 0) / 1024, 1)
    out["app_file_mb"] = round(app_mem.get("RssFile", 0) / 1024, 1)
    out["app_peak_mb"] = round(app_mem.get("VmHWM", 0) / 1024, 1)
    out["app_threads"] = app_mem.get("Threads", 0)
    with _notify_state_lock:
        out["notify_active"] = _notify_active
    out["notify_limit"] = NOTIFY_MAX_CONCURRENT
    out["children_mb"] = round(total_child / 1024, 1)
    out["children"] = procs[:12]
    out["hls_dir_mb"] = round(_dir_size_bytes(HLS_DIR) / 2**20, 1)
    out["buffer_dir_mb"] = round(_dir_size_bytes(buffer_root()) / 2**20, 1)
    out["thumb_dir_mb"] = round(_dir_size_bytes(THUMBS_DIR) / 2**20, 1)
    return out

@app.get("/api/disk_health")
def get_disk_health():
    return disk_health()

@app.get("/api/system")
def get_system_info():
    total, used, free = shutil.disk_usage(STORAGE_DIR)
    nvr_used_bytes = get_storage_dir_size()
    return {
        "version": APP_VERSION,
        "storage_ready": {
            "drive": _drive_storage_ready(),
            "smb": _smb_storage_ready(),
        },
        "retention_days": CONFIG.get("retention_days", 7),
        "max_storage_gb": CONFIG.get("max_storage_gb", 0),
        "max_disk_percent": CONFIG.get("max_disk_percent", 85),
        "min_free_percent": CONFIG.get("min_free_percent", 3),
        "recording_paused": bool(_disk_paused["low"] or disk_low()),
        "post_motion_sec": CONFIG.get("post_motion_sec", 60),
        "ram_buffer": bool(CONFIG.get("ram_buffer", True)),
        "disk_health": disk_health(),
        "cam_health": health_snapshot(),
        "live_idle_stop_sec": LIVE_IDLE_STOP_SEC,
        "live_max_height": LIVE_MAX_HEIGHT,
        "live_transcode_max_height": LIVE_TRANSCODE_MAX_HEIGHT,
        "go2rtc": {
            "mode": GO2RTC_MODE,
            "available": go2rtc_available(),
            "enabled": bool(_go2rtc.get("enabled")),
            "running": bool(_go2rtc.get("running")),
            "restream": GO2RTC_RTSP,
        },
        "host": _host_load(),
        "memory": _mem_breakdown(),
        "nvr_used_gb": round(nvr_used_bytes / (1024**3), 2),
        "disk_total_gb": round(total / (1024**3), 2),
        "disk_used_gb": round(used / (1024**3), 2),
        "disk_free_gb": round(free / (1024**3), 2),
        "disk_percent": round((used / total) * 100, 1) if total else 0
    }

@app.post("/api/storage")
def update_storage_settings(settings: StorageSettings):
    CONFIG["retention_days"] = settings.retention_days
    CONFIG["max_storage_gb"] = settings.max_storage_gb
    CONFIG["max_disk_percent"] = settings.max_disk_percent
    CONFIG["min_free_percent"] = max(0.0, float(settings.min_free_percent or 0))
    CONFIG["post_motion_sec"] = settings.post_motion_sec
    CONFIG["ram_buffer"] = settings.ram_buffer
    save_config(CONFIG)
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.post("/api/storage/clean")
def manual_clean():
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.get("/api/stats")
def api_stats():
    with _stats_lock:
        if latest_stats:
            return dict(latest_stats)
    return collect_stats()


@app.get("/api/stats/history")
def api_stats_history():
    with _stats_lock:
        return {"samples": list(stats_history)}

@app.get("/api/latest_snapshots")
def api_latest_snapshots(request: Request):
    """Ảnh snapshot mới nhất của mỗi camera (làm ảnh đại diện cho lưới camera)."""
    out: Dict[str, str] = {}
    try:
        c = db_conn().cursor()
        c.execute("""
            SELECT e.cam_id, e.snapshot FROM events e
            JOIN (SELECT cam_id, MAX(timestamp) AS mt FROM events
                  WHERE snapshot IS NOT NULL GROUP BY cam_id) m
              ON e.cam_id = m.cam_id AND e.timestamp = m.mt
        """)
        for cam_id, snap in c.fetchall():
            path = os.path.join(SNAPSHOTS_DIR, snap) if snap else ""
            if not _valid_snapshot(path):
                path = _latest_snapshot(cam_id)
            if path:
                out[cam_id] = f"snapshots/{os.path.basename(path)}"
    except Exception:
        pass
    u = getattr(request.state, "user", None) or {}
    if u.get("role") != "admin" and u.get("cameras"):
        out = {k: v for k, v in out.items() if k in u["cameras"]}
    return out

@app.get("/api/cameras/{cam_id}/snapshot.jpg")
async def api_camera_snapshot(cam_id: str, request: Request):
    if cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    assert_cam_access(request, cam_id)
    path = ""
    if not _go2rtc.get("running") and GO2RTC_MODE != "off":
        try:
            await asyncio.to_thread(start_go2rtc)
        except Exception:
            pass
    tag = hashlib.sha1(cam_id.encode("utf-8")).hexdigest()[:8]
    dest = os.path.join(MQTT_SNAPSHOT_DIR, f"ha_{_mqtt_slug(cam_id)}_{tag}.jpg")
    with _ha_snapshot_lock:
        try:
            if _go2rtc_snapshot(cam_id, dest):
                path = dest
        except Exception:
            path = ""
    if not path:
        path = _latest_snapshot(cam_id)
    if not _valid_snapshot(path):
        raise HTTPException(status_code=404,
                            detail="Chưa có ảnh snapshot; mở live hoặc chờ sự kiện chuyển động")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

@app.get("/api/cameras")
def get_cameras(request: Request):
    cameras = CONFIG.get("cameras", {})
    backup_cams = _backup_effective_cameras()
    u = getattr(request.state, "user", None) or {}
    if u.get("role") == "admin" or not AUTH_ENABLED:
        return {cid: {**dict(cam), "storage_smb": cid in backup_cams}
                for cid, cam in cameras.items()}
    out = {}
    for cam_id, cam in cameras.items():
        if u.get("cameras") and cam_id not in u["cameras"]:
            continue
        c = dict(cam)
        c["rtsp_url"] = mask_secrets(c.get("rtsp_url", ""))
        c["sub_rtsp_url"] = mask_secrets(c.get("sub_rtsp_url", ""))
        c["storage_smb"] = cam_id in backup_cams
        out[cam_id] = c
    return out

def _norm_cam_hass(item: CameraItem) -> dict:
    """Chuẩn hoá danh sách cảm biến Home Assistant của một camera."""
    seen = set()
    ents = []
    for ent in (item.hass_entities or []):
        e = _norm_hass_entity(ent)
        if e and e["entity_id"] not in seen:
            seen.add(e["entity_id"])
            ents.append(e)
    return {"hass_entities": ents}

def _camera_record(item: CameraItem) -> dict:
    """Chuẩn hoá dữ liệu camera trước khi ghi config (dùng chung cho thêm/sửa/bulk)."""
    return {
        "name": item.name,
        "brand": item.brand,
        "rtsp_url": item.rtsp_url.strip(),
        "sub_rtsp_url": (item.sub_rtsp_url or "").strip(),
        "rtsp_transport": item.rtsp_transport,
        "audio_mode": item.audio_mode,
        "record_mode": item.record_mode,
        "video_codec": item.video_codec,
        "storage_target": item.storage_target if item.storage_target in ("hdd", "drive", "both") else "both",
        "storage_smb_move": bool(item.storage_smb_move),
        "room": item.room,
        "tags": item.tags,
        "onvif_port": int(item.onvif_port or 80),
        "retention_days": max(0.0, float(item.retention_days or 0)),
        "max_gb": max(0.0, float(item.max_gb or 0)),
        "schedule_enabled": bool(item.schedule_enabled),
        "schedule": [w for w in (item.schedule or []) if isinstance(w, dict)],
        **_norm_cam_hass(item),
    }


def _camera_url_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def _onvif_existing_hosts() -> set:
    hosts = set()
    for cam in CONFIG.get("cameras", {}).values():
        for key in ("rtsp_url", "sub_rtsp_url"):
            host = _camera_url_host(cam.get(key, ""))
            if host:
                hosts.add(host)
    return hosts


def _onvif_unique_cam_id(name: str, host: str) -> str:
    raw = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    base = (base or host.replace(".", "_"))[:44].strip("_")
    candidate = "cam_" + base
    n = 2
    while candidate in CONFIG.get("cameras", {}):
        candidate = f"cam_{base}_{n}"
        n += 1
    return candidate


def _valid_cam_id(cam_id: str) -> bool:
    """Cho phép chữ/số/khoảng trắng tiếng Việt nhưng chặn path traversal và ký tự điều khiển."""
    if not cam_id or cam_id in (".", ".."):
        return False
    return not any(ch in cam_id for ch in ("/", "\\", "\x00", "'", '"', "<", ">", "`")) and not any(ord(ch) < 32 for ch in cam_id)

@app.post("/api/cameras")
def add_or_update_camera(item: CameraItem):
    cam_id = item.cam_id.strip()
    if not _valid_cam_id(cam_id):
        raise HTTPException(status_code=400, detail="Mã camera không hợp lệ (không dùng / \\ hoặc ký tự điều khiển)")
    stop_camera_worker(cam_id)
    CONFIG.setdefault("cameras", {})[cam_id] = _camera_record(item)
    if item.storage_smb is not None:
        _set_camera_smb(cam_id, item.storage_smb)
    save_config(CONFIG)
    refresh_go2rtc()
    start_camera_worker(cam_id)
    return {"status": "ok", "cameras": CONFIG["cameras"]}

@app.post("/api/cameras/bulk")
def bulk_add_cameras(req: BulkCamerasReq):
    """Thêm nhiều camera cùng lúc (cùng mẫu cấu hình)."""
    added = 0
    for item in req.items:
        cam_id = item.cam_id.strip()
        if not _valid_cam_id(cam_id):
            continue
        stop_camera_worker(cam_id)
        CONFIG.setdefault("cameras", {})[cam_id] = _camera_record(item)
        if item.storage_smb is not None:
            _set_camera_smb(cam_id, item.storage_smb)
        start_camera_worker(cam_id)
        added += 1
    save_config(CONFIG)
    refresh_go2rtc()
    log_event("info", f"Thêm hàng loạt {added} camera")
    return {"status": "ok", "added": added, "cameras": CONFIG["cameras"]}

@app.get("/api/onvif/discover")
def api_onvif_discover(timeout: float = 3.0):
    try:
        return {"devices": onvif_discover(timeout)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"WS-Discovery lỗi: {e}")

@app.get("/api/onvif/scan")
def api_onvif_scan(subnet: str, timeout: float = 0.8):
    """Quét unicast dải IP nội bộ cho camera ONVIF (dùng khi multicast bị chặn)."""
    try:
        return onvif_scan_network(subnet, timeout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Quét ONVIF lỗi: {e}")

@app.post("/api/onvif/auto_add")
def api_onvif_auto_add(req: OnvifAutoAddReq):
    """Tự lấy luồng và thêm các camera ONVIF đã chọn vào cấu hình."""
    devices = req.devices[:32]
    if not devices:
        raise HTTPException(status_code=400, detail="Chưa chọn camera nào")
    probed: List[Any] = [None] * len(devices)

    def probe_one(idx: int, dev: OnvifDeviceReq):
        try:
            probed[idx] = (dev, onvif_probe(dev.host, dev.port, req.username, req.password, dev.xaddr), "")
        except HTTPException as e:
            probed[idx] = (dev, None, str(e.detail))
        except Exception as e:
            probed[idx] = (dev, None, str(e))

    with ThreadPoolExecutor(max_workers=min(4, len(devices))) as ex:
        futures = [ex.submit(probe_one, i, dev) for i, dev in enumerate(devices)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass

    existing_hosts = _onvif_existing_hosts()
    added_ids = []
    results = []
    for dev, info, error in probed:
        host = (info or {}).get("host") or dev.host
        label = (info or {}).get("name") or dev.name or host
        if error:
            results.append({"status": "error", "host": host, "name": label, "error": error})
            continue
        if host.lower() in existing_hosts:
            results.append({"status": "skipped", "host": host, "name": label, "error": "Camera đã có trong danh sách"})
            continue
        main_url = (info or {}).get("main_rtsp_url") or ""
        if not main_url:
            results.append({"status": "error", "host": host, "name": label, "error": "Không lấy được URL RTSP chính"})
            continue
        cam_id = _onvif_unique_cam_id(label, host)
        item = CameraItem(
            cam_id=cam_id,
            name=label,
            brand=(info or {}).get("brand") or "onvif",
            rtsp_url=main_url,
            sub_rtsp_url=(info or {}).get("sub_rtsp_url") or "",
            onvif_port=int((info or {}).get("onvif_port") or dev.port or 80),
            record_mode=req.record_mode if req.record_mode in ("continuous", "motion", "off") else "continuous",
            storage_target=req.storage_target if req.storage_target in ("hdd", "drive", "both") else "both",
            storage_smb=req.storage_smb,
            storage_smb_move=req.storage_smb_move,
            room=req.room,
            tags=req.tags,
        )
        CONFIG.setdefault("cameras", {})[cam_id] = _camera_record(item)
        existing_hosts.add(host.lower())
        added_ids.append(cam_id)
        results.append({"status": "added", "host": host, "name": label, "cam_id": cam_id})

    if added_ids:
        if req.storage_smb is not None:
            for cam_id in added_ids:
                _set_camera_smb(cam_id, req.storage_smb)
        save_config(CONFIG)
        refresh_go2rtc()
        for cam_id in added_ids:
            start_camera_worker(cam_id)
        log_event("info", f"ONVIF tự thêm {len(added_ids)} camera")
    return {"status": "ok", "added": len(added_ids), "cam_ids": added_ids, "results": results}

@app.post("/api/onvif/probe")
def api_onvif_probe(req: OnvifProbeReq):
    try:
        return onvif_probe(req.host, req.port, req.username, req.password, req.xaddr)
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        try:
            body = (e.read() or b"").decode(errors="replace")
        except Exception:
            body = ""
        low = body.lower()
        if e.code in (401, 403) or any(k in low for k in (
                "not authorized", "invalid username", "invalidsecurity",
                "notauthorized", "unauthorized", "authentication")):
            raise HTTPException(status_code=401, detail="Sai tài khoản/mật khẩu ONVIF (hoặc camera chưa bật ONVIF)")
        texts = re.findall(r"<[^>]*Text[^>]*>([^<]*)<", body)
        reason = next((t.strip() for t in texts if t.strip()), "")
        raise HTTPException(status_code=502, detail=f"Camera ONVIF lỗi HTTP {e.code}" + (f": {reason}" if reason else ""))
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Không kết nối được camera ONVIF: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ONVIF probe lỗi: {e}")

@app.post("/api/cameras/test")
def test_camera_stream(req: CameraTestReq):
    """Kiểm tra nhanh URL camera bằng ffprobe (codec/độ phân giải/fps) trước khi lưu."""
    url = (req.rtsp_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Chưa nhập URL camera")
    if not re.match(r"^(rtsp|rtsps|http|https|rtmp|rtmps)://", url, re.I):
        raise HTTPException(status_code=400, detail="URL phải bắt đầu bằng rtsp://, http://, rtmp://...")
    transport = req.rtsp_transport if req.rtsp_transport in ("tcp", "udp") else "tcp"
    cmd = [
        "ffprobe", "-v", "error",
        *_input_args(url, {"rtsp_transport": transport}),
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,avg_frame_rate:format=duration,format_name",
        "-of", "json",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Quá thời gian 12s: camera không phản hồi hoặc sai URL")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Không tìm thấy ffprobe trên máy chủ")
    data = {}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {}
    streams = data.get("streams") or []
    info = streams[0] if streams else {}
    codec = (info.get("codec_name") or "").lower()
    if not codec:
        err = (proc.stderr or "").strip().splitlines()
        detail = mask_secrets(" | ".join(err[-3:])[:400]) if err else "Không đọc được luồng video"
        raise HTTPException(status_code=502, detail=f"Không mở được luồng: {detail}")
    pix_fmt = (info.get("pix_fmt") or "").lower()
    profile = (info.get("profile") or "").lower()
    fps = 0.0
    try:
        num, _, den = (info.get("avg_frame_rate") or "0/1").partition("/")
        fps = round(float(num) / float(den or 1), 1)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    browser_safe = _is_browser_safe_h264(codec, pix_fmt, profile)
    return {
        "ok": True,
        "codec": codec,
        "profile": profile,
        "pix_fmt": pix_fmt,
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": fps,
        "browser_safe": browser_safe,
        "format": (data.get("format") or {}).get("format_name", ""),
    }

@app.post("/api/live/{cam_id}/webrtc")
async def api_live_webrtc(cam_id: str, request: Request):
    if cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    assert_cam_access(request, cam_id)
    started = False
    if not _go2rtc.get("running") and GO2RTC_MODE != "off":
        await asyncio.to_thread(start_go2rtc)
        started = bool(_go2rtc.get("running"))
    if not _go2rtc.get("running"):
        raise HTTPException(status_code=503, detail="go2rtc chưa chạy; bật GO2RTC=on để xem WebRTC")
    if started and not await asyncio.to_thread(_wait_go2rtc_api, 5.0):
        raise HTTPException(status_code=503, detail="go2rtc đang khởi động, thử lại sau vài giây")
    offer = await request.body()
    if not offer or len(offer) > 2_000_000:
        raise HTTPException(status_code=400, detail="SDP offer không hợp lệ")
    cam_info = CONFIG["cameras"][cam_id]
    if not _restream_usable(cam_id, cam_info):
        raise HTTPException(status_code=503, detail="Restream go2rtc tạm lỗi; chuyển sang HLS trực tiếp")
    main_name = _g2_name(cam_id)
    sub_name = f"{main_name}_sub"
    name = _g2_live_name(cam_id, cam_info)

    def exchange(src_name: str):
        is_sub = src_name == sub_name and sub_name != main_name
        url = f"http://127.0.0.1:1984/api/webrtc?src={urllib.parse.quote(src_name)}"
        req = urllib.request.Request(
            url, data=offer, method="POST",
            headers={"Content-Type": "application/sdp", "User-Agent": "LiteNVR-WebRTC"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            if exc.code in (400, 404):
                _mark_stream_failure(cam_id, cam_info, "sub" if is_sub else "restream", f"go2rtc {exc.code}")
            log_event("warn", f"WebRTC go2rtc lỗi ({src_name}): {mask_secrets(detail)[:200]}", cam_id)
            raise HTTPException(status_code=502, detail=detail[:300] or f"go2rtc HTTP {exc.code}")
        except Exception as exc:
            log_event("warn", f"WebRTC không kết nối được go2rtc ({src_name}): {mask_secrets(str(exc))[:200]}", cam_id)
            raise HTTPException(status_code=502, detail=f"Không kết nối được go2rtc: {exc}")

    try:
        answer = await asyncio.to_thread(exchange, name)
    except HTTPException:
        if name == sub_name and sub_name != main_name \
                and main_name in _go2rtc_streams() and _restream_usable(cam_id, cam_info):
            log_event("warn", "WebRTC luồng phụ lỗi; thử lại bằng luồng chính", cam_id)
            name = main_name
            answer = await asyncio.to_thread(exchange, main_name)
        else:
            raise
    if not answer.startswith("v="):
        log_event("warn", f"WebRTC nhận SDP không hợp lệ ({name})", cam_id)
        raise HTTPException(status_code=502, detail="go2rtc trả về SDP không hợp lệ")
    host = webrtc_candidate_host(request)
    answer = inject_webrtc_candidates(answer, host, GO2RTC_WEBRTC_PORT)
    log_event("info", f"WebRTC kết nối nguồn {name}", cam_id)
    return Response(content=answer, media_type="application/sdp",
                    status_code=201,
                    headers={"Cache-Control": "no-store", "X-LiteNVR-WebRTC-Source": name})

@app.post("/api/live/{cam_id}")
def api_live_heartbeat(cam_id: str):
    if cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    ensure_live(cam_id)
    return {"status": "ok", "running": bool(cam_status.get(cam_id, {}).get("running"))}

@app.post("/api/client_log")
def api_client_log(req: ClientLogReq):
    """Nhận lỗi từ trình duyệt (HLS/playback) để hiện trong Nhật ký."""
    level = req.level if req.level in ("info", "warn", "error") else "warn"
    log_event(level, "UI: " + req.message[:500], req.cam_id)
    return {"status": "ok"}

@app.delete("/api/cameras/{cam_id}")
def delete_camera(cam_id: str):
    if cam_id in CONFIG.get("cameras", {}):
        stop_camera_worker(cam_id)
        del CONFIG["cameras"][cam_id]
        b = CONFIG.get("backup") or {}
        if b.get("cameras"):
            b["cameras"] = [c for c in b["cameras"] if c != cam_id]
        codec_cache.pop(cam_id, None)
        _clear_stream_failures(cam_id)
        cam_status.pop(cam_id, None)
        with _cam_offline_lock:
            _cam_offline.pop(cam_id, None)
        save_config(CONFIG)
        refresh_go2rtc()
    return {"status": "ok"}

# ---------------- Thông báo chuyển động (Telegram / Webhook) ----------------
_notify_last: Dict[str, float] = {}
_notify_lock = threading.Lock()
NOTIFY_MAX_CONCURRENT = max(1, int(os.environ.get("NOTIFY_MAX_CONCURRENT", "4") or "4"))
_notify_slots = threading.BoundedSemaphore(NOTIFY_MAX_CONCURRENT)
_notify_state_lock = threading.Lock()
_notify_active = 0
_notify_drop_count = 0
_notify_drop_log_at = 0.0
_disk_alert_state: Dict[str, Any] = {"level": "", "ts": 0.0}
_disk_alert_lock = threading.Lock()

def _normalize_ai_model(provider: str, model: Any) -> str:
    value = str(model or "").strip()
    if provider == "gemini" and value.startswith("models/"):
        value = value[7:].strip()
    return value

def _normalize_ai_base_url(raw: Any, provider: str, default_base: str) -> str:
    base = str(raw or default_base or "").strip().rstrip("/")
    if not base:
        return ""
    if provider == "gemini":
        base = re.sub(r"/models/[^/:]+:(?:streamGenerateContent|generateContent)$", "", base)
    else:
        if base.endswith("/chat/completions"):
            base = base[:-len("/chat/completions")].rstrip("/")
    return base

def _migrate_ai_model(provider: str, model: str, base_url: str, default_base: str) -> str:
    if base_url != str(default_base or "").rstrip("/"):
        return model
    return AI_RETIRED_MODELS.get(provider, {}).get(model, model)

def _ai_gemini_config(model: str, max_tokens: int) -> dict:
    config = {"maxOutputTokens": max_tokens}
    if re.match(r"^gemini-(?:3|4)", model):
        config["thinkingConfig"] = {"thinkingLevel": "low"}
    return config

def _ai_chat_payload(provider: str, model: str, content: Any, max_tokens: int) -> dict:
    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    if provider == "custom":
        payload["max_tokens"] = max_tokens
    else:
        payload["max_completion_tokens"] = max_tokens
    return payload

def _ai_settings() -> dict:
    ai_cfg = dict(DEFAULT_CONFIG["ai"])
    ai_cfg.update(CONFIG.get("ai") or {})
    provider = str(ai_cfg.get("provider") or "gemini").lower()
    if provider not in AI_PROVIDERS:
        provider = "gemini"
    label, default_model, default_base = AI_PROVIDERS[provider]
    ai_cfg["provider"] = provider
    ai_cfg["api_key"] = str(ai_cfg.get("api_key") or "").strip()
    ai_cfg["model"] = _normalize_ai_model(
        provider, ai_cfg.get("model") or default_model) or default_model
    ai_cfg["base_url"] = _normalize_ai_base_url(ai_cfg.get("base_url"), provider, default_base)
    ai_cfg["model"] = _migrate_ai_model(
        provider, ai_cfg["model"], ai_cfg["base_url"], default_base)
    ai_cfg["prompt"] = str(ai_cfg.get("prompt") or DEFAULT_CONFIG["ai"]["prompt"]).strip()
    try:
        ai_cfg["timeout"] = max(5, min(120, int(ai_cfg.get("timeout") or 30)))
    except (TypeError, ValueError):
        ai_cfg["timeout"] = 30
    return ai_cfg

def _ai_post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"LiteNVR/{APP_VERSION}",
            **headers,
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        raw = exc.read(64 * 1024)
        try:
            body = json.loads(raw)
            detail = body.get("error") if isinstance(body, dict) else body
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status") or detail
            detail = str(detail or "").strip()
        except Exception:
            detail = raw.decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {mask_secrets(detail[:300])}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Không kết nối được endpoint AI: {mask_secrets(str(exc))}") from None
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError("Endpoint AI trả về dữ liệu không phải JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("Endpoint AI trả về dữ liệu không hợp lệ")
    return data

def _ai_describe_snapshot(image_path: str, ai_cfg: Optional[dict] = None) -> dict:
    ai_cfg = ai_cfg or _ai_settings()
    if not ai_cfg.get("enabled"):
        raise RuntimeError("AI chưa được bật")
    provider = ai_cfg["provider"]
    if provider != "custom" and not ai_cfg.get("api_key"):
        raise RuntimeError("Chưa nhập API key AI")
    if not ai_cfg.get("model"):
        raise RuntimeError("Chưa nhập model AI")
    try:
        with open(image_path, "rb") as source:
            image = source.read(8 * 1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError(f"Không đọc được ảnh snapshot: {exc}") from None
    if not image:
        raise RuntimeError("Ảnh snapshot rỗng")
    if len(image) > 8 * 1024 * 1024:
        raise RuntimeError("Ảnh snapshot vượt quá 8 MB")
    b64 = base64.b64encode(image).decode("ascii")
    prompt = ai_cfg["prompt"]
    model = _normalize_ai_model(provider, ai_cfg["model"])
    default_base = AI_PROVIDERS[provider][2]
    base = _normalize_ai_base_url(ai_cfg["base_url"], provider, default_base)
    if provider == "gemini":
        url = f"{base}/models/{urllib.parse.quote(model, safe='')}:generateContent"
        data = _ai_post_json(url, {
            "contents": [{"role": "user", "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]}],
            "generationConfig": _ai_gemini_config(model, 2048),
        }, {"x-goog-api-key": ai_cfg["api_key"]}, ai_cfg["timeout"])
        candidates = data.get("candidates") or [{}]
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = ((candidate.get("content") or {}).get("parts") or [])
        text = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
        if not text:
            reason = candidate.get("finishReason") or (
                (data.get("promptFeedback") or {}).get("blockReason"))
            if reason:
                raise RuntimeError(f"AI không trả về mô tả (lý do: {reason})")
    else:
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("Base URL AI không hợp lệ")
        headers = {}
        if ai_cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {ai_cfg['api_key']}"
        data = _ai_post_json(f"{base}/chat/completions", _ai_chat_payload(provider, model, [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ], 2048), headers, ai_cfg["timeout"])
        choices = data.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = " ".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
        if not text and choice.get("finish_reason"):
            raise RuntimeError(f"AI không trả về mô tả (lý do: {choice['finish_reason']})")
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise RuntimeError("AI không trả về mô tả")
    return {"description": text[:1200], "model": model, "provider": provider}

def _ai_check_connection(ai_cfg: dict) -> dict:
    provider = ai_cfg["provider"]
    if provider != "custom" and not ai_cfg.get("api_key"):
        raise RuntimeError("Chưa nhập API key AI")
    if not ai_cfg.get("model"):
        raise RuntimeError("Chưa nhập model AI")
    model = _normalize_ai_model(provider, ai_cfg["model"])
    default_base = AI_PROVIDERS[provider][2]
    base = _normalize_ai_base_url(ai_cfg.get("base_url"), provider, default_base)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Base URL AI không hợp lệ")
    timeout = max(5, min(30, int(ai_cfg.get("timeout") or 30)))
    prompt = "Trả lời đúng một từ: OK"
    started = time.time()
    if provider == "gemini":
        data = _ai_post_json(
            f"{base}/models/{urllib.parse.quote(model, safe='')}:generateContent", {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": _ai_gemini_config(model, 1024),
            }, {"x-goog-api-key": ai_cfg["api_key"]}, timeout)
        candidates = data.get("candidates") or [{}]
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = ((candidate.get("content") or {}).get("parts") or [])
        message = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    else:
        headers = {}
        if ai_cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {ai_cfg['api_key']}"
        data = _ai_post_json(
            f"{base}/chat/completions",
            _ai_chat_payload(provider, model, prompt, 1024),
            headers, timeout)
        choices = data.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = (choice.get("message") or {}).get("content") or ""
        if isinstance(message, list):
            message = " ".join(str(p.get("text") or "") for p in message if isinstance(p, dict))
    message = re.sub(r"\s+", " ", str(message or "")).strip()
    return {
        "provider": provider,
        "model": model,
        "message": message[:120] or "Kết nối thành công",
        "latency_ms": int((time.time() - started) * 1000),
    }

def _wait_for_snapshot(snapshot_path: str, timeout: float) -> str:
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        if os.path.isfile(snapshot_path) and os.path.getsize(snapshot_path) > 0:
            return snapshot_path
        time.sleep(0.2)
    return ""

_license_lock = threading.Lock()
_license_gate_warn_at = 0.0

def _license_cfg() -> dict:
    lic = dict(DEFAULT_CONFIG["license"])
    lic.update(CONFIG.get("license") or {})
    lic["server_url"] = str(lic.get("server_url")
                          or DEFAULT_CONFIG["license"]["server_url"]).strip().rstrip("/")
    lic["key"] = str(lic.get("key") or "").strip()
    return lic

def _license_expiry() -> Optional[datetime]:
    raw = str(_license_cfg().get("expires_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def _license_valid() -> bool:
    lic = _license_cfg()
    if not lic.get("enforce", True):
        return True
    if not lic.get("key") or not lic.get("token"):
        return False
    expiry = _license_expiry()
    if expiry and expiry <= datetime.now(timezone.utc):
        return False
    last_check = float(lic.get("last_check") or 0)
    grace = float(lic.get("grace_days") or 7) * 86400
    if not last_check or time.time() - last_check > grace:
        return False
    return True

def _license_gate(feature: str) -> bool:
    global _license_gate_warn_at
    if _license_valid():
        return True
    now = time.time()
    if now - _license_gate_warn_at > 3600:
        _license_gate_warn_at = now
        log_event("warn", f"{feature} cần license LiteNVR Pro còn hiệu lực — vào Cài đặt → Bản quyền để kích hoạt")
    return False

def _license_server_call(action: str, payload: dict) -> dict:
    lic = _license_cfg()
    base = lic["server_url"]
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("License server URL không hợp lệ")
    url = f"{base}/api/license/{action}"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(128 * 1024)
    except urllib.error.HTTPError as exc:
        raw = exc.read(32 * 1024)
        try:
            detail = json.loads(raw).get("error") or json.loads(raw).get("detail")
        except Exception:
            detail = raw.decode("utf-8", "replace").strip()
        raise RuntimeError(str(detail or f"HTTP {exc.code}")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Không kết nối được license server: {exc}") from None
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError("License server trả về dữ liệu không hợp lệ") from None
    if not isinstance(data, dict):
        raise RuntimeError("License server trả về dữ liệu không hợp lệ")
    return data

def _license_apply(data: dict):
    lic = CONFIG.setdefault("license", dict(DEFAULT_CONFIG["license"]))
    lic["token"] = str(data.get("token") or "")
    lic["plan"] = str(data.get("plan") or "")
    lic["expires_at"] = str(data.get("expires_at") or "")
    lic["last_check"] = int(time.time())
    lic["last_error"] = ""
    lic["checked_version"] = APP_VERSION
    save_config(CONFIG)

def _license_activate(key: str) -> dict:
    key = key.strip()
    if not key:
        raise RuntimeError("Chưa nhập license key")
    lic = _license_cfg()
    data = _license_server_call("activate", {
        "key": key,
        "installation_id": lic["installation_id"],
        "version": APP_VERSION,
    })
    if not data.get("valid"):
        raise RuntimeError(str(data.get("error") or "License không hợp lệ"))
    with _license_lock:
        CONFIG.setdefault("license", dict(DEFAULT_CONFIG["license"]))["key"] = key
        _license_apply(data)
    log_event("info", f"Đã kích hoạt license {data.get('plan') or ''}".strip())
    return data

def _license_refresh(force: bool = False) -> bool:
    lic = _license_cfg()
    if not lic.get("key"):
        return False
    last_check = float(lic.get("last_check") or 0)
    if not force and time.time() - last_check < 6 * 3600:
        return _license_valid()
    try:
        data = _license_server_call("validate", {
            "key": lic["key"],
            "installation_id": lic["installation_id"],
            "version": APP_VERSION,
        })
        if not data.get("valid"):
            raise RuntimeError(str(data.get("error") or "License hết hạn"))
        with _license_lock:
            _license_apply(data)
        return True
    except Exception as exc:
        current = CONFIG.setdefault("license", dict(DEFAULT_CONFIG["license"]))
        current["last_error"] = mask_secrets(str(exc))
        save_config(CONFIG)
        return _license_valid()

def _license_status() -> dict:
    lic = _license_cfg()
    with _license_lock:
        cfg = _license_cfg()
    valid = _license_valid()
    reason = ""
    if not valid:
        if not cfg.get("enforce", True):
            reason = ""
        elif not cfg.get("key"):
            reason = "Chưa kích hoạt license"
        elif not cfg.get("token"):
            reason = "Chưa có token license"
        elif _license_expiry() and _license_expiry() <= datetime.now(timezone.utc):
            reason = "License đã hết hạn"
        elif float(cfg.get("last_check") or 0) and \
                time.time() - float(cfg.get("last_check") or 0) > float(cfg.get("grace_days") or 7) * 86400:
            reason = "Quá hạn kiểm tra license (ngoại tuyến quá lâu)"
        else:
            reason = "License không hợp lệ"
    return {
        "valid": valid,
        "enforce": bool(cfg.get("enforce", True)),
        "plan": cfg.get("plan", ""),
        "expires_at": cfg.get("expires_at", ""),
        "installation_id": cfg.get("installation_id", ""),
        "server_url": cfg.get("server_url", ""),
        "has_key": bool(cfg.get("key")),
        "last_check": int(cfg.get("last_check") or 0),
        "last_error": cfg.get("last_error", ""),
        "grace_days": int(cfg.get("grace_days") or 7),
        "reason": reason,
    }

def _license_worker():
    if _shutdown.wait(20):
        return
    while not _shutdown.is_set():
        try:
            _license_refresh()
        except Exception as exc:
            log_event("warn", f"Kiểm tra license lỗi: {mask_secrets(str(exc))}")
        if _shutdown.wait(6 * 3600):
            return

def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"

def _telegram_send(token: str, chat_id: str, text: str, photo_path: str = ""):
    """Gửi tin nhắn Telegram; có ảnh thì dùng sendPhoto (multipart)."""
    if photo_path and os.path.isfile(photo_path):
        boundary = "----LiteNVR" + uuid.uuid4().hex
        fields = {"chat_id": chat_id, "caption": text}
        parts = []
        for k, v in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        with open(photo_path, "rb") as f:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.jpg\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n".encode() + f.read() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(_telegram_api(token, "sendPhoto"), data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(_telegram_api(token, "sendMessage"), data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(300).decode("utf-8", "replace")

def _event_link(cam_id: str, when: datetime) -> str:
    """Link mở đúng camera + thời điểm sự kiện (nếu đã cấu hình public_url)."""
    base = (CONFIG.get("notify", {}).get("public_url") or "").strip().rstrip("/")
    if not base:
        return ""
    sec = when.hour * 3600 + when.minute * 60 + when.second
    return (f"{base}/?cam={urllib.parse.quote(cam_id)}"
            f"&date={when.strftime('%Y-%m-%d')}&t={sec}")


def _telegram_send_video(token: str, chat_id: str, text: str, video_path: str):
    if not (video_path and os.path.isfile(video_path)):
        return
    boundary = "----LiteNVR" + uuid.uuid4().hex
    parts = []
    for key, value in {"chat_id": chat_id, "caption": text, "supports_streaming": "true"}.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode())
    with open(video_path, "rb") as source:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; "
            f"filename=\"clip.mp4\"\r\nContent-Type: video/mp4\r\n\r\n".encode()
            + source.read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        _telegram_api(token, "sendVideo"), data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read(300).decode("utf-8", "replace")

def _capture_event_clip(cam_id: str, seconds: int) -> str:
    cam_info = CONFIG.get("cameras", {}).get(cam_id)
    if not cam_info or not shutil.which("ffmpeg"):
        return ""
    seconds = max(3, min(20, int(seconds or 8)))
    path = os.path.join("/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir(),
                        f"litenvr_clip_{uuid.uuid4().hex[:10]}.mp4")
    proc = None
    try:
        _, url = cam_source_urls(cam_id, cam_info)
        cmd = [*_ffmpeg_base_args(), "-y", *_input_args(url, cam_info), "-i", url,
               "-t", str(seconds), "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=seconds + 25)
        if proc.returncode or not os.path.isfile(path) or os.path.getsize(path) < 1024:
            raise RuntimeError("ffmpeg không tạo được clip")
        return path
    except Exception as exc:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        log_event("warn", f"Không tạo được clip Telegram: {mask_secrets(str(exc))}", cam_id)
        return ""
    finally:
        if proc and proc.poll() is None:
            _terminate_process(proc)

def notify_motion(cam_id: str, label: str, snapshot_path: str = "",
                  event_id: Optional[int] = None, event_ts: Optional[int] = None):
    """Mô tả AI + gửi thông báo Telegram/webhook (chạy trong thread riêng)."""
    cfg = CONFIG.get("notify") or {}
    ai_cfg = _ai_settings()
    tg_configured = bool(cfg.get("telegram_enabled") and cfg.get("telegram_token")
                         and cfg.get("telegram_chat_id"))
    needs_license = tg_configured or bool(ai_cfg.get("enabled"))
    licensed = _license_gate("Thông báo Telegram/Mô tả AI") if needs_license else True
    tg_on = bool(tg_configured and licensed)
    wh = (cfg.get("webhook_url") or "").strip()
    ai_on = bool(ai_cfg.get("enabled") and licensed)
    if not tg_on and not wh and not ai_on:
        return
    now = time.time()
    with _notify_lock:
        if now - _notify_last.get(cam_id, 0) < 3:
            return
        _notify_last[cam_id] = now

    cam_name = (CONFIG.get("cameras", {}).get(cam_id) or {}).get("name", cam_id)
    when = datetime.fromtimestamp(event_ts) if event_ts else datetime.now()
    link = _event_link(cam_id, when)
    need_snapshot = ai_on or (tg_on and cfg.get("with_photo", True))
    photo = _wait_for_snapshot(snapshot_path, 5) if (snapshot_path and need_snapshot) else ""
    description = ""
    if ai_on:
        path = photo or (snapshot_path if os.path.isfile(snapshot_path) else "")
        if path:
            try:
                result = _ai_describe_snapshot(path, ai_cfg)
                description = result["description"]
                if event_id:
                    conn = db_conn()
                    conn.execute(
                        "UPDATE events SET ai_description=?, ai_model=?, ai_ts=? WHERE id=?",
                        (description, result["model"], int(time.time()), event_id))
                    conn.commit()
                log_event("info", f"Mô tả AI: {description}", cam_id)
            except Exception as exc:
                log_event("warn", f"Mô tả AI lỗi: {mask_secrets(str(exc))}", cam_id)
        else:
            log_event("warn", "Bỏ qua mô tả AI vì chưa có snapshot", cam_id)

    text = f"🚨 {cam_name}: {label} lúc {when.strftime('%H:%M:%S %d/%m/%Y')}"
    if description:
        text += f"\n{description}"
    if link:
        text += f"\n▶️ Xem lại: {link}"

    if wh:
        try:
            payload = {"cam_id": cam_id, "name": cam_name, "label": label,
                       "time": when.isoformat(timespec="seconds")}
            if event_id:
                payload["event_id"] = event_id
            if description:
                payload["ai_description"] = description
            if link:
                payload["link"] = link
            data = json.dumps(payload).encode()
            req = urllib.request.Request(wh, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log_event("warn", f"Webhook thông báo lỗi: {e}", cam_id)

    if tg_on:
        clip = ""
        tg_text = text[:1020] + ("…" if len(text) > 1020 else "")
        try:
            if cfg.get("telegram_clip") and cam_id in CONFIG.get("cameras", {}):
                clip = _capture_event_clip(cam_id, int(cfg.get("telegram_clip_sec") or 8))
            if clip:
                _telegram_send_video(cfg["telegram_token"], str(cfg["telegram_chat_id"]), tg_text, clip)
            else:
                _telegram_send(cfg["telegram_token"], str(cfg["telegram_chat_id"]), tg_text,
                               photo if cfg.get("with_photo", True) else "")
        except Exception as e:
            log_event("warn", f"Telegram thông báo lỗi: {e}", cam_id)
        finally:
            if clip and os.path.isfile(clip):
                try:
                    os.remove(clip)
                except OSError:
                    pass


def _start_notify_thread(cam_id: str, label: str, snapshot_path: str,
                         event_id: Optional[int], event_ts: Optional[int]) -> bool:
    global _notify_active, _notify_drop_count, _notify_drop_log_at
    if not _notify_slots.acquire(blocking=False):
        with _notify_state_lock:
            _notify_drop_count += 1
            now = time.time()
            if now - _notify_drop_log_at >= 60:
                dropped = _notify_drop_count
                _notify_drop_count = 0
                _notify_drop_log_at = now
                log_event("warn", f"Bỏ qua {dropped} thông báo do quá tải "
                                  f"(tối đa {NOTIFY_MAX_CONCURRENT} luồng cùng lúc)")
        return False
    with _notify_state_lock:
        _notify_active += 1

    def run():
        global _notify_active
        try:
            notify_motion(cam_id, label, snapshot_path, event_id, event_ts)
        finally:
            _notify_slots.release()
            with _notify_state_lock:
                _notify_active = max(0, _notify_active - 1)

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _notify_slots.release()
        with _notify_state_lock:
            _notify_active = max(0, _notify_active - 1)
        return False
    return True


def send_alert(text: str):
    """Gửi cảnh báo hệ thống (ổ đĩa...) qua Telegram/webhook đã cấu hình."""
    cfg = CONFIG.get("notify") or {}
    wh = (cfg.get("webhook_url") or "").strip()
    if wh:
        try:
            data = json.dumps({"type": "alert", "text": text,
                               "time": datetime.now().isoformat(timespec="seconds")}).encode()
            req = urllib.request.Request(wh, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log_event("warn", f"Webhook cảnh báo lỗi: {e}")
    if cfg.get("telegram_enabled") and cfg.get("telegram_token") and cfg.get("telegram_chat_id"):
        try:
            _telegram_send(cfg["telegram_token"], str(cfg["telegram_chat_id"]), text)
        except Exception as e:
            log_event("warn", f"Telegram cảnh báo lỗi: {e}")


def disk_alert_level(pct: float, warn: float, crit: float) -> str:
    """Mức cảnh báo theo % đã dùng: '' | 'warn' | 'crit'."""
    return "crit" if pct >= crit else ("warn" if pct >= warn else "")


def disk_alert_supervisor():
    """Cảnh báo khi ổ đĩa vượt ngưỡng; báo lại tối đa mỗi 6 giờ khi vẫn vượt."""
    while not _shutdown.is_set():
        try:
            cfg = CONFIG.get("notify") or {}
            if cfg.get("disk_alert_enabled"):
                h = disk_health()
                pct = float(h.get("percent", 0) or 0)
                try:
                    warn = float(cfg.get("disk_warn_percent", 85) or 85)
                    crit = float(cfg.get("disk_crit_percent", 95) or 95)
                except (TypeError, ValueError):
                    warn, crit = 85.0, 95.0
                level = disk_alert_level(pct, warn, crit)
                with _disk_alert_lock:
                    prev, last = _disk_alert_state["level"], _disk_alert_state["ts"]
                if level and (level != prev or time.time() - last > 6 * 3600):
                    state = "nguy hiểm" if level == "crit" else "sắp đầy"
                    send_alert(f"⚠️ LiteNVR: ổ đĩa {state} — đã dùng {pct}% "
                               f"(còn {h.get('free_gb', '?')} GB trống)")
                    with _disk_alert_lock:
                        _disk_alert_state["level"] = level
                        _disk_alert_state["ts"] = time.time()
                    log_event("warn", f"Ổ đĩa {state}: {pct}%")
                elif not level:
                    with _disk_alert_lock:
                        _disk_alert_state["level"] = ""
        except Exception as e:
            log_event("warn", f"disk_alert_supervisor: {mask_secrets(str(e))}")
        _shutdown.wait(120)


def _valid_snapshot(path: str) -> bool:
    """Chỉ coi snapshot là hợp lệ khi file đã ghi xong và có dữ liệu."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False

def _snapshot_owner(filename: str) -> str:
    """Suy ra cam_id của ảnh snapshot để kiểm tra quyền xem theo camera."""
    name = os.path.basename(filename or "")
    if not name:
        return ""
    try:
        row = db_conn().execute(
            "SELECT cam_id FROM events WHERE snapshot=? ORDER BY timestamp DESC LIMIT 1",
            (name,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    for cid in CONFIG.get("cameras", {}):
        if name.startswith(cid + "_"):
            return cid
    return ""

def _latest_snapshot(cam_id: str) -> str:
    """Ảnh snapshot hợp lệ mới nhất của camera (dùng làm ảnh dự phòng cho sự kiện)."""
    try:
        found = []
        for name in os.listdir(SNAPSHOTS_DIR):
            if not (name.startswith(cam_id + "_") and name.endswith(".jpg")):
                continue
            path = os.path.join(SNAPSHOTS_DIR, name)
            if _valid_snapshot(path):
                found.append((os.path.getmtime(path), path))
        return max(found)[1] if found else ""
    except OSError:
        return ""

def _fetch_snapshot(url: str, dest: str, timeout: float = 8,
                    max_bytes: int = 8 * 1024 * 1024, label: str = "snapshot") -> bool:
    """Tải ảnh về file tạm rồi đổi tên; không để lại file dở dang."""
    tmp = f"{dest}.part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LiteNVR"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            data = r.read(max_bytes + 1)
            if not data or len(data) > max_bytes:
                return False
            f.write(data)
        if not _valid_snapshot(tmp):
            return False
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log_event("warn", f"Không tải được ảnh {label}: {mask_secrets(str(e))}")
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

def _go2rtc_snapshot(cam_id: str, dest: str) -> bool:
    """Lấy frame tức thời từ go2rtc nội bộ (nhanh, không mở thêm phiên RTSP)."""
    if not _go2rtc.get("running"):
        return False
    name = _g2_name(cam_id)
    if name not in _go2rtc_streams():
        return False
    url = f"http://{GO2RTC_API}/api/frame.jpeg?src={urllib.parse.quote(name, safe='')}"
    return _fetch_snapshot(url, dest, timeout=3, label="go2rtc")

def _start_snapshot(cam_id: str, cam_info: dict, snap_path: str,
                    event_id: Optional[int] = None) -> bool:
    with _snapshot_lock:
        if _shutdown.is_set() or cam_id in _snapshot_active:
            return False
        if not _snapshot_slots.acquire(blocking=False):
            return False
        _snapshot_active[cam_id] = snap_path
    try:
        threading.Thread(target=_capture_snapshot,
                         args=(cam_id, dict(cam_info), snap_path, event_id), daemon=True).start()
    except Exception:
        with _snapshot_lock:
            _snapshot_active.pop(cam_id, None)
        _snapshot_slots.release()
        return False
    return True


def _capture_snapshot(cam_id: str, cam_info: dict, snap_path: str,
                      event_id: Optional[int] = None):
    proc = None
    tmp_path = f"{snap_path}.part"
    try:
        _, snap_url = cam_source_urls(cam_id, cam_info)
        cmd = [*_ffmpeg_base_args(), "-y", *_input_args(snap_url, cam_info),
               "-i", snap_url, "-frames:v", "1", "-threads", str(FFMPEG_THREADS),
               "-q:v", "3", tmp_path]
        with _snapshot_lock:
            if _shutdown.is_set():
                return
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _snapshot_processes[cam_id] = proc
        proc.wait(timeout=12)
        if proc.returncode or not _valid_snapshot(tmp_path):
            log_event("warn", "Không chụp được snapshot", cam_id)
            return
        os.replace(tmp_path, snap_path)
        if event_id is not None:
            conn = db_conn()
            conn.execute("UPDATE events SET snapshot=? WHERE id=?",
                         (os.path.basename(snap_path), event_id))
            conn.commit()
        _mqtt_publish_snapshot_path(cam_id, snap_path)
    except subprocess.TimeoutExpired:
        log_event("warn", "Chụp snapshot quá thời gian, đã dừng ffmpeg", cam_id)
    except Exception as e:
        log_event("warn", f"Không chụp được snapshot: {mask_secrets(str(e))}", cam_id)
    finally:
        try:
            if proc:
                _terminate_process(proc)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            with _snapshot_lock:
                _snapshot_processes.pop(cam_id, None)
                _snapshot_active.pop(cam_id, None)
            _snapshot_slots.release()


AI_LABEL_VI = {
    "person": "Người", "face": "Khuôn mặt", "car": "Xe", "truck": "Xe tải",
    "bus": "Xe buýt", "motorcycle": "Xe máy", "bicycle": "Xe đạp",
    "dog": "Chó", "cat": "Mèo", "bird": "Chim", "animal": "Động vật",
    "package": "Bưu kiện", "license_plate": "Biển số",
}


def ai_event_label(objects: List[str], custom: str = "") -> str:
    """Nhãn sự kiện từ danh sách đối tượng AI (person, car, ...) sang tiếng Việt."""
    if custom.strip():
        return custom.strip()
    names, seen = [], set()
    for o in objects or []:
        key = str(o).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(AI_LABEL_VI.get(key, str(o).strip()))
    if not names:
        return "AI: đối tượng"
    return "AI: " + ", ".join(names)


def _safe_remote_url(url: str) -> bool:
    """Chặn SSRF vào loopback/link-local/metadata; vẫn cho phép thiết bị LAN RFC1918."""
    try:
        u = urllib.parse.urlsplit(url or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                    type=socket.SOCK_STREAM)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False
        return True
    except Exception:
        return False

def _download_snapshot(url: str, dest: str, max_bytes: int = 8 * 1024 * 1024) -> bool:
    """Tải ảnh snapshot từ URL (http/https) về dest. Trả True nếu thành công."""
    if not _safe_remote_url(url):
        return False
    return _fetch_snapshot(url, dest, max_bytes=max_bytes, label="AI")


def handle_motion(cam_id: str, label: str, snap_url: str = ""):
    """Ghi nhận 1 sự kiện chuyển động: kích hoạt ghi hình + chụp snapshot + lưu event."""
    cam_info = CONFIG.get("cameras", {}).get(cam_id)
    if not cam_info:
        return None

    now = datetime.now()
    ts = int(now.timestamp())
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    snap_filename = f"{cam_id}_{ts}_{uuid.uuid4().hex[:8]}.jpg"
    snap_path = os.path.join(SNAPSHOTS_DIR, snap_filename)

    # Kích hoạt cờ lưu video từ RAM xuống ổ cứng nếu camera đang ở chế độ Motion Only.
    # Chuyển ngay các đoạn đệm giao với 10 giây trước sự kiện để clip không bị mất đầu.
    post_sec = int(CONFIG.get("post_motion_sec", 60))
    motion_active_until[cam_id] = time.time() + post_sec
    try:
        promote_motion_buffer(cam_id, ts - MOTION_PREROLL_SEC)
    except Exception as e:
        log_event("warn", f"Không giữ được pre-roll motion: {e}", cam_id)
    log_event("info", f"Phát hiện chuyển động: {label}", cam_id)

    snapshot_ready = False
    if snap_url and _download_snapshot(snap_url, snap_path):
        snapshot_ready = True
    elif _go2rtc_snapshot(cam_id, snap_path):
        snapshot_ready = True

    # Sự kiện chỉ trỏ tới ảnh đã có sẵn; ảnh mới được chụp nền và cập nhật sau.
    initial_path = snap_path if snapshot_ready else _latest_snapshot(cam_id)
    initial_name = os.path.basename(initial_path) if _valid_snapshot(initial_path) else None

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (cam_id, ts, date_str, time_str, label, initial_name)
    )
    conn.commit()
    event_id = c.lastrowid
    if not snapshot_ready:
        _start_snapshot(cam_id, cam_info, snap_path, event_id)
    notify_path = initial_path if initial_name else snap_path
    _start_notify_thread(cam_id, label, notify_path, event_id, ts)
    _mqtt_publish_snapshot_path(cam_id, initial_path if initial_name else "")
    return {
        "status": "ok",
        "timestamp": ts,
        "record_mode": cam_info.get("record_mode", "continuous"),
        "pre_roll_sec": MOTION_PREROLL_SEC,
        "recording_until": motion_active_until[cam_id]
    }


@app.post("/api/motion")
def trigger_motion(payload: MotionPayload, request: Request):
    assert_cam_access(request, payload.cam_id)
    result = handle_motion(payload.cam_id, payload.label)
    if result is None:
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    return result

@app.post("/api/ai/detect")
def trigger_ai_detect(payload: AiDetectPayload, request: Request):
    """Nhận sự kiện từ bộ nhận diện AI bên ngoài (Frigate, DeepStack, HA...) và
    ghi thành sự kiện có nhãn tiếng Việt (Người, Xe...)."""
    assert_cam_access(request, payload.cam_id)
    label = ai_event_label(payload.objects, payload.label)
    result = handle_motion(payload.cam_id, label, payload.snapshot_url)
    if result is None:
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    return result

# Cấu hình liên kết Home Assistant
class HassRule(BaseModel):
    entity_id: str
    label: str = ""            # Nhãn sự kiện, VD: "Người quen", "Người lạ", tên người

class MqttSettings(BaseModel):
    enabled: bool = False
    host: str = ""
    base_url: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    discovery_prefix: str = "homeassistant"
    base_topic: str = "litenvr"
    snapshot_interval: int = 15

class HassSettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    token: str = ""            # Để trống khi lưu = giữ token cũ
    entities: Dict[str, List[HassRule]] = {}
    mqtt: Optional[MqttSettings] = None

def _hass_rules(entities: Dict[str, Any] = None) -> Dict[str, dict]:
    """Bảng tra entity_id -> {cam_id, state, label}. Gộp cảm biến gán riêng cho camera
    (`hass_entities`) với cấu hình cũ `homeassistant.entities` (để di trú)."""
    watch: Dict[str, dict] = {}
    for cam_id, ent_list in (entities or {}).items():
        for ent in (ent_list or []):
            e = _norm_hass_entity(ent)
            if e:
                watch[e["entity_id"]] = {"cam_id": cam_id, "state": e["state"], "label": e["label"]}
    for cam_id, cam in (CONFIG.get("cameras", {}) or {}).items():
        for ent in ((cam or {}).get("hass_entities") or []):
            e = _norm_hass_entity(ent)
            if e:
                watch[e["entity_id"]] = {"cam_id": cam_id, "state": e["state"], "label": e["label"]}
    return watch

# Trạng thái kết nối HA để hiển thị lên UI
hass_state = {"connected": False, "last_error": "", "last_event": "", "last_event_at": 0}

def _hass_default_headers(token: str):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def hass_ping(base_url: str, token: str):
    """Gọi REST /api/ để kiểm tra URL + token HA."""
    url = base_url.strip().rstrip("/") + "/api/"
    req = urllib.request.Request(url, headers=_hass_default_headers(token))
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

def _hass_ws_url(base_url: str):
    base = base_url.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not (base.startswith("ws://") or base.startswith("wss://")):
        base = "ws://" + base
    return base + "/api/websocket"

def hass_motion_listener():
    """Kết nối WebSocket tới HA, lắng nghe state_changed của các entity đã gán và
    kích hoạt ghi hình khi cảm biến đổi trạng thái (bất kỳ giá trị nào)."""
    try:
        import websocket  # websocket-client
    except Exception as e:
        print(f"[LiteNVR] Thiếu thư viện websocket-client, tắt liên kết HA: {e}", flush=True)
        return

    ws = None
    last_sig = None
    backoff = 5

    def close_ws():
        nonlocal ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            ws = None

    while True:
        ha = CONFIG.get("homeassistant", {}) or {}
        enabled = bool(ha.get("enabled"))
        base_url = (ha.get("base_url") or "").strip()
        token = (ha.get("token") or "").strip()
        entities: Dict[str, list] = ha.get("entities") or {}

        if not enabled or not base_url or not token:
            close_ws()
            last_sig = None
            hass_state["connected"] = False
            time.sleep(3)
            continue

        # Bảng tra: entity_id -> {cam_id, state, label}
        watch = _hass_rules(entities)

        sig = (base_url, token,
               tuple(sorted((eid, v["cam_id"], v["state"], v["label"]) for eid, v in watch.items())))
        if ws is not None and sig != last_sig:
            close_ws()

        if ws is None:
            try:
                ws = websocket.create_connection(_hass_ws_url(base_url), timeout=15)
                ws.recv()  # auth_required
                ws.send(json.dumps({"type": "auth", "access_token": token}))
                resp = json.loads(ws.recv())
                if resp.get("type") != "auth_ok":
                    raise RuntimeError(f"Xác thực HA thất bại: {resp.get('type')}")
                ws.send(json.dumps({
                    "id": 1, "type": "subscribe_events",
                    "event_type": "state_changed"
                }))
                ws.recv()  # ack subscribe
                last_sig = sig
                backoff = 5
                hass_state["connected"] = True
                hass_state["last_error"] = ""
                print("[LiteNVR] Đã kết nối Home Assistant, đang lắng nghe chuyển động.", flush=True)
                log_event("info", "Đã kết nối Home Assistant, đang lắng nghe chuyển động")
            except Exception as e:
                hass_state["connected"] = False
                hass_state["last_error"] = str(e)
                print(f"[LiteNVR] Lỗi kết nối HA: {e}", flush=True)
                log_event("error", f"Lỗi kết nối Home Assistant: {e}")
                close_ws()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

        try:
            ws.settimeout(1)
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            hass_state["connected"] = False
            hass_state["last_error"] = str(e)
            print(f"[LiteNVR] Mất kết nối HA, thử lại: {e}", flush=True)
            log_event("warn", f"Mất kết nối Home Assistant, thử lại: {e}")
            close_ws()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("type") != "event":
            continue
        event = msg.get("event") or {}
        if event.get("event_type") != "state_changed":
            continue
        data = event.get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        new_state = data.get("new_state") or {}
        prev_state = (data.get("old_state") or {}).get("state")
        state = str(new_state.get("state", "")).lower()
        rule = watch.get(entity_id)
        if not rule:
            continue
        # Chỉ tính là chuyển động khi trạng thái CHUYỂN sang giá trị kích hoạt
        # (tránh cảm biến báo liên tục làm ghi hình không ngừng).
        if not _hass_motion_triggered(rule, entity_id, state, prev_state):
            continue
        cam_id = rule["cam_id"]
        label = rule.get("label") or f"Chuyển động (HA: {entity_id})"
        try:
            handle_motion(cam_id, label)
            hass_state["last_event"] = f"{cam_id} <- {entity_id} ({state})"
            hass_state["last_event_at"] = int(time.time())
            print(f"[LiteNVR] HA motion: {hass_state['last_event']}", flush=True)
        except Exception as e:
            print(f"[LiteNVR] Lỗi xử lý HA motion: {e}", flush=True)

@app.get("/api/hass")
def get_hass_settings():
    ha = CONFIG.get("homeassistant", {}) or {}
    watch = _hass_rules()
    rules: Dict[str, List[dict]] = {}
    for eid, info in watch.items():
        rules.setdefault(info["cam_id"], []).append({
            "entity_id": eid, "state": info.get("state", ""), "label": info.get("label", "")})
    mq = CONFIG.get("mqtt", {}) or {}
    return {
        "enabled": bool(ha.get("enabled")),
        "base_url": ha.get("base_url", ""),
        "has_token": bool((ha.get("token") or "").strip()),
        "entities": rules,
        "status": hass_state,
        "mqtt": {
            "enabled": bool(mq.get("enabled")),
            "host": mq.get("host", ""),
            "port": int(mq.get("port") or 1883),
            "username": mq.get("username", ""),
            "has_password": bool((mq.get("password") or "").strip()),
            "tls": bool(mq.get("tls")),
            "discovery_prefix": mq.get("discovery_prefix") or "homeassistant",
            "base_topic": mq.get("base_topic") or "litenvr",
            "snapshot_interval": int(mq.get("snapshot_interval") or 15),
            "status": dict(mqtt_state),
        },
    }

@app.post("/api/hass")
def update_hass_settings(settings: HassSettings):
    base_url = settings.base_url.strip()
    mqtt_data = None
    if settings.mqtt is not None:
        data = settings.mqtt
        try:
            discovery_prefix = _mqtt_clean_topic(data.discovery_prefix, "homeassistant")
            base_topic = _mqtt_clean_topic(data.base_topic, "litenvr")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not 1 <= int(data.port) <= 65535:
            raise HTTPException(status_code=400, detail="Cổng MQTT không hợp lệ (1-65535)")
        host = _mqtt_normalize_host(data.host)
        effective_host = host or _mqtt_effective_host({"host": "", "base_url": base_url})
        if data.enabled and not effective_host:
            raise HTTPException(status_code=400,
                                detail="Chưa có host MQTT (core-mosquitto hoặc IP Home Assistant)")
        mqtt_data = {
            "enabled": bool(data.enabled),
            "host": host,
            "port": int(data.port),
            "username": data.username.strip(),
            "password": data.password,
            "tls": bool(data.tls),
            "discovery_prefix": discovery_prefix,
            "base_topic": base_topic,
            "snapshot_interval": max(5, min(300, int(data.snapshot_interval or 15))),
        }
    ha = CONFIG.setdefault("homeassistant", {})
    ha["enabled"] = settings.enabled
    ha["base_url"] = base_url
    if settings.token.strip():
        ha["token"] = settings.token.strip()
    # Cảm biến nay gán riêng theo từng camera (xem `hass_entities`), không ghi đè ở đây.
    if mqtt_data is not None:
        mq = CONFIG.setdefault("mqtt", {})
        mq["enabled"] = mqtt_data["enabled"]
        mq["host"] = mqtt_data["host"]
        mq["port"] = mqtt_data["port"]
        mq["username"] = mqtt_data["username"]
        if mqtt_data["password"]:
            mq["password"] = mqtt_data["password"]
        mq["tls"] = mqtt_data["tls"]
        mq["discovery_prefix"] = mqtt_data["discovery_prefix"]
        mq["base_topic"] = mqtt_data["base_topic"]
        mq["snapshot_interval"] = mqtt_data["snapshot_interval"]
        mqtt_state["enabled"] = bool(mq["enabled"])
    save_config(CONFIG)
    _mqtt_wake.set()
    return {"status": "ok"}

@app.post("/api/hass/entities")
def hass_available_entities(settings: HassSettings):
    """Trả danh sách cảm biến/entity từ Home Assistant để UI gợi ý chọn."""
    base_url = settings.base_url.strip() or (CONFIG.get("homeassistant", {}).get("base_url") or "").strip()
    token = settings.token.strip() or (CONFIG.get("homeassistant", {}).get("token") or "").strip()
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="Chưa cấu hình URL hoặc Token của Home Assistant")
    url = base_url.rstrip("/") + "/api/states"
    req = urllib.request.Request(url, headers=_hass_default_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            states = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HA trả về lỗi HTTP {e.code} (sai token?)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không lấy được danh sách sensor: {e}")

    out = []
    for s in states:
        eid = str(s.get("entity_id", ""))
        if not (eid.startswith("binary_sensor.") or eid.startswith("input_boolean.")
                or eid.startswith("switch.") or eid.startswith("device_tracker.")
                or eid.startswith("person.")):
            continue
        attrs = s.get("attributes") or {}
        out.append({
            "entity_id": eid,
            "name": attrs.get("friendly_name", eid),
            "device_class": attrs.get("device_class", "") or "",
            "state": s.get("state", ""),
            "domain": eid.split(".", 1)[0],
            "state_options": _hass_state_options(eid),
            "suggested_state": _default_active_state(eid),
        })
    motion_first = {"motion", "presence", "occupancy", "movement", "door", "window",
                    "smoke", "sound", "vibration"}
    out.sort(key=lambda x: (0 if (x["device_class"] or "").lower() in motion_first else 1,
                            (x["device_class"] or "zzz"), x["name"].lower()))
    return {"entities": out}

@app.post("/api/hass/test")
def test_hass_connection(settings: HassSettings):
    token = settings.token.strip() or (CONFIG.get("homeassistant", {}).get("token") or "").strip()
    base_url = settings.base_url.strip() or (CONFIG.get("homeassistant", {}).get("base_url") or "").strip()
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="Thiếu URL hoặc Token của Home Assistant")
    try:
        info = hass_ping(base_url, token)
        return {"status": "ok", "message": info.get("message", "API running")}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HA trả về lỗi HTTP {e.code} (sai token?)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không kết nối được HA: {e}")


mqtt_state = {
    "enabled": False,
    "connected": False,
    "broker": "",
    "published": 0,
    "last_connect": 0,
    "last_publish": 0,
    "last_error": "",
    "connect_since": 0,
}
_mqtt_lock = threading.Lock()
_mqtt_known_lock = threading.Lock()
_mqtt_wake = threading.Event()
_mqtt_client = None
_mqtt_client_sig = None
_mqtt_camera_sig = None
_mqtt_last_payload: Dict[str, bytes] = {}
_mqtt_last_shot: Dict[str, float] = {}


def _mqtt_lib():
    try:
        from paho.mqtt import client as mqtt_lib
    except Exception as e:
        raise RuntimeError("Thiếu thư viện paho-mqtt trong image LiteNVR") from e
    return mqtt_lib


def _mqtt_cfg() -> dict:
    return CONFIG.get("mqtt", {}) or {}


def _mqtt_normalize_host(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        return raw[1:raw.index("]")].strip()
    if "://" in raw:
        try:
            return (urllib.parse.urlsplit(raw).hostname or "").strip()
        except Exception:
            return ""
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host
    return raw.strip().strip("[]")


def _mqtt_effective_host(cfg: dict = None) -> str:
    cfg = cfg if isinstance(cfg, dict) else _mqtt_cfg()
    host = _mqtt_normalize_host(cfg.get("host") or "")
    if host:
        return host
    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        base_url = str((CONFIG.get("homeassistant") or {}).get("base_url") or "").strip()
    if not base_url:
        return ""
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").strip()
    except Exception:
        return ""


def _mqtt_clean_topic(value: str, default: str) -> str:
    topic = str(value or "").strip().strip("/")
    if not topic:
        return default
    if any(ch in topic for ch in "+#") or any(ch.isspace() for ch in topic) or "//" in topic:
        raise ValueError("MQTT topic không hợp lệ (không dùng +, #, khoảng trắng hoặc //)")
    return topic


def _mqtt_topics(cfg: dict = None) -> tuple:
    cfg = cfg if isinstance(cfg, dict) else _mqtt_cfg()
    try:
        prefix = _mqtt_clean_topic(cfg.get("discovery_prefix"), "homeassistant")
    except ValueError:
        prefix = "homeassistant"
    try:
        base = _mqtt_clean_topic(cfg.get("base_topic"), "litenvr")
    except ValueError:
        base = "litenvr"
    return prefix, base


def _mqtt_slug(cam_id: str) -> str:
    text = unicodedata.normalize("NFKD", str(cam_id or "")).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "cam"


def _mqtt_camera_slugs() -> Dict[str, str]:
    out: Dict[str, str] = {}
    used = set()
    for cam_id in sorted((CONFIG.get("cameras") or {}).keys()):
        slug = _mqtt_slug(cam_id)
        if slug in used:
            slug = f"{slug}_{hashlib.sha1(str(cam_id).encode()).hexdigest()[:6]}"
        used.add(slug)
        out[cam_id] = slug
    return out


def _mqtt_camera_signature(cfg: dict = None) -> tuple:
    prefix, base = _mqtt_topics(cfg)
    cameras = CONFIG.get("cameras") or {}
    items = []
    for cam_id in sorted(cameras.keys()):
        cam = cameras.get(cam_id) or {}
        items.append((str(cam_id), str(cam.get("name") or cam_id)))
    return prefix, base, tuple(items)


def _mqtt_load_known() -> Dict[str, dict]:
    try:
        with _mqtt_known_lock:
            with open(MQTT_DISCOVERY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, item in data.items():
        if isinstance(item, dict) and isinstance(item.get("topic"), str):
            out[str(key)] = item
    return out


def _mqtt_save_known(data: Dict[str, dict]):
    try:
        tmp = MQTT_DISCOVERY_FILE + ".tmp"
        with _mqtt_known_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, MQTT_DISCOVERY_FILE)
    except Exception as e:
        log_event("warn", f"Không lưu được danh sách MQTT discovery: {mask_secrets(str(e))}")


def _mqtt_discovery_payload(cam_id: str, cam: dict, slug: str, cfg: dict = None) -> tuple:
    _, base = _mqtt_topics(cfg)
    image_topic = f"{base}/camera/{slug}/image"
    name = str((cam or {}).get("name") or cam_id)
    payload = {
        "name": name,
        "unique_id": f"litenvr_camera_{slug}",
        "default_entity_id": f"camera.litenvr_{slug}",
        "topic": image_topic,
        "availability_topic": f"{base}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": ["litenvr"],
            "name": "LiteNVR",
            "manufacturer": "LiteNVR",
            "model": "LiteNVR NVR",
            "sw_version": APP_VERSION,
        },
        "origin": {
            "name": "LiteNVR",
            "sw_version": APP_VERSION,
            "support_url": "https://github.com/trankhanhduy2929-beep/litenvr_docker_zimaos",
        },
    }
    prefix, _ = _mqtt_topics(cfg)
    topic = f"{prefix}/camera/litenvr/{slug}/config"
    return topic, payload


def _mqtt_clear_discovery(client, wait: bool = False):
    known = _mqtt_load_known()
    if not known:
        return True
    ok = True
    infos = []
    for item in known.values():
        for topic in (item.get("topic", ""), item.get("image", "")):
            if not topic:
                continue
            try:
                info = client.publish(topic, "", qos=0, retain=True)
                infos.append(info)
                if getattr(info, "rc", 0) != 0:
                    ok = False
            except Exception:
                ok = False
    if wait:
        for info in infos:
            try:
                waiter = getattr(info, "wait_for_publish", None)
                if waiter:
                    waiter(timeout=1)
            except Exception:
                pass
    if ok:
        _mqtt_save_known({})
    return ok


def _mqtt_publish_discovery(client, cfg: dict = None) -> bool:
    if client is None:
        return False
    cfg = cfg if isinstance(cfg, dict) else _mqtt_cfg()
    current = {}
    ok = True
    _, base = _mqtt_topics(cfg)
    cameras = CONFIG.get("cameras") or {}
    slugs = _mqtt_camera_slugs()
    for cam_id, slug in slugs.items():
        topic, payload = _mqtt_discovery_payload(cam_id, cameras.get(cam_id) or {}, slug, cfg)
        try:
            info = client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=True)
            if getattr(info, "rc", 0) != 0:
                ok = False
        except Exception as e:
            ok = False
            mqtt_state["last_error"] = mask_secrets(str(e))
        current[slug] = {"topic": topic, "image": f"{base}/camera/{slug}/image"}
    known = _mqtt_load_known()
    for slug, item in known.items():
        if slug in current:
            continue
        for topic in (item.get("topic", ""), item.get("image", "")):
            if not topic:
                continue
            try:
                info = client.publish(topic, "", qos=0, retain=True)
                if getattr(info, "rc", 0) != 0:
                    ok = False
            except Exception:
                ok = False
    if ok:
        _mqtt_save_known(current)
    if ok:
        mqtt_state["published"] = len(current)
    mqtt_state["last_publish"] = int(time.time())
    return ok


def _mqtt_publish_snapshot_path(cam_id: str, path: str = "", force: bool = False) -> bool:
    cfg = _mqtt_cfg()
    if not cfg.get("enabled") or not mqtt_state.get("connected"):
        return False
    slug = _mqtt_camera_slugs().get(cam_id)
    if not slug:
        return False
    if not path:
        path = _latest_snapshot(cam_id)
    if not _valid_snapshot(path):
        return False
    try:
        if os.path.getsize(path) > 8 * 1024 * 1024:
            return False
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    if not force and _mqtt_last_payload.get(cam_id) == data:
        return False
    with _mqtt_lock:
        client = _mqtt_client
    if client is None:
        return False
    _, base = _mqtt_topics(cfg)
    try:
        info = client.publish(f"{base}/camera/{slug}/image", data, qos=0, retain=True)
        if getattr(info, "rc", 0) != 0:
            return False
    except Exception as e:
        mqtt_state["last_error"] = mask_secrets(str(e))
        return False
    _mqtt_last_payload[cam_id] = data
    mqtt_state["last_publish"] = int(time.time())
    return True


def _mqtt_publish_periodic(cfg: dict = None):
    cfg = cfg if isinstance(cfg, dict) else _mqtt_cfg()
    try:
        interval = max(5, min(300, int(cfg.get("snapshot_interval") or 15)))
    except (TypeError, ValueError):
        interval = 15
    now = time.time()
    cameras = CONFIG.get("cameras") or {}
    for cam_id in list(cameras.keys()):
        if _shutdown.is_set() or not mqtt_state.get("connected"):
            return
        if now - _mqtt_last_shot.get(cam_id, 0) < interval:
            continue
        _mqtt_last_shot[cam_id] = now
        slug = _mqtt_camera_slugs().get(cam_id, _mqtt_slug(cam_id))
        path = os.path.join(MQTT_SNAPSHOT_DIR, f"{slug}.jpg")
        try:
            if _go2rtc_snapshot(cam_id, path):
                _mqtt_publish_snapshot_path(cam_id, path)
                continue
        except Exception as e:
            mqtt_state["last_error"] = mask_secrets(str(e))
        latest = _latest_snapshot(cam_id)
        if latest:
            _mqtt_publish_snapshot_path(cam_id, latest)


def _mqtt_new_client(cfg: dict, client_id: str, will: bool = False):
    mqtt_lib = _mqtt_lib()
    try:
        client = mqtt_lib.Client(mqtt_lib.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):
        client = mqtt_lib.Client(client_id=client_id)
    username = str(cfg.get("username") or "")
    password = str(cfg.get("password") or "")
    if username or password:
        client.username_pw_set(username or "litenvr", password or None)
    if cfg.get("tls"):
        client.tls_set()
    if will:
        _, base = _mqtt_topics(cfg)
        client.will_set(f"{base}/status", "offline", qos=0, retain=True)
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    return client


def _mqtt_install_callbacks(client, cfg: dict):
    def on_connect(c, userdata, flags, reason_code, properties=None):
        global _mqtt_camera_sig
        code = int(getattr(reason_code, "value", reason_code))
        if code == 0:
            _, base = _mqtt_topics(cfg)
            mqtt_state.update(connected=True, last_connect=int(time.time()),
                              last_error="", connect_since=0)
            _mqtt_last_payload.clear()
            _mqtt_last_shot.clear()
            try:
                c.publish(f"{base}/status", "online", qos=0, retain=True)
            except Exception:
                pass
            if _mqtt_publish_discovery(c, cfg):
                _mqtt_camera_sig = _mqtt_camera_signature(cfg)
        else:
            mqtt_state.update(connected=False, last_error=f"MQTT từ chối kết nối (mã {code})")

    def on_disconnect(c, userdata, *args):
        mqtt_state["connected"] = False
        if not _shutdown.is_set():
            mqtt_state["last_error"] = "Mất kết nối MQTT; đang thử lại"

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect


def _mqtt_disconnect(clear: bool = False):
    global _mqtt_client, _mqtt_client_sig, _mqtt_camera_sig
    with _mqtt_lock:
        client = _mqtt_client
        _mqtt_client = None
    _mqtt_client_sig = None
    _mqtt_camera_sig = None
    if client is None:
        mqtt_state["connected"] = False
        mqtt_state["connect_since"] = 0
        return
    if clear and mqtt_state.get("connected"):
        try:
            _mqtt_clear_discovery(client, wait=True)
        except Exception:
            pass
    _, base = _mqtt_topics()
    try:
        info = client.publish(f"{base}/status", "offline", qos=0, retain=True)
        waiter = getattr(info, "wait_for_publish", None)
        if waiter:
            waiter(timeout=1)
    except Exception:
        pass
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.loop_stop()
    except Exception:
        pass
    mqtt_state["connected"] = False
    mqtt_state["connect_since"] = 0


def mqtt_shutdown():
    _mqtt_wake.set()
    _mqtt_disconnect(clear=False)


def mqtt_worker():
    global _mqtt_client, _mqtt_client_sig, _mqtt_camera_sig
    try:
        _mqtt_lib()
    except RuntimeError as e:
        mqtt_state["last_error"] = str(e)
        print(f"[LiteNVR] {e}", flush=True)
        return
    while not _shutdown.is_set():
        cfg = dict(_mqtt_cfg())
        enabled = bool(cfg.get("enabled"))
        mqtt_state["enabled"] = enabled
        host = _mqtt_effective_host(cfg)
        if not enabled or not host:
            if _mqtt_client is not None:
                _mqtt_disconnect(clear=True)
            mqtt_state["connected"] = False
            mqtt_state["connect_since"] = 0
            mqtt_state["last_error"] = "" if not enabled else "Chưa có host MQTT (core-mosquitto hoặc IP HA)"
            _mqtt_wake.wait(5)
            _mqtt_wake.clear()
            continue
        try:
            port = int(cfg.get("port") or 1883)
        except (TypeError, ValueError):
            port = 1883
        prefix, base = _mqtt_topics(cfg)
        signature = (
            host,
            port,
            str(cfg.get("username") or ""),
            str(cfg.get("password") or ""),
            bool(cfg.get("tls")),
            prefix,
            base,
        )
        if _mqtt_client is None or _mqtt_client_sig != signature:
            if _mqtt_client is not None:
                _mqtt_disconnect(clear=bool(mqtt_state.get("connected")))
            try:
                client = _mqtt_new_client(cfg, f"litenvr-{uuid.uuid4().hex[:10]}", will=True)
                _mqtt_install_callbacks(client, cfg)
                with _mqtt_lock:
                    _mqtt_client = client
                _mqtt_client_sig = signature
                _mqtt_camera_sig = None
                mqtt_state["broker"] = f"{host}:{port}"
                mqtt_state["connect_since"] = time.time()
                client.connect_async(host, port, keepalive=60)
                client.loop_start()
            except Exception as e:
                mqtt_state["connected"] = False
                mqtt_state["last_error"] = mask_secrets(str(e))
                _mqtt_disconnect(clear=False)
                _mqtt_wake.wait(10)
                _mqtt_wake.clear()
                continue
        if not mqtt_state.get("connected") and not mqtt_state.get("last_error"):
            since = float(mqtt_state.get("connect_since") or 0)
            if since and time.time() - since > 10:
                mqtt_state["last_error"] = f"Chưa kết nối được {host}:{port}"
        if mqtt_state.get("connected") and _mqtt_client is not None:
            camera_sig = _mqtt_camera_signature(cfg)
            if _mqtt_camera_sig != camera_sig:
                if _mqtt_publish_discovery(_mqtt_client, cfg):
                    _mqtt_camera_sig = camera_sig
            _mqtt_publish_periodic(cfg)
        _mqtt_wake.wait(1)
        _mqtt_wake.clear()


def _mqtt_test_connection(cfg: dict) -> dict:
    host = _mqtt_effective_host(cfg)
    if not host:
        raise RuntimeError("Chưa có host MQTT")
    try:
        port = int(cfg.get("port") or 1883)
    except (TypeError, ValueError):
        port = 1883
    _, base = _mqtt_topics(cfg)
    client = _mqtt_new_client(cfg, f"litenvr-test-{uuid.uuid4().hex[:8]}")
    connected = threading.Event()
    errors = []

    def on_connect(c, userdata, flags, reason_code, properties=None):
        code = int(getattr(reason_code, "value", reason_code))
        if code == 0:
            connected.set()
        else:
            errors.append(f"Broker từ chối kết nối (mã {code})")
            connected.set()

    client.on_connect = on_connect
    try:
        client.connect(host, port, keepalive=15)
        client.loop_start()
        if not connected.wait(6):
            raise RuntimeError(f"Không kết nối được {host}:{port}")
        if errors:
            raise RuntimeError(errors[0])
        client.publish(f"{base}/test", "LiteNVR MQTT OK", qos=0, retain=False)
        return {"host": host, "port": port}
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass


@app.post("/api/hass/mqtt/test")
def test_mqtt_connection(settings: MqttSettings):
    current = _mqtt_cfg()
    cfg = dict(current)
    cfg.update({
        "host": settings.host,
        "base_url": settings.base_url,
        "port": settings.port,
        "username": settings.username,
        "password": settings.password or current.get("password", ""),
        "tls": settings.tls,
        "discovery_prefix": settings.discovery_prefix,
        "base_topic": settings.base_topic,
        "snapshot_interval": settings.snapshot_interval,
    })
    try:
        info = _mqtt_test_connection(cfg)
        return {"status": "ok", "message": f"Đã kết nối MQTT {info['host']}:{info['port']}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không kết nối được MQTT: {mask_secrets(str(e))}")

@app.get("/api/timeline/{cam_id}")
def get_timeline(cam_id: str, date: str, source: str = "auto"):
    if source not in ("auto", "local", "drive", "smb"):
        raise HTTPException(status_code=400, detail="Nguồn phát lại không hợp lệ")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
        raise HTTPException(status_code=400, detail="Ngày không hợp lệ")
    cam_id = os.path.basename(cam_id)
    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    segments = []
    local_names = set()
    locked = locked_paths()
    if source in ("auto", "local") and os.path.exists(cam_rec_dir):
        for fname in sorted(os.listdir(cam_rec_dir)):
            if fname.startswith(date) and fname.endswith(".mp4"):
                try:
                    time_part = fname.replace(".mp4", "").split("_")[1]
                    h, m, s = map(int, time_part.split("-"))
                    start_sec = h * 3600 + m * 60 + s
                    local_names.add(fname)
                    segments.append({
                        "file": f"api/play/{cam_id}/{fname}?t=2",
                        "cam": cam_id,
                        "fname": fname,
                        "time": time_part.replace("-", ":"),
                        "start_sec": start_sec,
                        "locked": f"{cam_id}/{fname}" in locked,
                        "drive": False,
                        "smb": False,
                    })
                except Exception:
                    pass

    # Bổ sung các đoạn chỉ còn trên Google Drive (đã tải lên, đã xoá khỏi ổ cứng)
    if source in ("auto", "drive"):
        try:
            rows = db_conn().execute(
                "SELECT name FROM drive_uploads WHERE cam_id=? AND status='done' AND name GLOB ? ORDER BY name",
                (cam_id, date + "_*.mp4")).fetchall()
        except Exception:
            rows = []
        for (fname,) in rows:
            if fname in local_names:
                continue
            try:
                time_part = fname.replace(".mp4", "").split("_")[1]
                h, m, s = map(int, time_part.split("-"))
                start_sec = h * 3600 + m * 60 + s
            except Exception:
                continue
            segments.append({
                "file": f"api/play/{cam_id}/{fname}?t=2",
                "cam": cam_id,
                "fname": fname,
                "time": time_part.replace("-", ":"),
                "start_sec": start_sec,
                "locked": f"{cam_id}/{fname}" in locked,
                "drive": True,
                "smb": False,
            })

    # Chỉ đọc danh sách SMB khi người dùng chọn đúng nguồn (tránh chậm timeline).
    if source == "smb":
        for fname in _smb_list_recordings(cam_id, date):
            try:
                time_part = fname.replace(".mp4", "").split("_")[1]
                h, m, s = map(int, time_part.split("-"))
                start_sec = h * 3600 + m * 60 + s
            except Exception:
                continue
            segments.append({
                "file": f"api/smb/play/{urllib.parse.quote(cam_id)}/{date}/{urllib.parse.quote(fname)}?t=2",
                "cam": cam_id,
                "fname": fname,
                "time": time_part.replace("-", ":"),
                "start_sec": start_sec,
                "locked": False,
                "drive": False,
                "smb": True,
            })
    segments.sort(key=lambda s: s["start_sec"])

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, timestamp, time_str, label, snapshot, ai_description FROM events WHERE cam_id=? AND date_str=? ORDER BY timestamp DESC",
        (cam_id, date)
    )
    events = []
    for row in c.fetchall():
        try:
            h, m, s = map(int, row[2].replace("-", ":").split(":"))
        except Exception:
            continue
        events.append({
            "id": row[0],
            "timestamp": row[1],
            "time": row[2],
            "sec_of_day": h * 3600 + m * 60 + s,
            "label": row[3],
            "snapshot": f"snapshots/{row[4]}",
            "ai_description": row[5] or ""
        })
    return {
        "segments": segments,
        "events": events,
        "post_motion_sec": CONFIG.get("post_motion_sec", 60),
    }

@app.get("/api/recording_days/{cam_id}")
def get_recording_days(cam_id: str, month: str):
    """Các ngày có bản ghi trong tháng YYYY-MM (ổ cứng + đã tải lên Drive)."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(status_code=400, detail="Tháng không hợp lệ (YYYY-MM)")
    days = set()
    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    try:
        for fname in os.listdir(cam_rec_dir):
            if fname.startswith(month + "-") and fname.endswith(".mp4"):
                days.add(fname[:10])
    except OSError:
        pass
    try:
        rows = db_conn().execute(
            "SELECT name FROM drive_uploads WHERE cam_id=? AND status='done' AND name GLOB ?",
            (cam_id, month + "-*.mp4")).fetchall()
    except Exception:
        rows = []
    for (fname,) in rows:
        if len(fname) >= 10 and fname[4] == "-" and fname[7] == "-":
            days.add(fname[:10])
    return {"month": month, "days": sorted(days)}

_thumb_slots = threading.BoundedSemaphore(2)


def _thumb_path(cam_id: str, fname: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", fname)
    return os.path.join(THUMBS_DIR, cam_id, safe + ".jpg")


def generate_thumbnail(cam_id: str, fname: str) -> Optional[str]:
    """Tạo (và cache) ảnh thu nhỏ cho một đoạn ghi hình cục bộ. Trả None nếu không có."""
    src = os.path.join(RECORDINGS_DIR, cam_id, fname)
    if not os.path.isfile(src):
        return None
    out = _thumb_path(cam_id, fname)
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0 and \
                os.path.getmtime(out) >= os.path.getmtime(src):
            return out
    except OSError:
        pass
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + f".{os.getpid()}.part.jpg"
    cmd = [*_ffmpeg_base_args(), "-y", "-ss", "1", "-i", src, "-frames:v", "1",
           "-vf", "scale=320:-2", "-q:v", "4", tmp]
    rc = -1
    with _thumb_slots:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
        except Exception:
            rc = -1
    if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
        try:
            os.replace(tmp, out)
            return out
        except OSError:
            return None
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


@app.get("/api/thumb/{cam_id}/{fname}")
def get_thumbnail(cam_id: str, fname: str, request: Request):
    assert_cam_access(request, cam_id)
    if cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.mp4", fname):
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    path = generate_thumbnail(cam_id, fname)
    if not path:
        raise HTTPException(status_code=404, detail="Không tạo được ảnh thu nhỏ")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/events")
def search_events(request: Request, cam_id: str = "", q: str = "",
                  label: str = "", date_from: str = "", date_to: str = "",
                  starred: int = 0, limit: int = 200, offset: int = 0):
    """Tìm kiếm sự kiện chuyển động theo camera, nhãn, khoảng ngày và trạng thái đánh dấu."""
    u = getattr(request.state, "user", None) or {}
    allowed = None if (u.get("role") == "admin" or not u.get("cameras")) else set(u["cameras"])
    try:
        limit = max(1, min(int(limit or 200), 1000))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(offset or 0))
    except Exception:
        offset = 0
    where = " WHERE 1=1"
    params: list = []
    if cam_id:
        assert_cam_access(request, cam_id)
        where += " AND cam_id=?"
        params.append(cam_id)
    elif allowed is not None:
        if not allowed:
            return {"events": [], "count": 0, "total": 0, "offset": offset, "limit": limit}
        where += " AND cam_id IN (%s)" % ",".join("?" * len(allowed))
        params.extend(sorted(allowed))
    if date_from:
        where += " AND date_str>=?"
        params.append(date_from)
    if date_to:
        where += " AND date_str<=?"
        params.append(date_to)
    if label:
        where += " AND label=?"
        params.append(label)
    if q:
        like = f"%{q}%"
        where += " AND (label LIKE ? OR cam_id LIKE ? OR ai_description LIKE ?)"
        params.extend([like, like, like])
    if starred:
        where += " AND starred=1"
    conn = db_conn()
    total = conn.execute("SELECT COUNT(*) FROM events" + where, params).fetchone()[0]
    rows = conn.execute(
        "SELECT id, cam_id, timestamp, date_str, time_str, label, snapshot, starred, ai_description "
        "FROM events" + where + " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    cams = CONFIG.get("cameras", {})
    out = []
    for r in rows:
        cid = r[1]
        t = (r[4] or "").replace("-", ":")
        try:
            hh, mm, ss = (int(x) for x in t.split(":")[:3])
            sec_of_day = hh * 3600 + mm * 60 + ss
        except Exception:
            sec_of_day = 0
        snap = r[6] if r[6] and _valid_snapshot(os.path.join(SNAPSHOTS_DIR, r[6])) else ""
        out.append({
            "id": r[0], "cam_id": cid,
            "cam_name": (cams.get(cid) or {}).get("name", cid),
            "timestamp": r[2], "date": r[3], "time": t, "label": r[5],
            "sec_of_day": sec_of_day,
            "starred": bool(r[7]),
            "ai_description": r[8] or "",
            "snapshot": f"snapshots/{snap}" if snap else "",
        })
    return {"events": out, "count": len(out), "total": total, "offset": offset, "limit": limit}

@app.get("/api/events/labels")
def event_labels():
    """Danh sách nhãn sự kiện đã ghi nhận, dùng cho bộ lọc."""
    rows = db_conn().execute(
        "SELECT DISTINCT label FROM events WHERE label IS NOT NULL AND label<>'' ORDER BY label").fetchall()
    return {"labels": [r[0] for r in rows]}

@app.post("/api/events/{event_id}/star")
def star_event(event_id: int, body: EventStar, request: Request):
    """Đánh dấu/bỏ đánh dấu một sự kiện để giữ lại khi dọn dẹp."""
    conn = db_conn()
    row = conn.execute("SELECT cam_id FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sự kiện không tồn tại")
    assert_cam_access(request, row[0])
    conn.execute("UPDATE events SET starred=? WHERE id=?", (1 if body.starred else 0, event_id))
    conn.commit()
    return {"status": "ok", "id": event_id, "starred": bool(body.starred)}

@app.get("/api/locks")
def list_locks():
    c = db_conn().cursor()
    c.execute("SELECT path, cam_id, created, note FROM locks ORDER BY created DESC")
    return {"locks": [{"path": r[0], "cam_id": r[1], "created": r[2], "note": r[3]} for r in c.fetchall()]}

@app.post("/api/locks")
def add_lock(req: LockReq, request: Request):
    assert_cam_access(request, req.cam_id)
    if not lock_recording(req.cam_id, req.file, req.note):
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi để khoá")
    log_event("info", f"Khoá bản ghi {req.file}", req.cam_id)
    return {"status": "ok"}

@app.delete("/api/locks/{cam_id}/{fname}")
def remove_lock(cam_id: str, fname: str, request: Request):
    assert_cam_access(request, cam_id)
    if not unlock_recording(cam_id, fname):
        raise HTTPException(status_code=404, detail="Bản ghi này chưa được khoá")
    log_event("info", f"Mở khoá bản ghi {fname}", cam_id)
    return {"status": "ok"}

# ---------------- Thông báo chuyển động (Telegram / Webhook) ----------------
@app.get("/api/notify")
def get_notify_settings():
    nt = CONFIG.get("notify") or {}
    return {
        "telegram_enabled": bool(nt.get("telegram_enabled")),
        "telegram_chat_id": nt.get("telegram_chat_id", ""),
        "has_telegram_token": bool((nt.get("telegram_token") or "").strip()),
        "webhook_url": nt.get("webhook_url", ""),
        "public_url": nt.get("public_url", ""),
        "with_photo": bool(nt.get("with_photo", True)),
        "telegram_clip": bool(nt.get("telegram_clip", False)),
        "telegram_clip_sec": int(nt.get("telegram_clip_sec") or 8),
        "disk_alert_enabled": bool(nt.get("disk_alert_enabled", False)),
        "disk_warn_percent": float(nt.get("disk_warn_percent", 85) or 85),
        "disk_crit_percent": float(nt.get("disk_crit_percent", 95) or 95),
        "camera_offline_alert": bool(nt.get("camera_offline_alert", True)),
    }

@app.post("/api/notify")
def update_notify_settings(settings: NotifySettings):
    nt = CONFIG.setdefault("notify", dict(DEFAULT_CONFIG["notify"]))
    nt["telegram_enabled"] = settings.telegram_enabled
    if settings.telegram_token.strip():
        nt["telegram_token"] = settings.telegram_token.strip()
    nt["telegram_chat_id"] = settings.telegram_chat_id.strip()
    nt["webhook_url"] = settings.webhook_url.strip()
    nt["public_url"] = settings.public_url.strip().rstrip("/")
    nt["with_photo"] = settings.with_photo
    nt["telegram_clip"] = settings.telegram_clip
    nt["telegram_clip_sec"] = max(3, min(20, int(settings.telegram_clip_sec or 8)))
    nt["disk_alert_enabled"] = settings.disk_alert_enabled
    nt["disk_warn_percent"] = float(settings.disk_warn_percent or 85)
    nt["disk_crit_percent"] = float(settings.disk_crit_percent or 95)
    nt["camera_offline_alert"] = settings.camera_offline_alert
    save_config(CONFIG)
    log_event("info", "Đã lưu cấu hình thông báo")
    return {"status": "ok"}

@app.post("/api/notify/test")
def test_notify():
    nt = CONFIG.get("notify") or {}
    results = {}
    if nt.get("telegram_enabled") and nt.get("telegram_token") and nt.get("telegram_chat_id"):
        if not _license_valid():
            results["telegram"] = "lỗi: cần license còn hiệu lực"
        else:
            try:
                _telegram_send(nt["telegram_token"], str(nt["telegram_chat_id"]),
                               "✅ LiteNVR: thông báo thử nghiệm — cấu hình Telegram hoạt động!")
                results["telegram"] = "ok"
            except Exception as e:
                results["telegram"] = f"lỗi: {e}"
    if (nt.get("webhook_url") or "").strip():
        try:
            req = urllib.request.Request(nt["webhook_url"].strip(), data=json.dumps(
                {"test": True, "app": "LiteNVR"}).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            results["webhook"] = "ok"
        except Exception as e:
            results["webhook"] = f"lỗi: {e}"
    if not results:
        raise HTTPException(status_code=400, detail="Chưa bật Telegram và chưa có Webhook URL")
    return {"status": "ok", "results": results}

# ---------------- Mô tả sự kiện bằng AI ----------------
@app.get("/api/ai")
def get_ai_settings():
    ai_cfg = _ai_settings()
    return {
        "enabled": bool(ai_cfg.get("enabled")),
        "provider": ai_cfg["provider"],
        "model": ai_cfg["model"],
        "base_url": ai_cfg["base_url"],
        "prompt": ai_cfg["prompt"],
        "timeout": ai_cfg["timeout"],
        "has_api_key": bool(ai_cfg.get("api_key")),
        "providers": [
            {"id": key, "label": value[0], "default_model": value[1],
             "default_base_url": value[2]}
            for key, value in AI_PROVIDERS.items()
        ],
    }

@app.post("/api/ai")
def update_ai_settings(settings: AiSettings):
    provider = settings.provider.strip().lower()
    if provider not in AI_PROVIDERS:
        raise HTTPException(status_code=400, detail="Nhà cung cấp AI không hợp lệ")
    _, default_model, default_base = AI_PROVIDERS[provider]
    ai_cfg = CONFIG.setdefault("ai", dict(DEFAULT_CONFIG["ai"]))
    ai_cfg["enabled"] = settings.enabled
    ai_cfg["provider"] = provider
    if settings.api_key.strip():
        ai_cfg["api_key"] = settings.api_key.strip()
    ai_cfg["model"] = _normalize_ai_model(
        provider, settings.model.strip() or default_model)
    ai_cfg["base_url"] = _normalize_ai_base_url(
        settings.base_url, provider, default_base)
    ai_cfg["prompt"] = settings.prompt.strip() or DEFAULT_CONFIG["ai"]["prompt"]
    ai_cfg["timeout"] = max(5, min(120, int(settings.timeout or 30)))
    if not ai_cfg["model"]:
        raise HTTPException(status_code=400, detail="Chưa nhập model AI")
    if provider != "custom" and not ai_cfg.get("api_key"):
        raise HTTPException(status_code=400, detail="Chưa nhập API key AI")
    save_config(CONFIG)
    log_event("info", f"Đã lưu cấu hình AI ({provider}/{ai_cfg['model']})")
    return {"status": "ok"}

@app.post("/api/ai/test")
def test_ai():
    if not _license_valid():
        raise HTTPException(status_code=402, detail="Cần license LiteNVR Pro còn hiệu lực")
    ai_cfg = _ai_settings()
    if not ai_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="Chưa bật mô tả AI")
    snapshot_dir = SNAPSHOTS_DIR
    candidates = []
    try:
        for name in os.listdir(snapshot_dir):
            path = os.path.join(snapshot_dir, name)
            if _valid_snapshot(path):
                candidates.append((os.path.getmtime(path), path))
    except OSError:
        pass
    if not candidates:
        raise HTTPException(status_code=400, detail="Chưa có snapshot để kiểm tra AI")
    path = max(candidates)[1]
    try:
        result = _ai_describe_snapshot(path, ai_cfg)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=mask_secrets(str(exc)))
    return {"status": "ok", "result": result}

@app.post("/api/ai/check")
def check_ai(settings: AiSettings):
    provider = settings.provider.strip().lower()
    if provider not in AI_PROVIDERS:
        raise HTTPException(status_code=400, detail="Nhà cung cấp AI không hợp lệ")
    _, default_model, default_base = AI_PROVIDERS[provider]
    saved = _ai_settings()
    api_key = settings.api_key.strip()
    if not api_key and provider == saved.get("provider"):
        api_key = saved.get("api_key", "")
    model = _normalize_ai_model(provider, settings.model.strip() or default_model)
    if not model:
        raise HTTPException(status_code=400, detail="Chưa nhập model AI")
    if provider != "custom" and not api_key:
        raise HTTPException(status_code=400, detail="Chưa nhập API key AI")
    base = _normalize_ai_base_url(settings.base_url, provider, default_base)
    model = _migrate_ai_model(provider, model, base, default_base)
    ai_cfg = {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "base_url": base,
        "timeout": max(5, min(120, int(settings.timeout or 30))),
    }
    try:
        result = _ai_check_connection(ai_cfg)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=mask_secrets(str(exc)))
    return {"status": "ok", "valid": True, **result}

# ---------------- Bản quyền LiteNVR Pro ----------------
@app.get("/api/license")
def get_license_status():
    return _license_status()

@app.post("/api/license")
def activate_license(req: LicenseActivate):
    if req.server_url.strip():
        lic = CONFIG.setdefault("license", dict(DEFAULT_CONFIG["license"]))
        lic["server_url"] = req.server_url.strip().rstrip("/")
        save_config(CONFIG)
    try:
        _license_activate(req.key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=mask_secrets(str(exc)))
    return {"status": "ok", **_license_status()}

@app.post("/api/license/refresh")
def refresh_license():
    if not _license_cfg().get("key"):
        raise HTTPException(status_code=400, detail="Chưa kích hoạt license")
    ok = _license_refresh(force=True)
    if not ok:
        raise HTTPException(status_code=402, detail=_license_status().get("reason") or "License không hợp lệ")
    return {"status": "ok", **_license_status()}

# ---------------- Sao lưu ra SMB/NFS/FTP ----------------
class BackupSettings(BaseModel):
    enabled: bool = False
    kind: str = "folder"              # folder | ftp | smb
    folder_path: str = ""
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_user: str = ""
    ftp_pass: str = ""                # để trống khi lưu = giữ mật khẩu cũ
    ftp_dir: str = "/"
    ftp_tls: bool = False
    smb_host: str = ""
    smb_port: int = 445
    smb_share: str = ""
    smb_user: str = ""
    smb_pass: str = ""                # để trống khi lưu = giữ mật khẩu cũ
    smb_domain: str = ""
    smb_dir: str = ""
    smb_cleanup_enabled: bool = False
    smb_retention_days: float = 0
    smb_max_gb: float = 0
    smb_min_free_gb: float = 2
    mode: str = "copy"                # copy | move
    cameras: List[str] = []
    since_ts: float = 0


def _backup_cfg() -> dict:
    return CONFIG.get("backup") or {}


def _backup_destination_active(cfg: Optional[dict] = None) -> bool:
    b = _backup_cfg() if cfg is None else cfg
    kind = str(b.get("kind") or "")
    return bool(b.get("enabled")) and (
        (kind == "folder" and (b.get("folder_path") or "").strip())
        or (kind == "ftp" and (b.get("ftp_host") or "").strip())
        or (kind == "smb" and (b.get("smb_host") or "").strip()
            and (b.get("smb_share") or "").strip())
    )


def _smb_storage_ready() -> bool:
    b = _backup_cfg()
    return bool(b.get("enabled") and str(b.get("kind") or "") == "smb"
                and (b.get("smb_host") or "").strip() and (b.get("smb_share") or "").strip())


def _drive_storage_ready() -> bool:
    g = gd_cfg()
    return bool(g.get("enabled") and drive_connected())


def _backup_effective_cameras() -> set:
    """Camera đang được sao lưu ngoài; rỗng = không camera nào (trừ cấu hình cũ)."""
    cfg = _backup_cfg()
    selected = {str(c) for c in (cfg.get("cameras") or [])}
    if selected:
        return selected
    if cfg.get("cameras_explicit"):
        return set()
    return set(CONFIG.get("cameras", {}).keys())


def _set_camera_smb(cam_id: str, enabled: bool):
    """Bật/tắt camera trong danh sách sao lưu ngoài mà không làm mất lựa chọn khác."""
    b = CONFIG.setdefault("backup", dict(DEFAULT_CONFIG["backup"]))
    known = CONFIG.get("cameras", {})
    selected = {str(c) for c in (b.get("cameras") or []) if str(c) in known}
    if not b.get("cameras_explicit"):
        if not selected and _backup_destination_active(b):
            selected = set(known)
        b["cameras_explicit"] = True
    if enabled:
        selected.add(cam_id)
    else:
        selected.discard(cam_id)
    b["cameras"] = sorted(c for c in selected if c in known)


def _backup_done() -> set:
    try:
        rows = db_conn().execute("SELECT path FROM backup_done").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _recording_day(name: str, fallback_ts: float = 0) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_", os.path.basename(name or ""))
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(fallback_ts or time.time()).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _backup_dest_parts(rel: str, fallback_ts: float = 0):
    cam, fn = rel.split("/", 1)
    return _recording_day(fn, fallback_ts), cam, fn


def _drive_pending_paths() -> set:
    try:
        rows = db_conn().execute(
            "SELECT path FROM drive_uploads WHERE status IN ('pending','uploading','error')").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


class SmbTarget:
    def __init__(self, cfg: dict):
        try:
            import smbclient
        except ImportError as e:
            raise RuntimeError("Thiếu thư viện SMB (smbprotocol) trong image") from e
        self._client = smbclient
        self._host = (cfg.get("smb_host") or "").strip()
        self._share = (cfg.get("smb_share") or "").strip().strip("\\/")
        self._base = (cfg.get("smb_dir") or "").strip().strip("\\/").replace("/", "\\")
        self._port = int(cfg.get("smb_port") or 445)
        if not self._host or not self._share:
            raise RuntimeError("Chưa nhập máy chủ hoặc share SMB")
        user = (cfg.get("smb_user") or "").strip()
        domain = (cfg.get("smb_domain") or "").strip()
        if domain and user and "\\" not in user and "@" not in user:
            user = f"{domain}\\{user}"
        smbclient.register_session(
            self._host, username=user or None, password=(cfg.get("smb_pass") or "") or None,
            port=self._port)

    def path(self, *parts) -> str:
        segments = [self._base] if self._base else []
        for part in parts:
            clean = str(part or "").replace("/", "\\").strip("\\")
            if clean:
                segments.append(clean)
        suffix = ("\\" + "\\".join(segments)) if segments else ""
        return f"\\\\{self._host}\\{self._share}{suffix}"

    def ensure_dir(self, path: str):
        if path:
            self._client.makedirs(path, exist_ok=True)

    def upload(self, src: str, dest: str):
        self.ensure_dir(dest.rsplit("\\", 1)[0])
        with open(src, "rb") as srcf, self._client.open_file(dest, "wb") as dstf:
            shutil.copyfileobj(srcf, dstf, 1024 * 1024)

    def list_recordings(self, day: str, cam: str) -> List[str]:
        try:
            names = self._client.listdir(self.path(day, cam))
        except Exception:
            return []
        return sorted(n for n in names
                      if n.endswith(".mp4") and n.startswith(day + "_"))

    def open_recording(self, day: str, cam: str, fname: str):
        return self._client.open_file(self.path(day, cam, fname), "rb")

    def remove(self, path: str):
        self._client.remove(path)

    def stat_volume(self) -> dict:
        try:
            raw = self._client.stat_volume(self.path())
        except Exception:
            if not self._base:
                raise
            raw = self._client.stat_volume(f"\\\\{self._host}\\{self._share}")
        total = int(getattr(raw, "total_size", 0) or 0)
        actual_free = int(getattr(raw, "actual_available_size", 0) or 0)
        caller_free = int(getattr(raw, "caller_available_size", 0) or 0)
        free = actual_free if actual_free > 0 else caller_free
        return {
            "total": total,
            "free": max(0, min(free, total)) if total else max(0, free),
            "caller_free": max(0, caller_free),
            "used": max(0, total - free) if total else 0,
        }

    def probe(self):
        if self._base:
            self.ensure_dir(self.path())
        remote = self.path(".litenvr_write_test")
        with self._client.open_file(remote, "wb") as f:
            f.write(b"ok")
        self._client.remove(remote)

    def close(self):
        try:
            self._client.delete_session(self._host, port=self._port)
        except Exception:
            pass


def _smb_target_for_playback() -> Optional[SmbTarget]:
    """Trả về kết nối SMB nếu backup đang cấu hình SMB; None nếu chưa dùng SMB."""
    cfg = _backup_cfg()
    if str(cfg.get("kind") or "") != "smb":
        return None
    if not (cfg.get("smb_host") or "").strip() or not (cfg.get("smb_share") or "").strip():
        return None
    return SmbTarget(cfg)


def _smb_list_recordings(cam: str, day: str) -> List[str]:
    target = None
    try:
        target = _smb_target_for_playback()
        if not target:
            return []
        return target.list_recordings(day, os.path.basename(cam))
    except Exception as e:
        log_event("warn", f"Không đọc được danh sách bản ghi SMB: {mask_secrets(str(e))}")
        return []
    finally:
        if target:
            target.close()


def backup_scan() -> List[str]:
    """Danh sách bản ghi (cam_id/fname) chưa sao lưu, đủ cũ và không bị khoá."""
    cfg = _backup_cfg()
    if not cfg.get("enabled"):
        return []
    done = _backup_done()
    locks = locked_paths()
    cams = sorted(_backup_effective_cameras())
    since = float(cfg.get("since_ts", 0) or 0)
    now = time.time()
    drive_pending = _drive_pending_paths() if drive_connected() else set()
    out: List[str] = []
    for cam in cams:
        d = os.path.join(RECORDINGS_DIR, cam)
        if not os.path.isdir(d):
            continue
        uploading_drive = cam_uploads_drive(cam)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".mp4"):
                continue
            rel = f"{cam}/{fn}"
            if rel in done or rel in locks:
                continue
            if uploading_drive and rel in drive_pending:
                continue
            try:
                st = os.stat(os.path.join(d, fn))
            except OSError:
                continue
            if now - st.st_mtime < 30 or (since and st.st_mtime < since):
                continue
            out.append(rel)
    return out


def _ftp_connect(cfg: dict):
    import ftplib
    c = ftplib.FTP_TLS() if cfg.get("ftp_tls") else ftplib.FTP()
    c.connect(cfg.get("ftp_host", ""), int(cfg.get("ftp_port") or 21), timeout=15)
    c.login(cfg.get("ftp_user", ""), cfg.get("ftp_pass", ""))
    if cfg.get("ftp_tls"):
        c.prot_p()
    return c


def _ftp_makedirs(ftp, path: str):
    cur = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        cur += "/" + part
        try:
            ftp.mkd(cur)
        except Exception:
            pass


def _backup_file(rel: str, ftp=None, smb=None):
    """Sao lưu một bản ghi (cam_id/fname) ra đích; ghi nhận vào backup_done."""
    cfg = _backup_cfg()
    src = os.path.join(RECORDINGS_DIR, rel)
    try:
        mtime = os.path.getmtime(src)
    except OSError:
        mtime = time.time()
    day, cam, fn = _backup_dest_parts(rel, mtime)
    kind = cfg.get("kind", "folder")
    own_ftp = False
    own_smb = False
    try:
        if kind == "folder":
            root = (cfg.get("folder_path") or "").strip()
            if not root or not os.path.isdir(root):
                raise RuntimeError("thư mục đích không tồn tại")
            dest_dir = os.path.join(root, day, cam)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, fn)
            tmp = dest + ".part"
            try:
                shutil.copy2(src, tmp)
                if os.path.getsize(tmp) != os.path.getsize(src):
                    raise RuntimeError("sao lưu thiếu dữ liệu")
                os.replace(tmp, dest)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        elif kind == "ftp":
            if ftp is None:
                ftp = _ftp_connect(cfg)
                own_ftp = True
            base = (cfg.get("ftp_dir") or "/").strip("/")
            remote_dir = "/".join(p for p in (base, day, cam) if p)
            _ftp_makedirs(ftp, "/" + remote_dir)
            with open(src, "rb") as f:
                ftp.storbinary(f"STOR /{remote_dir}/{fn}", f)
        elif kind == "smb":
            if smb is None:
                smb = SmbTarget(cfg)
                own_smb = True
            smb.upload(src, smb.path(day, cam, fn))
        else:
            raise RuntimeError("kiểu sao lưu không hợp lệ")
    finally:
        if own_ftp and ftp:
            try:
                ftp.quit()
            except Exception:
                pass
        if own_smb and smb:
            smb.close()
    conn = db_conn()
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    conn.execute(
        "INSERT OR REPLACE INTO backup_done (path, cam_id, size, ts, dest, deleted, deleted_ts) "
        "VALUES (?,?,?,?,?,0,NULL)",
        (rel, cam, size, time.time(), kind))
    conn.commit()
    cam_cfg = CONFIG.get("cameras", {}).get(cam) or {}
    if cfg.get("mode") == "move" or cam_cfg.get("storage_smb_move"):
        try:
            os.remove(src)
        except OSError:
            pass


_smb_cleanup_state = {"last_cleanup": 0.0, "last_result": {}}


def _backup_recording_ts(rel: str, fallback_ts: float = 0) -> float:
    day = _recording_day(rel.split("/", 1)[-1], fallback_ts)
    try:
        return time.mktime(time.strptime(day, "%Y-%m-%d"))
    except (TypeError, ValueError, OverflowError):
        return float(fallback_ts or 0)


def smb_cleanup(force: bool = False, target=None) -> dict:
    """Xoá bản ghi cũ do LiteNVR đã sao lưu lên SMB khi vượt giới hạn."""
    cfg = _backup_cfg()
    now = time.time()
    if str(cfg.get("kind") or "") != "smb":
        return {"deleted": 0, "freed": 0, "reason": "not_smb"}
    if not cfg.get("enabled"):
        return {"deleted": 0, "freed": 0, "reason": "disabled"}
    if not force and not cfg.get("smb_cleanup_enabled"):
        return {"deleted": 0, "freed": 0, "reason": "disabled"}
    if not (cfg.get("smb_host") or "").strip() or not (cfg.get("smb_share") or "").strip():
        return {"deleted": 0, "freed": 0, "reason": "not_configured"}
    if not force and now - float(_smb_cleanup_state.get("last_cleanup") or 0) < 600:
        return {"deleted": 0, "freed": 0, "reason": "wait"}

    try:
        retention_days = max(0.0, float(cfg.get("smb_retention_days") or 0))
        max_gb = max(0.0, float(cfg.get("smb_max_gb") or 0))
        min_free_gb = max(0.0, float(cfg.get("smb_min_free_gb") or 0))
    except (TypeError, ValueError):
        retention_days = max_gb = 0.0
        min_free_gb = 2.0
    if retention_days <= 0 and max_gb <= 0 and min_free_gb <= 0:
        _smb_cleanup_state["last_cleanup"] = now
        return {"deleted": 0, "freed": 0, "reason": "no_limits"}

    own_target = target is None
    if own_target:
        target = SmbTarget(cfg)
    conn = db_conn()
    try:
        try:
            vol = target.stat_volume()
        except Exception as e:
            if force:
                raise
            log_event("warn", f"SMB cleanup không đọc được dung lượng: {mask_secrets(str(e))}")
            _smb_cleanup_state["last_cleanup"] = now
            return {"deleted": 0, "freed": 0, "reason": "space_error"}
        total = int(vol.get("total") or 0)
        free = int(vol.get("free") or 0)
        rows = list(conn.execute(
            "SELECT path, cam_id, size, ts FROM backup_done "
            "WHERE dest='smb' AND COALESCE(deleted, 0)=0").fetchall())
        rows.sort(key=lambda r: (_backup_recording_ts(r["path"], r["ts"]), float(r["ts"] or 0)))
        tracked_used = sum(int(r["size"] or 0) for r in rows)
        max_bytes = max_gb * (1024 ** 3) if max_gb > 0 else 0
        min_free_bytes = min_free_gb * (1024 ** 3) if min_free_gb > 0 else 0
        locked = locked_paths()
        deleted = 0
        freed = 0
        for row in rows:
            if deleted >= 300:
                break
            rel = row["path"]
            if rel in locked:
                continue
            uploaded_ts = float(row["ts"] or 0)
            if now - uploaded_ts < 120:
                continue
            rec_ts = _backup_recording_ts(rel, uploaded_ts)
            age_days = max(0.0, (now - rec_ts) / 86400.0) if rec_ts else 0.0
            over_days = retention_days > 0 and age_days > retention_days
            over_gb = max_bytes > 0 and tracked_used > max_bytes
            low_free = min_free_bytes > 0 and total > 0 and free < min_free_bytes
            if not (over_days or over_gb or low_free):
                break
            try:
                day, cam, fn = _backup_dest_parts(rel, uploaded_ts)
                target.remove(target.path(day, cam, fn))
            except Exception as e:
                log_event("warn", f"SMB cleanup không xoá được {rel}: {mask_secrets(str(e))}")
                continue
            size = int(row["size"] or 0)
            conn.execute("UPDATE backup_done SET deleted=1, deleted_ts=? WHERE path=?", (now, rel))
            tracked_used = max(0, tracked_used - size)
            if total:
                free = min(total, free + size)
            else:
                free += size
            deleted += 1
            freed += size
        conn.commit()
        result = {
            "deleted": deleted,
            "freed": freed,
            "reason": "cleaned" if deleted else "ok",
            "total": total,
            "free": free,
            "used": tracked_used,
        }
        _smb_cleanup_state["last_cleanup"] = now
        _smb_cleanup_state["last_result"] = result
        return result
    finally:
        if own_target and target:
            target.close()


def backup_worker():
    while not _shutdown.is_set():
        try:
            cfg = _backup_cfg()
            kind = str(cfg.get("kind") or "folder")
            cleanup_due = (
                bool(cfg.get("enabled")) and kind == "smb"
                and bool(cfg.get("smb_cleanup_enabled"))
                and time.time() - float(_smb_cleanup_state.get("last_cleanup") or 0) >= 600
            )
            if cfg.get("enabled") and (cfg.get("folder_path") or cfg.get("ftp_host") or cfg.get("smb_host")):
                files = backup_scan()
                ftp = None
                smb = None
                if kind == "ftp" and files:
                    try:
                        ftp = _ftp_connect(cfg)
                    except Exception as e:
                        log_event("warn", f"Backup FTP kết nối lỗi: {mask_secrets(str(e))}")
                        files = []
                elif kind == "smb" and (files or cleanup_due):
                    try:
                        smb = SmbTarget(cfg)
                    except Exception as e:
                        log_event("warn", f"Backup SMB kết nối lỗi: {mask_secrets(str(e))}")
                        files = []
                        cleanup_due = False
                fail_streak = 0
                for rel in files[:50]:
                    if _shutdown.is_set():
                        break
                    try:
                        _backup_file(rel, ftp, smb)
                        fail_streak = 0
                    except Exception as e:
                        log_event("warn", f"Sao lưu lỗi {rel}: {mask_secrets(str(e))}")
                        fail_streak += 1
                        if fail_streak >= 3:
                            break  # lỗi hệ thống (kết nối/quyền), tránh spam cả batch
                if smb and cleanup_due:
                    try:
                        smb_cleanup(force=False, target=smb)
                    except Exception as e:
                        log_event("warn", f"SMB cleanup: {mask_secrets(str(e))}")
                if ftp:
                    try:
                        ftp.quit()
                    except Exception:
                        pass
                if smb:
                    smb.close()
        except Exception as e:
            log_event("warn", f"backup_worker: {mask_secrets(str(e))}")
        _shutdown.wait(60)


@app.get("/api/backup")
def get_backup_settings():
    b = _backup_cfg()
    try:
        done = db_conn().execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM backup_done "
            "WHERE COALESCE(deleted, 0)=0").fetchone()
        smb_done = db_conn().execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM backup_done "
            "WHERE dest='smb' AND COALESCE(deleted, 0)=0").fetchone()
    except Exception:
        done = (0, 0)
        smb_done = (0, 0)
    return {
        "enabled": bool(b.get("enabled")),
        "kind": b.get("kind", "folder"),
        "folder_path": b.get("folder_path", ""),
        "ftp_host": b.get("ftp_host", ""), "ftp_port": int(b.get("ftp_port") or 21),
        "ftp_user": b.get("ftp_user", ""), "has_ftp_pass": bool((b.get("ftp_pass") or "").strip()),
        "ftp_dir": b.get("ftp_dir", "/"), "ftp_tls": bool(b.get("ftp_tls")),
        "smb_host": b.get("smb_host", ""), "smb_port": int(b.get("smb_port") or 445),
        "smb_share": b.get("smb_share", ""), "smb_user": b.get("smb_user", ""),
        "has_smb_pass": bool((b.get("smb_pass") or "").strip()),
        "smb_domain": b.get("smb_domain", ""), "smb_dir": b.get("smb_dir", ""),
        "smb_cleanup_enabled": bool(b.get("smb_cleanup_enabled")),
        "smb_retention_days": float(b.get("smb_retention_days") or 0),
        "smb_max_gb": float(b.get("smb_max_gb") or 0),
        "smb_min_free_gb": float(b.get("smb_min_free_gb") or 0),
        "smb_backup_count": smb_done[0], "smb_backup_bytes": smb_done[1],
        "smb_last_cleanup": float(_smb_cleanup_state.get("last_cleanup") or 0),
        "mode": b.get("mode", "copy"),
        "cameras": b.get("cameras", []),
        "since_ts": float(b.get("since_ts", 0) or 0),
        "done_count": done[0], "done_bytes": done[1],
        "pending": len(backup_scan()),
    }


@app.post("/api/backup")
def update_backup_settings(settings: BackupSettings):
    b = CONFIG.setdefault("backup", dict(DEFAULT_CONFIG["backup"]))
    b["enabled"] = settings.enabled
    b["kind"] = settings.kind if settings.kind in ("folder", "ftp", "smb") else "folder"
    b["folder_path"] = settings.folder_path.strip()
    b["ftp_host"] = settings.ftp_host.strip()
    b["ftp_port"] = int(settings.ftp_port or 21)
    b["ftp_user"] = settings.ftp_user.strip()
    if settings.ftp_pass.strip():
        b["ftp_pass"] = settings.ftp_pass.strip()
    b["ftp_dir"] = settings.ftp_dir.strip() or "/"
    b["ftp_tls"] = settings.ftp_tls
    b["smb_host"] = settings.smb_host.strip()
    b["smb_port"] = int(settings.smb_port or 445)
    b["smb_share"] = settings.smb_share.strip()
    b["smb_user"] = settings.smb_user.strip()
    if settings.smb_pass.strip():
        b["smb_pass"] = settings.smb_pass.strip()
    b["smb_domain"] = settings.smb_domain.strip()
    b["smb_dir"] = settings.smb_dir.strip()
    b["smb_cleanup_enabled"] = bool(settings.smb_cleanup_enabled)
    b["smb_retention_days"] = max(0.0, float(settings.smb_retention_days or 0))
    b["smb_max_gb"] = max(0.0, float(settings.smb_max_gb or 0))
    b["smb_min_free_gb"] = max(0.0, float(settings.smb_min_free_gb or 0))
    b["mode"] = settings.mode if settings.mode in ("copy", "move") else "copy"
    b["cameras"] = [str(x) for x in (settings.cameras or []) if str(x)]
    b["cameras_explicit"] = bool(b["cameras"])
    b["since_ts"] = float(settings.since_ts or 0)
    save_config(CONFIG)
    log_event("info", "Đã lưu cấu hình lưu trữ ngoài SMB/NFS/FTP")
    return {"status": "ok"}


@app.post("/api/backup/test")
def test_backup_destination():
    cfg = _backup_cfg()
    kind = cfg.get("kind", "folder")
    if kind == "folder":
        root = (cfg.get("folder_path") or "").strip()
        if not root:
            raise HTTPException(status_code=400, detail="Chưa nhập thư mục đích")
        if not os.path.isdir(root):
            raise HTTPException(status_code=400, detail="Thư mục đích không tồn tại trong container (đã mount chưa?)")
        probe = os.path.join(root, ".litenvr_write_test")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Không ghi được vào thư mục đích: {e}")
        return {"status": "ok", "results": {"folder": "ok"}}
    if kind == "ftp":
        if not cfg.get("ftp_host"):
            raise HTTPException(status_code=400, detail="Chưa nhập máy chủ FTP")
        try:
            ftp = _ftp_connect(cfg)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Không kết nối được FTP: {mask_secrets(str(e))}")
        try:
            ftp.quit()
        except Exception:
            pass
        return {"status": "ok", "results": {"ftp": "ok"}}
    if kind == "smb":
        if not cfg.get("smb_host") or not cfg.get("smb_share"):
            raise HTTPException(status_code=400, detail="Chưa nhập máy chủ hoặc share SMB")
        smb = None
        try:
            smb = SmbTarget(cfg)
            smb.probe()
            space = smb.stat_volume()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Không ghi được vào SMB: {mask_secrets(str(e))}")
        finally:
            if smb:
                smb.close()
        return {"status": "ok", "results": {"smb": "ok"}, "smb_space": space}
    raise HTTPException(status_code=400, detail="Kiểu sao lưu không hợp lệ")


@app.get("/api/backup/smb/space")
def get_smb_space():
    cfg = _backup_cfg()
    if str(cfg.get("kind") or "") != "smb":
        raise HTTPException(status_code=400, detail="Chưa chọn kiểu lưu trữ SMB")
    if not (cfg.get("smb_host") or "").strip() or not (cfg.get("smb_share") or "").strip():
        raise HTTPException(status_code=400, detail="Chưa nhập máy chủ hoặc share SMB")
    smb = None
    try:
        smb = SmbTarget(cfg)
        return {"status": "ok", **smb.stat_volume()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không đọc được dung lượng SMB: {mask_secrets(str(e))}")
    finally:
        if smb:
            smb.close()


@app.post("/api/backup/smb/cleanup")
def cleanup_smb_now():
    cfg = _backup_cfg()
    if str(cfg.get("kind") or "") != "smb":
        raise HTTPException(status_code=400, detail="Chưa chọn kiểu lưu trữ SMB")
    if not (cfg.get("smb_host") or "").strip() or not (cfg.get("smb_share") or "").strip():
        raise HTTPException(status_code=400, detail="Chưa nhập máy chủ hoặc share SMB")
    try:
        result = smb_cleanup(force=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không dọn được SMB: {mask_secrets(str(e))}")
    return {"status": "ok", **result}


# ---------------- Sao lưu Google Drive (24/7) ----------------
# Kiến trúc: quét thư mục bản ghi -> hàng đợi SQLite (drive_uploads) -> luồng nền
# upload resumable (có giới hạn tốc độ) -> kiểm tra quota định kỳ -> khi Drive gần
# đầy (hoặc vượt max_gb) thì xoá VĨNH VIỄN file cũ nhất đã tải lên để dọn chỗ.

REC_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$")

drive_state = {
    "paused": False, "reason": "", "retry_at": 0.0,
    "last_scan": 0.0, "last_cleanup": 0.0,
    "auth_error": "", "quota": None, "quota_ts": 0.0,
}
drive_pending_auth = {}  # phiên xác thực thiết bị đang chờ (device flow)
_drive_token_cache = {"token": "", "exp": 0.0}
_drive_notify_last = {"t": 0.0}


class DriveHttpError(Exception):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {json.dumps(payload, ensure_ascii=False)[:300]}")


class DriveAuthError(Exception):
    pass


def gd_cfg() -> dict:
    g = CONFIG.setdefault("gdrive", dict(DEFAULT_CONFIG["gdrive"]))
    for k, v in DEFAULT_CONFIG["gdrive"].items():
        g.setdefault(k, v)
    return g


def drive_connected() -> bool:
    g = gd_cfg()
    return bool((g.get("client_id") or "").strip() and (g.get("client_secret") or "").strip()
                and (g.get("refresh_token") or "").strip())


def cam_drive_target(cam_id: str) -> str:
    """Nơi lưu của camera: hdd | drive | both (thiếu = both để giữ hành vi cũ)."""
    cam = CONFIG.get("cameras", {}).get(cam_id) or {}
    t = str(cam.get("storage_target") or "both")
    return t if t in ("hdd", "drive", "both") else "both"


def cam_uploads_drive(cam_id: str) -> bool:
    """Camera này có được tải bản ghi lên Drive không (theo cài đặt Drive + camera)."""
    g = gd_cfg()
    if not g.get("enabled") or not drive_connected():
        return False
    cams_filter = g.get("cameras") or []
    if cams_filter and cam_id not in cams_filter:
        return False
    return cam_drive_target(cam_id) in ("drive", "both")


def drive_notify(text: str, force: bool = False):
    """Gửi cảnh báo Drive qua Telegram (nếu bật), tối đa 1 tin/phút để tránh spam."""
    now = time.time()
    if not force and now - _drive_notify_last["t"] < 60:
        return
    _drive_notify_last["t"] = now
    try:
        nt = CONFIG.get("notify") or {}
        if nt.get("telegram_enabled") and nt.get("telegram_token") and nt.get("telegram_chat_id"):
            _telegram_send(nt["telegram_token"], str(nt["telegram_chat_id"]), "☁️ LiteNVR Drive: " + text)
    except Exception:
        pass


def _http_json(url: str, data: Any = None, headers: Optional[dict] = None,
               method: Optional[str] = None, timeout: int = 30) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json; charset=UTF-8", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")[:400]}
        raise DriveHttpError(e.code, payload)


def _form_post(url: str, fields: dict, timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")[:400]}
        raise DriveHttpError(e.code, payload)


def drive_access_token(force: bool = False) -> str:
    """Đổi refresh_token lấy access_token (cache trong RAM tới khi gần hết hạn)."""
    now = time.time()
    if not force and _drive_token_cache["token"] and now < _drive_token_cache["exp"]:
        return _drive_token_cache["token"]
    if not drive_connected():
        raise DriveAuthError("Chưa kết nối Google Drive")
    g = gd_cfg()
    try:
        j = _form_post(DRIVE_OAUTH_BASE + "/token", {
            "client_id": g["client_id"], "client_secret": g["client_secret"],
            "refresh_token": g["refresh_token"], "grant_type": "refresh_token"})
    except DriveHttpError as e:
        err = str((e.payload or {}).get("error") or e.payload)
        raise DriveAuthError(f"Token bị từ chối ({err}) — cần kết nối lại Google Drive")
    tok = j.get("access_token") or ""
    if not tok:
        raise DriveAuthError("Google không trả access_token")
    _drive_token_cache["token"] = tok
    _drive_token_cache["exp"] = now + max(60, int(j.get("expires_in", 3600)) - 120)
    drive_state["auth_error"] = ""
    return tok


def drive_api(method: str, path: str, data: Any = None, retry_auth: bool = True) -> Any:
    """Gọi Drive REST API (path bắt đầu bằng /...). Tự thử lại 1 lần sau khi refresh token."""
    url = DRIVE_API_BASE + path
    try:
        return _http_json(url, data=data, method=method,
                          headers={"Authorization": "Bearer " + drive_access_token()})
    except DriveHttpError as e:
        if e.status == 401 and retry_auth:
            drive_access_token(force=True)
            return drive_api(method, path, data, retry_auth=False)
        raise


def drive_quota(use_cache: bool = True) -> Optional[dict]:
    now = time.time()
    if use_cache and drive_state["quota"] and now - drive_state["quota_ts"] < 600:
        return drive_state["quota"]
    try:
        j = drive_api("GET", "/about?fields=storageQuota")
        q = j.get("storageQuota") or {}
        info = {"limit": int(q.get("limit", 0) or 0), "usage": int(q.get("usage", 0) or 0)}
        info["free"] = max(0, info["limit"] - info["usage"]) if info["limit"] else 0
        drive_state["quota"] = info
        drive_state["quota_ts"] = now
        return info
    except DriveAuthError:
        raise
    except Exception:
        return drive_state["quota"]


def drive_ensure_folder(name: str, parent_id: str = "") -> str:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    j = drive_api("GET", "/files?" + urllib.parse.urlencode({
        "q": q, "fields": "files(id,name)", "pageSize": "5",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}))
    files = j.get("files") or []
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    j = drive_api("POST", "/files?fields=id&supportsAllDrives=true", data=meta)
    return j.get("id") or ""


def drive_root_folder() -> str:
    g = gd_cfg()
    root_id = (g.get("root_folder_id") or "").strip()
    if not root_id:
        root_id = drive_ensure_folder((g.get("folder_name") or "LiteNVR").strip() or "LiteNVR")
        g["root_folder_id"] = root_id
        save_config(CONFIG)
    return root_id


def drive_cam_folder(cam_id: str) -> str:
    """Đảm bảo có LiteNVR/<tên camera>/ trên Drive; lưu id vào config để tái sử dụng."""
    g = gd_cfg()
    cams = CONFIG.get("cameras", {})
    root_id = drive_root_folder()
    if not cam_id:
        return root_id
    cam_name = (cams.get(cam_id) or {}).get("name") or cam_id
    cmap = g.setdefault("cam_folders", {})
    fid = (cmap.get(cam_id) or "").strip()
    if not fid:
        fid = drive_ensure_folder(cam_name, root_id)
        cmap[cam_id] = fid
        save_config(CONFIG)
    return fid


_drive_folder_lock = threading.Lock()

def drive_upload_folder(cam_id: str, day: str) -> str:
    with _drive_folder_lock:
        root_id = drive_root_folder()
        day_id = drive_ensure_folder(day, root_id)
        if not cam_id:
            return day_id
        cam_name = (CONFIG.get("cameras", {}).get(cam_id) or {}).get("name") or cam_id
        return drive_ensure_folder(cam_name, day_id)


def drive_upload_file(local_path: str, name: str, parent_id: str, kbps: int = 0) -> dict:
    """Upload resumable từng phần (chunk). Giới hạn tốc độ kbps: 0 = không giới hạn."""
    token = drive_access_token()
    size = os.path.getsize(local_path)
    meta = {"name": name, "parents": [parent_id]}
    req = urllib.request.Request(
        DRIVE_UPLOAD_BASE + "/files?uploadType=resumable&supportsAllDrives=true",
        data=json.dumps(meta).encode(), method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            session_url = r.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")[:400]}
        raise DriveHttpError(e.code, payload)
    if not session_url:
        raise DriveHttpError(500, {"error": "Google không trả URL phiên upload"})

    chunk_size = DRIVE_CHUNK if kbps <= 0 else max(262144, min(int(kbps * 125 * 5), 32 * 1024 * 1024))
    pos = 0
    with open(local_path, "rb") as f:
        while pos < size:
            chunk = f.read(min(chunk_size, size - pos))
            if not chunk:
                break
            end = pos + len(chunk)
            if kbps > 0:
                time.sleep(len(chunk) / max(1, kbps * 125))
            for attempt in range(4):
                try:
                    req = urllib.request.Request(
                        session_url, data=chunk, method="PUT",
                        headers={"Content-Range": f"bytes {pos}-{end - 1}/{size}",
                                 "Content-Length": str(len(chunk))})
                    with urllib.request.urlopen(req, timeout=180) as r:
                        raw = r.read()
                        return json.loads(raw) if raw else {}
                except urllib.error.HTTPError as e:
                    if e.code == 308:  # chưa xong, Google đã nhận chunk
                        break
                    if e.code >= 500 and attempt < 3:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raw = e.read()
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {"raw": raw.decode("utf-8", "replace")[:400]}
                    raise DriveHttpError(e.code, payload)
            pos = end
    # Hiếm khi tới đây (file thay đổi kích thước giữa chừng) — hỏi trạng thái cuối
    req = urllib.request.Request(session_url, data=b"", method="PUT",
                                 headers={"Content-Range": f"bytes */{size}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")[:400]}
        raise DriveHttpError(e.code, payload)


def drive_delete_file(file_id: str) -> bool:
    """Xoá vĩnh viễn (không vào thùng rác) — giải phóng dung lượng Drive ngay."""
    if not file_id:
        return False
    try:
        drive_api("DELETE", f"/files/{urllib.parse.quote(file_id)}?supportsAllDrives=true")
        return True
    except DriveHttpError as e:
        if e.status == 404:
            return True  # đã bị xoá từ trước
        raise


# --- Tải bản ghi từ Drive về ổ cứng để xem lại (tua/seek) ---
_drive_fetch_lock = threading.Lock()
_drive_fetching: Dict[str, threading.Event] = {}   # "cam/fname" -> Event báo tải xong
_drive_fetch_prog: Dict[str, dict] = {}            # "cam/fname" -> tiến độ tải

def drive_download_file(file_id: str, dest_path: str, kbps: int, prog: dict) -> int:
    """Tải 1 file từ Drive về dest_path (ghi qua .part rồi đổi tên). kbps: 0 = không giới hạn.
    Nếu lần thử trước bị ngắt giữa chừng, lần sau tải tiếp bằng Range thay vì tải lại từ đầu."""
    token = drive_access_token()
    url = (DRIVE_API_BASE + "/files/" + urllib.parse.quote(file_id)
           + "?alt=media&supportsAllDrives=true")
    tmp = dest_path + ".part"
    last_err: Exception = DriveHttpError(500, {"error": "Tải thất bại"})
    for attempt in range(8):
        resume_from = 0
        try:
            if os.path.isfile(tmp):
                resume_from = os.path.getsize(tmp)
        except OSError:
            resume_from = 0
        try:
            headers = {"Authorization": "Bearer " + token}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
            req = urllib.request.Request(url, headers=headers)
            # timeout ngắn để phát hiện kết nối Google bị treo rồi tải tiếp, thay vì đứng im
            with urllib.request.urlopen(req, timeout=20) as r:
                if resume_from > 0 and getattr(r, "status", 200) != 206:
                    resume_from = 0  # server bỏ qua Range -> phải tải lại từ đầu
                if resume_from == 0:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                total = 0
                cr = r.headers.get("Content-Range") or ""
                m = re.match(r"bytes \d+-(\d+)/(\d+)", cr)
                if m:
                    total = int(m.group(2))
                if not total:
                    cl = int(r.headers.get("Content-Length") or 0)
                    total = (cl + resume_from) if cl else int(prog.get("total") or 0)
                done = resume_from
                prog["total"], prog["done"] = total, done
                with open(tmp, "ab" if resume_from else "wb") as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        prog["done"] = done
                        if kbps > 0:
                            time.sleep(len(chunk) / max(1, kbps * 125))
            if total and done < total:
                # http.client trả dữ liệu ngắn khi kết nối đứt mà không báo lỗi:
                # phải kiểm tra đủ dung lượng, nếu không sẽ coi file cụt là hoàn chỉnh.
                raise DriveHttpError(500, {"error": "Kết nối tới Drive bị ngắt giữa chừng"})
            os.replace(tmp, dest_path)
            return done
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 416:  # .part không dùng được nữa -> tải lại từ đầu
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt < 7:
                    time.sleep(1)
                    continue
                raise DriveHttpError(416, {"error": "Tải bản ghi từ Drive thất bại"})
            if e.code == 401 and attempt == 0:
                token = drive_access_token(force=True)
                continue
            if e.code >= 500 and attempt < 7:
                time.sleep(2 * (attempt + 1))
                continue
            raise DriveHttpError(e.code, {"error": "Tải bản ghi từ Drive thất bại"})
        except Exception as e:
            last_err = e
            if attempt < 7:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise DriveHttpError(500, {"error": str(last_err)})


def _drive_fetch_job(key: str, cam: str, name: str, file_id: str, path: str):
    prog = _drive_fetch_prog.setdefault(key, {})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Tải về để xem lại luôn chạy hết tốc độ (giới hạn băng thông chỉ dành cho tải lên)
        drive_download_file(file_id, path, 0, prog)
        prog["state"] = "done"
    except Exception as e:
        prog["state"] = "error"
        prog["error"] = f"Lỗi tải bản ghi từ Drive: {e}"
        log_event("warn", prog["error"], cam)
    finally:
        with _drive_fetch_lock:
            ev = _drive_fetching.pop(key, None)
        if ev:
            ev.set()


def _start_drive_fetch(cam: str, name: str, drive_id: str, path: str) -> threading.Event:
    """Bắt đầu tải 1 bản ghi từ Drive (nếu chưa chạy). Trả Event báo khi xong."""
    key = f"{cam}/{name}"
    with _drive_fetch_lock:
        ev = _drive_fetching.get(key)
        if ev is None:
            ev = threading.Event()
            _drive_fetching[key] = ev
            _drive_fetch_prog[key] = {"state": "downloading", "done": 0, "total": 0, "error": ""}
            threading.Thread(target=_drive_fetch_job,
                             args=(key, cam, name, drive_id, path), daemon=True).start()
    return ev


def ensure_local_recording(cam_id: str, fname: str, timeout: float = 1800) -> Optional[str]:
    """Trả về đường dẫn file bản ghi trên ổ cứng; nếu chỉ còn trên Drive thì tải về trước.
    Trả None nếu không có bản ghi này ở đâu cả."""
    cam = os.path.basename(cam_id)
    name = os.path.basename(fname)
    path = os.path.join(RECORDINGS_DIR, cam, name)
    if os.path.isfile(path):
        return path
    try:
        row = db_conn().execute(
            "SELECT drive_id FROM drive_uploads WHERE path=? AND status='done' AND drive_id<>''",
            (f"{cam}/{name}",)).fetchone()
    except Exception:
        row = None
    if not row or not row[0]:
        return None
    key = f"{cam}/{name}"
    ev = _start_drive_fetch(cam, name, row[0], path)
    if not ev.wait(timeout):
        raise HTTPException(status_code=504, detail="Tải bản ghi từ Drive quá lâu, vui lòng thử lại")
    if os.path.isfile(path):
        return path
    err = (_drive_fetch_prog.get(key) or {}).get("error") or "Không tải được bản ghi từ Drive"
    raise HTTPException(status_code=502, detail=err)


def _rec_name_ts(name: str) -> Optional[float]:
    m = REC_NAME_RE.match(name)
    if not m:
        return None
    try:
        y, mo, d, h, mi, s = [int(x) for x in m.groups()]
        return datetime(y, mo, d, h, mi, s).timestamp()
    except Exception:
        return None


def drive_scan_once():
    """Thêm các file bản ghi hoàn chỉnh chưa có trong hàng đợi (bỏ file đang ghi)."""
    since = float(gd_cfg().get("since_ts") or 0)
    now = time.time()
    conn = db_conn()
    added = 0
    for cam_id in list(CONFIG.get("cameras", {}).keys()):
        if not cam_uploads_drive(cam_id):
            continue
        cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
        if not os.path.isdir(cam_dir):
            continue
        try:
            names = sorted(f for f in os.listdir(cam_dir) if REC_NAME_RE.match(f))
        except Exception:
            continue
        for name in names:
            fpath = os.path.join(cam_dir, name)
            try:
                st = os.stat(fpath)
            except Exception:
                continue
            if now - st.st_mtime < 10:
                continue  # vừa chốt xong, chờ ổn định
            ts = _rec_name_ts(name) or st.st_mtime
            if ts < since:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO drive_uploads (path, cam_id, name, ts, size, status, tries, next_try, updated) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?)",
                (f"{cam_id}/{name}", cam_id, name, ts, st.st_size, now))
            added += cur.rowcount
    if added:
        conn.commit()
    return added


def drive_queue_backfill(cam_id: str, date_from: str, date_to: str) -> int:
    """Đưa bản ghi cũ (theo khoảng ngày) vào hàng đợi tải bù."""
    cams = [c for c in ([cam_id] if cam_id else CONFIG.get("cameras", {})) if cam_uploads_drive(c)]
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Ngày không hợp lệ (định dạng YYYY-MM-DD)")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="Ngày kết thúc phải sau ngày bắt đầu")
    conn = db_conn()
    now = time.time()
    queued = 0
    for cid in cams:
        cam_dir = os.path.join(RECORDINGS_DIR, cid)
        if not os.path.isdir(cam_dir):
            continue
        try:
            names = sorted(f for f in os.listdir(cam_dir) if REC_NAME_RE.match(f))
        except Exception:
            continue
        for name in names:
            ts = _rec_name_ts(name)
            if ts is None:
                continue
            day = datetime.fromtimestamp(ts).date()
            if not (d_from <= day <= d_to):
                continue
            fpath = os.path.join(cam_dir, name)
            try:
                if now - os.path.getmtime(fpath) < 10:
                    continue  # có thể ffmpeg vẫn đang ghi
                size = os.path.getsize(fpath)
            except Exception:
                continue
            rel = f"{cid}/{name}"
            row = conn.execute("SELECT status FROM drive_uploads WHERE path=?", (rel,)).fetchone()
            if row and row["status"] in ("pending", "uploading", "done"):
                continue  # đang chờ/đang lên/đã lên thì thôi
            conn.execute(
                "INSERT OR REPLACE INTO drive_uploads (path, cam_id, name, ts, size, status, error, drive_id, tries, next_try, updated) "
                "VALUES (?, ?, ?, ?, ?, 'pending', '', '', 0, 0, ?)",
                (rel, cid, name, ts, size, now))
            queued += 1
    conn.commit()
    return queued


def drive_cleanup(force: bool = False) -> dict:
    """Dọn Drive: xoá vĩnh viễn file cũ nhất khi vượt max_gb hoặc Drive sắp đầy."""
    g = gd_cfg()
    if not drive_connected():
        return {"deleted": 0, "freed": 0, "reason": "chưa kết nối"}
    conn = db_conn()
    used = int(conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM drive_uploads WHERE status='done'").fetchone()[0] or 0)
    max_gb = float(g.get("max_gb") or 0)
    min_free = max(200, int(g.get("min_free_mb") or 2000)) * 1024 * 1024
    target_used = max_gb * 1_000_000_000 if max_gb > 0 else None
    quota = drive_quota(use_cache=not force) or {}
    limit = int(quota.get("limit") or 0)
    free = int(quota.get("free") or 0) if limit else None
    ok_used = target_used is None or used <= target_used
    ok_free = free is None or free >= min_free
    if ok_used and ok_free:
        return {"deleted": 0, "freed": 0, "reason": "ok", "used": used,
                "free": free, "limit": limit}
    locked = locked_paths()
    rows = conn.execute(
        "SELECT path, name, cam_id, drive_id, size, ts FROM drive_uploads "
        "WHERE status='done' ORDER BY ts ASC").fetchall()
    deleted, freed = 0, 0
    now = time.time()
    for r in rows:
        if deleted >= 300:  # giới hạn an toàn mỗi lượt
            break
        if ok_used and ok_free:
            break
        if r["path"] in locked:
            continue
        try:
            drive_delete_file(r["drive_id"])
        except DriveAuthError:
            raise
        except Exception as e:
            log_event("error", f"Drive: không xoá được {r['name']}: {e}")
            continue
        size = int(r["size"] or 0)
        conn.execute("UPDATE drive_uploads SET status='deleted', updated=? WHERE path=?", (now, r["path"]))
        deleted += 1
        freed += size
        used -= size
        if free is not None:
            free += size
        ok_used = target_used is None or used <= target_used * 0.98
        ok_free = free is None or free >= min_free
    conn.commit()
    if deleted:
        # Giữ cache quota khớp thực tế (tránh dọn lặp vì số cũ)
        q = drive_state.get("quota")
        if q and q.get("limit"):
            q["usage"] = max(0, int(q.get("usage", 0)) - freed)
            q["free"] = max(0, int(q.get("limit", 0)) - q["usage"])
        drive_state["quota_ts"] = time.time()
    return {"deleted": deleted, "freed": freed, "reason": "cleaned",
            "used": used, "free": free, "limit": limit}


def drive_process_row(row) -> str:
    """Upload 1 file. Trả về trạng thái mới: done | pending | error | skipped."""
    conn = db_conn()
    now = time.time()
    g = gd_cfg()
    local_path = os.path.join(RECORDINGS_DIR, row["cam_id"], row["name"])
    if not os.path.exists(local_path):
        conn.execute("UPDATE drive_uploads SET status='skipped', error='file đã bị xoá khỏi ổ cứng', updated=? WHERE path=?",
                     (now, row["path"]))
        conn.commit()
        return "skipped"
    try:
        day = _recording_day(row["name"], float(row["ts"] or 0))
        folder_id = drive_upload_folder(row["cam_id"], day)
        size = os.path.getsize(local_path)
        kbps = int(g.get("bandwidth_kbps") or 0)
        parallel = max(1, min(DRIVE_MAX_PARALLEL, int(g.get("parallel_uploads") or 1)))
        if kbps > 0 and parallel > 1:
            kbps = max(64, kbps // parallel)
        res = drive_upload_file(local_path, row["name"], folder_id, kbps)
        conn.execute(
            "UPDATE drive_uploads SET status='done', error='', drive_id=?, size=?, tries=0, next_try=0, updated=? WHERE path=?",
            (res.get("id") or "", size, now, row["path"]))
        conn.commit()
        if cam_drive_target(row["cam_id"]) == "drive" and row["path"] not in locked_paths():
            # Camera chỉ lưu trên Drive: xoá bản gốc trên ổ cứng sau khi tải xong
            try:
                os.remove(local_path)
            except Exception:
                pass
        q = drive_state.get("quota")
        if q and q.get("limit"):
            q["usage"] = int(q.get("usage", 0)) + size
            q["free"] = max(0, int(q.get("limit", 0)) - q["usage"])
        return "done"
    except DriveAuthError as e:
        drive_state["auth_error"] = str(e)
        drive_state["paused"] = True
        drive_state["reason"] = "Lỗi xác thực Google — cần kết nối lại"
        drive_state["retry_at"] = now + 3600
        drive_notify(str(e) + " — vào LiteNVR → Google Drive để kết nối lại.")
        conn.execute("UPDATE drive_uploads SET status='pending', updated=? WHERE path=?", (now, row["path"]))
        conn.commit()
        return "pending"
    except DriveHttpError as e:
        payload = json.dumps(e.payload, ensure_ascii=False)
        if e.status == 403 and ("quota" in payload or "storageQuota" in payload):
            drive_state["paused"] = True
            drive_state["reason"] = "Drive đã hết dung lượng — đang dọn file cũ"
            drive_state["retry_at"] = now + 300
            drive_notify("Drive hết dung lượng, LiteNVR sẽ dọn file cũ nhất rồi tiếp tục tải lên.")
            conn.execute("UPDATE drive_uploads SET status='pending', updated=? WHERE path=?", (now, row["path"]))
            conn.commit()
            return "pending"
        return drive_mark_retry(conn, row, now, f"HTTP {e.status}: {payload[:200]}")
    except Exception as e:
        return drive_mark_retry(conn, row, now, str(e)[:200])


def drive_mark_retry(conn, row, now: float, err: str) -> str:
    tries = int(row["tries"] or 0) + 1
    if tries >= 5:
        conn.execute("UPDATE drive_uploads SET status='error', error=?, tries=?, updated=? WHERE path=?",
                     (err, tries, now, row["path"]))
        conn.commit()
        return "error"
    conn.execute("UPDATE drive_uploads SET status='pending', error=?, tries=?, next_try=?, updated=? WHERE path=?",
                 (err, tries, now + min(DRIVE_RETRY_BASE * (2 ** tries), 1800), now, row["path"]))
    conn.commit()
    return "pending"


_drive_inflight = {}
_drive_inflight_lock = threading.Lock()

def _drive_upload_thread(row):
    try:
        drive_process_row(row)
    except Exception as e:
        log_event("error", f"Drive upload lỗi: {str(e)[:200]}")
        try:
            conn = db_conn()
            conn.execute("UPDATE drive_uploads SET status='pending' WHERE path=? AND status='uploading'",
                         (row["path"],))
            conn.commit()
        except Exception:
            pass
    finally:
        with _drive_inflight_lock:
            _drive_inflight.pop(row["path"], None)

def drive_worker():
    while True:
        try:
            conn = db_conn()
            # Treo từ lần chạy trước (app bị restart giữa chừng) -> trả về hàng đợi
            conn.execute("UPDATE drive_uploads SET status='pending' WHERE status='uploading'")
            conn.commit()
            break
        except Exception:
            time.sleep(5)
    last_quota_check = 0.0
    while True:
        try:
            g = gd_cfg()
            if not g.get("enabled") or not drive_connected():
                time.sleep(DRIVE_TICK)
                continue
            now = time.time()
            if drive_state["paused"]:
                if now >= drive_state["retry_at"]:
                    drive_state["paused"] = False
                    drive_state["reason"] = ""
                else:
                    time.sleep(DRIVE_TICK)
                    continue
            with _drive_inflight_lock:
                active = len(_drive_inflight)
            parallel = max(1, min(DRIVE_MAX_PARALLEL, int(g.get("parallel_uploads") or 1)))
            if now - drive_state["last_scan"] >= DRIVE_TICK:
                drive_scan_once()
                drive_state["last_scan"] = now
            if active < parallel:
                row = db_conn().execute(
                    "SELECT * FROM drive_uploads WHERE status='pending' AND (next_try IS NULL OR next_try<=?) "
                    "ORDER BY ts ASC LIMIT 1", (now,)).fetchone()
                if row is not None:
                    cur = db_conn().execute(
                        "UPDATE drive_uploads SET status='uploading' WHERE path=? AND status='pending'",
                        (row["path"],))
                    db_conn().commit()
                    if cur.rowcount:
                        th = threading.Thread(target=_drive_upload_thread, args=(row,), daemon=True)
                        with _drive_inflight_lock:
                            _drive_inflight[row["path"]] = th
                        th.start()
                        continue
            if active:
                time.sleep(0.5)
                continue
            # Kiểm tra quota mỗi 5 phút (hoặc khi có lỗi vừa xử lý)
            if time.time() - last_quota_check > 300:
                last_quota_check = time.time()
                try:
                    drive_cleanup()
                except DriveAuthError:
                    pass
                except Exception:
                    pass
            # Hàng đợi trống: dọn định kỳ 10 phút/lần
            if now - drive_state["last_cleanup"] > 600:
                drive_state["last_cleanup"] = now
                try:
                    drive_cleanup()
                except DriveAuthError:
                    pass
                except Exception:
                    pass
            time.sleep(DRIVE_TICK)
        except DriveAuthError:
            time.sleep(DRIVE_TICK)
        except Exception as e:
            log_event("error", f"Drive worker lỗi: {str(e)[:200]}")
            time.sleep(DRIVE_TICK)


class DriveSettings(BaseModel):
    enabled: bool = False
    folder_name: str = "LiteNVR"
    cameras: List[str] = []
    bandwidth_kbps: int = 0
    parallel_uploads: int = 1
    max_gb: float = 0
    min_free_mb: int = 2000


class DriveConnect(BaseModel):
    client_id: str
    client_secret: str


class DriveToken(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""


class DriveNow(BaseModel):
    cam_id: str = ""
    date_from: str
    date_to: str = ""


def drive_counts() -> dict:
    rows = db_conn().execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS s FROM drive_uploads GROUP BY status").fetchall()
    out = {"pending": 0, "uploading": 0, "done": 0, "error": 0, "skipped": 0, "deleted": 0}
    bytes_map = {k: 0 for k in out}
    for r in rows:
        if r["status"] in out:
            out[r["status"]] = r["n"]
            bytes_map[r["status"]] = int(r["s"] or 0)
    out["_bytes"] = bytes_map
    return out


@app.get("/api/drive")
def get_drive():
    g = gd_cfg()
    conn = db_conn()
    connected = drive_connected()
    if connected and drive_state["quota"] is None:
        try:
            drive_quota(use_cache=True)
        except Exception:
            pass
    counts = drive_counts()
    recent = [dict(r) for r in conn.execute(
        "SELECT path, cam_id, name, ts, size, status, error, updated FROM drive_uploads "
        "ORDER BY updated DESC LIMIT 30").fetchall()]
    cameras = [{"id": cid, "name": (c or {}).get("name") or cid,
                "storage_target": cam_drive_target(cid)}
               for cid, c in CONFIG.get("cameras", {}).items()]
    return {
        "enabled": bool(g.get("enabled")),
        "connected": drive_connected(),
        "auth_error": drive_state["auth_error"],
        "paused": drive_state["paused"],
        "paused_reason": drive_state["reason"],
        "client_id": g.get("client_id", ""),
        "has_client_secret": bool((g.get("client_secret") or "").strip()),
        "has_refresh_token": bool((g.get("refresh_token") or "").strip()),
        "folder_name": g.get("folder_name", "LiteNVR"),
        "cameras": g.get("cameras") or [],
        "bandwidth_kbps": int(g.get("bandwidth_kbps") or 0),
        "parallel_uploads": max(1, min(DRIVE_MAX_PARALLEL, int(g.get("parallel_uploads") or 1))),
        "max_gb": float(g.get("max_gb") or 0),
        "min_free_mb": int(g.get("min_free_mb") or 2000),
        "since_ts": float(g.get("since_ts") or 0),
        "counts": counts,
        "uploaded_bytes": counts["_bytes"]["done"],
        "queued_bytes": counts["_bytes"]["pending"] + counts["_bytes"]["uploading"] + counts["_bytes"]["error"],
        "quota": drive_state["quota"] if drive_connected() else None,
        "recent": recent,
        "cameras_all": cameras,
    }


@app.post("/api/drive")
def update_drive(settings: DriveSettings):
    g = gd_cfg()
    was_enabled = bool(g.get("enabled"))
    g["enabled"] = settings.enabled
    folder = settings.folder_name.strip() or "LiteNVR"
    if folder != g.get("folder_name"):
        g["folder_name"] = folder
        g["root_folder_id"] = ""
        g["cam_folders"] = {}
    g["cameras"] = [c for c in settings.cameras if c in CONFIG.get("cameras", {})]
    g["bandwidth_kbps"] = max(0, int(settings.bandwidth_kbps))
    g["parallel_uploads"] = max(1, min(DRIVE_MAX_PARALLEL, int(settings.parallel_uploads)))
    g["max_gb"] = max(0.0, float(settings.max_gb))
    g["min_free_mb"] = max(200, int(settings.min_free_mb))
    if settings.enabled and not was_enabled and not float(g.get("since_ts") or 0):
        # Bật lần đầu: chỉ sao lưu bản ghi mới từ giờ trở đi (tránh tự đẩy hàng TB cũ lên)
        g["since_ts"] = time.time()
    save_config(CONFIG)
    log_event("info", f"Cấu hình Google Drive: {'bật' if settings.enabled else 'tắt'}"
                      + f", tải song song {g['parallel_uploads']} file"
                      + (f", giới hạn tốc độ {g['bandwidth_kbps']} kbps" if g["bandwidth_kbps"] else ""))
    return {"status": "ok"}


@app.post("/api/drive/connect")
def drive_connect_start(body: DriveConnect):
    cid, sec = body.client_id.strip(), body.client_secret.strip()
    if not cid or not sec:
        raise HTTPException(status_code=400, detail="Cần nhập Client ID và Client Secret")
    try:
        j = _form_post(DRIVE_OAUTH_BASE + "/device/code", {"client_id": cid, "scope": DRIVE_SCOPE})
    except DriveHttpError as e:
        err = str((e.payload or {}).get("error", "")) if isinstance(e.payload, dict) else ""
        if err == "invalid_scope":
            hint = ("Google chỉ cho phép scope drive.file (hoặc drive.appdata) với client "
                    "TVs and Limited Input devices. Kiểm tra biến môi trường NVR_DRIVE_SCOPE nếu bạn tự đổi.")
        elif err == "invalid_client":
            hint = ("Client ID sai hoặc không phải loại 'TVs and Limited Input devices'. "
                    "Tạo lại đúng loại client trong Google Cloud Console.")
        else:
            hint = str(e.payload)
        raise HTTPException(status_code=400, detail=f"Google từ chối: {hint}")
    if not j.get("device_code"):
        raise HTTPException(status_code=400, detail=f"Phản hồi không hợp lệ: {j}")
    drive_pending_auth.update({
        "device_code": j["device_code"], "client_id": cid, "client_secret": sec,
        "interval": int(j.get("interval", 5)), "exp": time.time() + int(j.get("expires_in", 900)),
        "last_poll": 0.0})
    return {"user_code": j.get("user_code", ""),
            "verification_url": j.get("verification_url", "https://www.google.com/device"),
            "expires_in": int(j.get("expires_in", 900)),
            "interval": int(j.get("interval", 5))}


@app.get("/api/drive/connect/poll")
def drive_connect_poll():
    pa = drive_pending_auth
    if not pa:
        return {"status": "none"}
    if time.time() > pa["exp"]:
        drive_pending_auth.clear()
        return {"status": "expired"}
    if time.time() - pa["last_poll"] < pa["interval"]:
        return {"status": "pending"}
    pa["last_poll"] = time.time()
    try:
        j = _form_post(DRIVE_OAUTH_BASE + "/token", {
            "client_id": pa["client_id"], "client_secret": pa["client_secret"],
            "device_code": pa["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    except DriveHttpError as e:
        err = str((e.payload or {}).get("error") or "")
        if err == "authorization_pending":
            return {"status": "pending"}
        if err == "slow_down":
            pa["interval"] += 5
            return {"status": "pending"}
        if err == "access_denied":
            drive_pending_auth.clear()
            return {"status": "denied"}
        if err == "expired_token":
            drive_pending_auth.clear()
            return {"status": "expired"}
        return {"status": "error", "detail": str(e.payload)[:300]}
    if not j.get("refresh_token"):
        return {"status": "error", "detail": "Google không trả refresh_token — hãy xoá quyền cũ của app "
                                             "tại myaccount.google.com/permissions rồi thử lại"}
    cid, sec = pa["client_id"], pa["client_secret"]
    drive_pending_auth.clear()
    drive_apply_tokens(cid, sec, j["refresh_token"])
    return {"status": "ok"}


def drive_apply_tokens(cid: str, sec: str, refresh_token: str):
    g = gd_cfg()
    g["client_id"] = cid.strip()
    g["client_secret"] = sec.strip()
    g["refresh_token"] = refresh_token.strip()
    g["root_folder_id"] = ""
    g["cam_folders"] = {}
    save_config(CONFIG)
    _drive_token_cache["token"] = ""
    drive_state["auth_error"] = ""
    drive_state["paused"] = False
    drive_state["reason"] = ""
    try:
        drive_root_folder()  # tạo thư mục gốc ngay
    except DriveAuthError as e:
        drive_state["auth_error"] = str(e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        pass


@app.post("/api/drive/token")
def drive_save_token(body: DriveToken):
    cid, sec, rt = body.client_id.strip(), body.client_secret.strip(), body.refresh_token.strip()
    # Hỗ trợ dán nguyên JSON token của rclone: {"access_token": ..., "refresh_token": ...}
    if rt.startswith("{"):
        try:
            j = json.loads(rt)
            rt = (j.get("refresh_token") or "").strip()
            cid = cid or (j.get("client_id") or "").strip()
            sec = sec or (j.get("client_secret") or "").strip()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON token không đọc được")
    if not (cid and sec and rt):
        raise HTTPException(status_code=400, detail="Cần đủ Client ID, Client Secret và Refresh Token "
                                                    "(refresh token của rclone cần kèm client_id/secret đã tạo token đó)")
    drive_apply_tokens(cid, sec, rt)
    return {"status": "ok"}


@app.post("/api/drive/test")
def drive_test():
    if not drive_connected():
        raise HTTPException(status_code=400, detail="Chưa kết nối Google Drive")
    try:
        drive_access_token(force=True)
        quota = drive_quota(use_cache=False)
        drive_root_folder()
    except DriveAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])
    drive_state["auth_error"] = ""
    return {"status": "ok", "quota": quota}


@app.post("/api/drive/now")
def drive_upload_now(body: DriveNow):
    if not drive_connected():
        raise HTTPException(status_code=400, detail="Chưa kết nối Google Drive")
    if not gd_cfg().get("enabled"):
        raise HTTPException(status_code=400, detail="Sao lưu Drive đang tắt — bật trước rồi thử lại")
    queued = drive_queue_backfill(body.cam_id.strip(), body.date_from.strip(),
                                  (body.date_to or body.date_from).strip())
    log_event("info", f"Drive: xếp hàng tải bù {queued} đoạn ghi")
    return {"status": "ok", "queued": queued}


@app.get("/api/drive/play/{cam_id}/{fname}")
def drive_play_status(cam_id: str, fname: str, retry: int = 0):
    """Tiến độ tải bản ghi từ Drive về ổ cứng (để UI hiện % khi xem lại).
    Tự bắt đầu tải nếu chưa chạy, nhờ vậy UI không bị kẹt khi request video bị ngắt."""
    cam = os.path.basename(cam_id)
    name = os.path.basename(fname)
    path = os.path.join(RECORDINGS_DIR, cam, name)
    if os.path.isfile(path):
        return {"state": "local", "done": 1, "total": 1}
    key = f"{cam}/{name}"
    pr = _drive_fetch_prog.get(key) or {}
    if pr.get("state") == "error" and not retry:
        return {"state": "error", "error": pr.get("error", ""),
                "done": int(pr.get("done") or 0), "total": int(pr.get("total") or 0)}
    row = None
    try:
        row = db_conn().execute(
            "SELECT drive_id, size FROM drive_uploads WHERE path=? AND status='done'",
            (key,)).fetchone()
    except Exception:
        pass
    if not row:
        return {"state": "none", "done": 0, "total": 0}
    if not row[0]:
        return {"state": "drive", "done": 0, "total": 0}
    _start_drive_fetch(cam, name, row[0], path)  # chạy hoặc tham gia tiến trình đang tải
    pr = _drive_fetch_prog.get(key) or {}
    return {"state": pr.get("state") or "downloading",
            "error": pr.get("error", ""),
            "done": int(pr.get("done") or 0),
            "total": int(pr.get("total") or (row[1] if len(row) > 1 else 0) or 0)}


@app.post("/api/drive/cleanup")
def drive_cleanup_now():
    if not drive_connected():
        raise HTTPException(status_code=400, detail="Chưa kết nối Google Drive")
    try:
        res = drive_cleanup(force=True)
    except DriveAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    log_event("info", f"Drive dọn tay: xoá {res.get('deleted', 0)} file, giải phóng "
                      f"{round(int(res.get('freed') or 0) / 1e6)} MB")
    return {"status": "ok", **res}


@app.post("/api/drive/retry")
def drive_retry_errors():
    cur = db_conn().execute(
        "UPDATE drive_uploads SET status='pending', tries=0, next_try=0, error='' WHERE status='error'")
    db_conn().commit()
    return {"status": "ok", "requeued": cur.rowcount}


@app.post("/api/drive/disconnect")
def drive_disconnect():
    g = gd_cfg()
    g["refresh_token"] = ""
    g["root_folder_id"] = ""
    g["cam_folders"] = {}
    g["enabled"] = False
    save_config(CONFIG)
    _drive_token_cache["token"] = ""
    drive_state["auth_error"] = ""
    log_event("info", "Đã ngắt kết nối Google Drive (file trên Drive giữ nguyên)")
    return {"status": "ok"}

# ---------------- PTZ (ONVIF) ----------------
@app.post("/api/ptz/{cam_id}")
def ptz_command(cam_id: str, cmd: PtzCmd):
    return onvif_ptz_move(cam_id, cmd.action, cmd.speed, cmd.duration, cmd.preset)

@app.get("/api/ptz/{cam_id}/presets")
def ptz_list_presets(cam_id: str):
    return {"presets": onvif_ptz_presets(cam_id)}

# ---------------- Xuất clip từ bản ghi ----------------
def _segments_between(cam_id: str, date: str, start_sec: float, end_sec: float) -> List[tuple]:
    """Các segment giao với khoảng [start_sec, end_sec] (giây trong ngày)."""
    cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
    out = []
    local_names = set()
    if os.path.isdir(cam_dir):
        for fname in sorted(os.listdir(cam_dir)):
            if not (fname.startswith(date) and fname.endswith(".mp4")):
                continue
            try:
                time_part = fname.replace(".mp4", "").split("_")[1]
                h, m, s = map(int, time_part.split("-"))
            except Exception:
                continue
            st = h * 3600 + m * 60 + s
            fp = os.path.join(cam_dir, fname)
            mt = datetime.fromtimestamp(os.path.getmtime(fp))
            en = mt.hour * 3600 + mt.minute * 60 + mt.second
            if mt.strftime("%Y-%m-%d") != date or en <= st:
                en = st + 60
            local_names.add(fname)
            if st < end_sec and en > start_sec:
                out.append((fp, st, en))
    # Các đoạn chỉ còn trên Drive: tải về rồi đưa vào danh sách ghép clip
    try:
        rows = db_conn().execute(
            "SELECT name FROM drive_uploads WHERE cam_id=? AND status='done' AND name GLOB ? ORDER BY name",
            (cam_id, date + "_*.mp4")).fetchall()
    except Exception:
        rows = []
    for (fname,) in rows:
        if fname in local_names:
            continue
        try:
            time_part = fname.replace(".mp4", "").split("_")[1]
            h, m, s = map(int, time_part.split("-"))
        except Exception:
            continue
        st = h * 3600 + m * 60 + s
        if not (st < end_sec and st + 60 > start_sec):
            continue
        try:
            fp = ensure_local_recording(cam_id, fname)
        except HTTPException as e:
            log_event("warn", f"Xuất clip: bỏ đoạn trên Drive ({fname}): {e.detail}", cam_id)
            continue
        if fp:
            out.append((fp, st, st + 60))
    out.sort(key=lambda x: x[1])
    return out

def _fmt_hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}-{sec % 3600 // 60:02d}-{sec % 60:02d}"

@app.get("/api/export/{cam_id}")
def export_clip(cam_id: str, date: str, start: float = 0, end: float = 86400, hc: int = 0):
    """Ghép các đoạn ghi trong khoảng [start, end] (giây trong ngày) thành 1 file MP4 tải về.
    hc=1: chuyển mã H.264 để phát trên mọi thiết bị (tốn CPU hơn)."""
    safe_cam = os.path.basename(cam_id)
    if safe_cam not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    segs = _segments_between(safe_cam, date, start, end)
    if not segs:
        raise HTTPException(status_code=404, detail="Không có bản ghi trong khoảng đã chọn")
    dur = max(1.0, end - start)
    fname_out = f"{safe_cam}_{date}_{_fmt_hms(start)}_to_{_fmt_hms(end)}.mp4"

    # 1 segment + không cần chuyển mã: trả file gốc luôn (nhanh, không tốn CPU)
    if len(segs) == 1 and not hc:
        return FileResponse(segs[0][0], media_type="video/mp4", filename=fname_out)

    tmpdir = tempfile.mkdtemp(prefix="litenvr_export_")
    out_path = os.path.join(tmpdir, fname_out)
    list_path = os.path.join(tmpdir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for fp, _, _ in segs:
            f.write("file '" + fp.replace("'", "'\\''") + "'\n")

    offs = max(0.0, start - segs[0][1])
    cmd = [*_ffmpeg_base_args(), "-threads", str(FFMPEG_THREADS),
           "-f", "concat", "-safe", "0", "-i", list_path,
           "-ss", f"{offs:.3f}", "-t", f"{dur:.3f}"]
    if hc:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-threads", str(FFMPEG_THREADS), "-c:a", "aac"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", out_path]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Lỗi xuất clip: {e}")
    if proc.returncode != 0 or not os.path.isfile(out_path):
        shutil.rmtree(tmpdir, ignore_errors=True)
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise HTTPException(status_code=500, detail=f"ffmpeg lỗi khi xuất clip: {err}" if err else "ffmpeg lỗi khi xuất clip")

    log_event("info", f"Xuất clip {date} {_fmt_hms(start)}-{_fmt_hms(end)} ({len(segs)} đoạn)", safe_cam)
    return FileResponse(out_path, media_type="video/mp4", filename=fname_out,
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))

# ---------------- Backup / Restore cấu hình ----------------
@app.get("/api/config/export")
def export_config():
    """Tải toàn bộ cấu hình (camera, lịch, HA, thông báo, lưu trữ) dạng JSON."""
    body = json.dumps(CONFIG, indent=2, ensure_ascii=False)
    fname = f"litenvr_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.post("/api/config/import")
def import_config(payload: Dict[str, Any] = Body(...)):
    """Khôi phục cấu hình từ file backup JSON (thay thế toàn bộ cấu hình hiện tại)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("cameras"), dict):
        raise HTTPException(status_code=400, detail="File cấu hình không hợp lệ (thiếu 'cameras')")
    for cid in list(CONFIG.get("cameras", {})):
        stop_camera_worker(cid)
    merged = _normalize_config(json.loads(json.dumps(payload)))
    bad = [cid for cid in (merged.get("cameras") or {}) if not _valid_cam_id(cid)]
    if bad:
        raise HTTPException(status_code=400, detail=f"Mã camera không hợp lệ trong file: {bad[0]}")
    CONFIG.clear()
    CONFIG.update(merged)
    save_config(CONFIG)
    refresh_go2rtc()
    started = 0
    for cid, cam in CONFIG.get("cameras", {}).items():
        if schedule_record_mode(cam) != "off":
            start_camera_worker(cid)
            started += 1
    log_event("info", f"Khôi phục cấu hình: {len(CONFIG.get('cameras', {}))} camera, khởi động {started} luồng")
    return {"status": "ok", "cameras": len(CONFIG.get("cameras", {})), "started": started}

@app.get("/api/logs")
def get_logs(limit: int = 200):
    """Nhật ký hệ thống + trạng thái lỗi từng camera để UI hiển thị và gợi ý cách sửa."""
    cams = {}
    for cam_id, cam in CONFIG.get("cameras", {}).items():
        st = cam_status.get(cam_id, {})
        cams[cam_id] = {
            "name": cam.get("name", cam_id),
            "running": bool(st.get("running")),
            "last_error": st.get("last_error", ""),
            "rate_limited": bool(st.get("rate_limited", False)),
            "detect_failed": bool(st.get("detect_failed", False)),
            "detected_codec": st.get("detected_codec", ""),
        }
    events = list(APP_LOG)[-max(1, min(limit, 300)):]
    events.reverse()
    return {"events": events, "cameras": cams}

@app.get("/api/status/{cam_id}")
def get_cam_status(cam_id: str, request: Request):
    cam = (CONFIG.get("cameras") or {}).get(cam_id) or {}
    st = cam_status.get(cam_id, {})
    hls_path = os.path.join(HLS_DIR, cam_id, "live.m3u8")
    try:
        hls_age = max(0.0, time.time() - os.path.getmtime(hls_path))
    except OSError:
        hls_age = None
    # Chỉ báo HLS sẵn sàng khi ffmpeg live thật sự đang chạy và playlist vừa được ghi.
    # Playlist cũ còn trên tmpfs không được khiến UI/HA tưởng luồng vẫn sống.
    hls_ready = bool(st.get("running") and st.get("_live_mode")
                     and hls_age is not None and hls_age < 15)
    u = getattr(request.state, "user", None) or {}
    logs = list(st.get("log", []))
    if AUTH_ENABLED and u.get("role") != "admin":
        logs = [mask_secrets(x) for x in logs]
    off = camera_offline_state(cam_id)
    last_error = st.get("last_error", "")
    last_warning = st.get("last_warning", "")
    if AUTH_ENABLED and u.get("role") != "admin":
        last_error = mask_secrets(last_error)
        last_warning = mask_secrets(last_warning)
    return {
        "running": bool(st.get("running")),
        "expected": off["expected"],
        "offline": off["offline"],
        "offline_since": off["down_since"],
        "record_mode": schedule_record_mode(cam) if cam else "off",
        "stop_reason": st.get("stop_reason", ""),
        "last_exit_code": st.get("last_exit_code"),
        "last_exit_at": st.get("last_exit_at", 0),
        "hls_ready": hls_ready,
        "hls_age_sec": round(hls_age, 1) if hls_age is not None else None,
        "detected_codec": st.get("detected_codec", ""),
        "detected_profile": st.get("detected_profile", ""),
        "detected_pix_fmt": st.get("detected_pix_fmt", ""),
        "detect_failed": bool(st.get("detect_failed", False)),
        "substream_fallback": not _substream_usable(cam_id, cam),
        "restream_bypass": not _restream_usable(cam_id, cam),
        "rate_limited": bool(st.get("rate_limited", False)),
        "last_warning": last_warning,
        "last_error": last_error,
        "webrtc": {
            "enabled": bool(_go2rtc.get("enabled")),
            "running": bool(_go2rtc.get("running")),
            "error": _go2rtc.get("error", ""),
            "port": GO2RTC_WEBRTC_PORT,
            "public_host": GO2RTC_WEBRTC_HOST,
        },
        "log": logs,
    }

_playback_probe_cache: Dict[str, tuple] = {}
_playback_probe_lock = threading.Lock()
_playback_cache_state = {"last_prune": 0.0}
_playback_sem: Optional[asyncio.Semaphore] = None


def _playback_semaphore() -> asyncio.Semaphore:
    global _playback_sem
    if _playback_sem is None:
        _playback_sem = asyncio.Semaphore(PLAYBACK_TRANSCODE_MAX)
    return _playback_sem


def _probe_recording(path: str) -> dict:
    """Dò codec video/audio của một file bản ghi (cache theo mtime + dung lượng)."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    if not st.st_size:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    with _playback_probe_lock:
        cached = _playback_probe_cache.get(path)
        if cached and cached[0] == key:
            return dict(cached[1])
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,profile,pix_fmt",
        "-of", "json", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return {}
    info: Dict[str, Any] = {}
    for stream in data.get("streams") or []:
        kind = str(stream.get("codec_type") or "")
        if kind == "video" and "video_codec" not in info:
            info["video_codec"] = str(stream.get("codec_name") or "").lower()
            info["video_profile"] = str(stream.get("profile") or "").lower()
            info["video_pix_fmt"] = str(stream.get("pix_fmt") or "").lower()
        elif kind == "audio" and "audio_codec" not in info:
            info["audio_codec"] = str(stream.get("codec_name") or "").lower()
    if info.get("video_codec"):
        with _playback_probe_lock:
            if len(_playback_probe_cache) >= 512:
                for old_key in list(_playback_probe_cache)[:128]:
                    _playback_probe_cache.pop(old_key, None)
            _playback_probe_cache[path] = (key, dict(info))
    return info


def _recording_needs_transcode(path: str) -> bool:
    """True nếu video/audio không chắc trình duyệt phát được (HEVC, H.264 10-bit, audio lạ)."""
    info = _probe_recording(path)
    if not info:
        return True
    codec = info.get("video_codec", "")
    if not _is_browser_safe_h264(codec, info.get("video_pix_fmt", ""),
                                 info.get("video_profile", "")):
        return True
    return bool(info.get("audio_codec") and info["audio_codec"] not in ("aac", "mp3"))


def prune_playback_cache(force: bool = False) -> int:
    """Dọn file SMB tải tạm để xem lại; giữ cache gần đây, xoá cũ và quá hạn mức."""
    now = time.time()
    if not force and now - float(_playback_cache_state.get("last_prune") or 0) < 300:
        return 0
    _playback_cache_state["last_prune"] = now
    files = []
    removed = 0
    for root, _, names in os.walk(PLAYBACK_CACHE_DIR):
        for name in names:
            fp = os.path.join(root, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if now - st.st_mtime > PLAYBACK_CACHE_TTL_SEC:
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
            else:
                files.append((st.st_mtime, st.st_size, fp))
    max_bytes = PLAYBACK_CACHE_MB * 1024 * 1024
    total = sum(size for _, size, _ in files)
    for _, size, fp in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.remove(fp)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def _smb_playback_cache_path(cam: str, date: str, name: str) -> str:
    return os.path.join(PLAYBACK_CACHE_DIR, "smb", os.path.basename(cam),
                        os.path.basename(name))


def _ensure_smb_playback_file(target, cam: str, date: str, name: str) -> str:
    """Tải đoạn ghi SMB vào cache cục bộ để ffprobe/ffmpeg đọc và tua ổn định."""
    path = _smb_playback_cache_path(cam, date, name)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.part"
    fh = None
    try:
        fh = target.open_recording(date, os.path.basename(cam), os.path.basename(name))
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fh, out, 1024 * 1024)
        if not os.path.isfile(tmp) or os.path.getsize(tmp) <= 0:
            raise RuntimeError("File SMB rỗng")
        os.replace(tmp, path)
        prune_playback_cache()
        return path
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _playback_response(path: str, force_url: str):
    """Phát trực tiếp nếu trình duyệt chắc chắn đọc được; nếu không, chuyển hướng
    sang URL t=1. UI gọi info=1 trước để biết có chuyển mã và tránh tua hai lần."""
    if not _recording_needs_transcode(path):
        return FileResponse(path, media_type="video/mp4")
    return RedirectResponse(force_url, status_code=307)


def _play_transcode_cmd(path: str, ss: float = 0):
    """Chuyển mã bản ghi HEVC/H.265/H.264 lạ sang H.264 + AAC cho trình duyệt.
    -ss đặt TRƯỚC -i để tua nhanh (input seek). Ultrafast + GOP 2s giúp seek mượt."""
    cmd = _ffmpeg_base_args()
    if ss and ss > 0:
        cmd += ["-ss", f"{ss:.3f}"]
    cmd += [
        "-threads", str(FFMPEG_THREADS), "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", PLAYBACK_PRESET, "-tune", "zerolatency",
        "-crf", str(PLAYBACK_CRF), "-pix_fmt", "yuv420p",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        "-threads", str(FFMPEG_THREADS),
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-sn", "-dn",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return cmd

_playback_hls_lock = threading.Lock()
_playback_hls_procs: Dict[str, dict] = {}
PLAYBACK_HLS_MAX_AGE_SEC = 3600

def _prune_playback_hls(max_age: int = PLAYBACK_HLS_MAX_AGE_SEC):
    now = time.time()
    with _playback_hls_lock:
        for token, rec in list(_playback_hls_procs.items()):
            proc = rec.get("proc")
            expired = now - float(rec.get("started") or 0) > max_age
            if proc is not None and proc.poll() is None and not expired:
                continue
            if proc is not None and proc.poll() is None:
                _terminate_process(proc)
            _playback_hls_procs.pop(token, None)
    if not os.path.isdir(PLAYBACK_HLS_DIR):
        return
    try:
        cams = os.listdir(PLAYBACK_HLS_DIR)
    except OSError:
        return
    for cam in cams:
        cam_dir = os.path.join(PLAYBACK_HLS_DIR, cam)
        if not os.path.isdir(cam_dir):
            continue
        try:
            tokens = os.listdir(cam_dir)
        except OSError:
            continue
        for token in tokens:
            token_dir = os.path.join(cam_dir, token)
            try:
                age = now - os.path.getmtime(token_dir)
            except OSError:
                continue
            if age > max_age:
                with _playback_hls_lock:
                    rec = _playback_hls_procs.pop(token, None)
                proc = rec.get("proc") if rec else None
                if proc is not None and proc.poll() is None:
                    _terminate_process(proc)
                shutil.rmtree(token_dir, ignore_errors=True)
        try:
            if not os.listdir(cam_dir):
                os.rmdir(cam_dir)
        except OSError:
            pass

def _play_hls_cmd(path: str, ss: float, out_dir: str) -> List[str]:
    cmd = _ffmpeg_base_args()
    if ss and ss > 0:
        cmd += ["-ss", f"{ss:.3f}"]
    cmd += ["-threads", str(FFMPEG_THREADS), "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?"]
    if _recording_needs_transcode(path):
        cmd += [
            "-c:v", "libx264", "-preset", PLAYBACK_PRESET, "-tune", "zerolatency",
            "-crf", str(PLAYBACK_CRF), "-pix_fmt", "yuv420p",
            "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-threads", str(FFMPEG_THREADS),
        ]
    else:
        cmd += ["-c:v", "copy"]
    cmd += [
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-sn", "-dn",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_segment_type", "mpegts",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
        os.path.join(out_dir, "index.m3u8"),
    ]
    return cmd

def _start_playback_hls(cam_id: str, path: str, ss: float = 0) -> str:
    _prune_playback_hls()
    with _playback_hls_lock:
        active = [(token, rec) for token, rec in _playback_hls_procs.items()
                  if rec.get("proc") is not None and rec["proc"].poll() is None]
    excess = active[:max(0, len(active) - PLAYBACK_TRANSCODE_MAX + 1)]
    for token, rec in excess:
        with _playback_hls_lock:
            _playback_hls_procs.pop(token, None)
        proc = rec.get("proc")
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        old_dir = rec.get("dir")
        if old_dir:
            shutil.rmtree(old_dir, ignore_errors=True)
    cam = os.path.basename(cam_id)
    token = uuid.uuid4().hex
    out_dir = os.path.join(PLAYBACK_HLS_DIR, cam, token)
    os.makedirs(out_dir, exist_ok=True)
    cmd = _play_hls_cmd(path, ss, out_dir)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail=f"Không chạy được ffmpeg HLS: {e}")
    playlist = os.path.join(out_dir, "index.m3u8")
    first_seg = os.path.join(out_dir, "seg00000.ts")
    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        try:
            ready = (os.path.getsize(playlist) > 0 and os.path.getsize(first_seg) > 0)
        except OSError:
            ready = False
        if ready:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if not ready:
        _terminate_process(proc)
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail="Không tạo được HLS cho bản ghi")
    with _playback_hls_lock:
        _playback_hls_procs[token] = {"proc": proc, "started": time.time(), "dir": out_dir}
    return f"/hls/_play/{urllib.parse.quote(cam)}/{token}/index.m3u8"

async def _stop_playback_process(proc):
    async def discard_output():
        if proc.stdout:
            while await proc.stdout.read(65536):
                pass

    drain = asyncio.create_task(discard_output())
    try:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(proc.wait(), timeout=3)
    finally:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        with _playback_lock:
            _playback_processes.pop(proc.pid, None)


class _TranscodedRecordingResponse(StreamingResponse):
    def __init__(self, cmd, cam_id):
        self.cmd = cmd
        self.cam_id = cam_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        super().__init__(self._stream(), media_type="video/mp4")

    async def _stream(self):
        if self.proc is None or self.proc.stdout is None:
            return
        while True:
            chunk = await asyncio.wait_for(self.proc.stdout.read(65536), timeout=30)
            if not chunk:
                break
            yield chunk

    async def __call__(self, scope, receive, send):
        sem = _playback_semaphore()
        await sem.acquire()
        try:
            with anyio.CancelScope(shield=True):
                if _shutdown.is_set():
                    await Response(status_code=503)(scope, receive, send)
                    return
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=65536,
                )
                with _playback_lock:
                    _playback_processes[self.proc.pid] = (self.proc, self.cam_id)
            await super().__call__(scope, receive, send)
        finally:
            if self.proc:
                with anyio.CancelScope(shield=True):
                    await _stop_playback_process(self.proc)
            sem.release()


def _range_bounds(header: str, size: int):
    """Trả (start, length, partial) cho một Range đơn; raise 416 nếu không hợp lệ."""
    if not header:
        return 0, size, False
    m = re.match(r"^bytes=(\d*)-(\d*)$", header.strip())
    if not m:
        raise HTTPException(status_code=416, detail="Range không hợp lệ",
                            headers={"Content-Range": f"bytes */{size}"})
    first, last = m.group(1), m.group(2)
    if not first:
        if not last:
            raise HTTPException(status_code=416, detail="Range không hợp lệ",
                                headers={"Content-Range": f"bytes */{size}"})
        length = min(int(last), size)
        start = max(0, size - length)
        return start, length, True
    start = int(first)
    if start >= size:
        raise HTTPException(status_code=416, detail="Range ngoài file",
                            headers={"Content-Range": f"bytes */{size}"})
    end = int(last) if last else size - 1
    if end < start:
        raise HTTPException(status_code=416, detail="Range không hợp lệ",
                            headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    return start, end - start + 1, True


@app.get("/api/smb/play/{cam_id}/{date}/{fname}")
def play_smb_recording(cam_id: str, date: str, fname: str, request: Request,
                       t: int = 0, ss: float = 0, info: int = 0, hls: int = 0):
    """Phát bản ghi SMB. t=0: stream trực tiếp; t=1: buộc H.264;
    t=2: tự dò codec rồi phát trực tiếp hoặc chuyển mã H.264/AAC."""
    cam = os.path.basename(cam_id)
    name = os.path.basename(fname)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or "") or not REC_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Tên bản ghi không hợp lệ")
    target = _smb_target_for_playback()
    if not target:
        raise HTTPException(status_code=400, detail="Chưa cấu hình lưu trữ ngoài SMB")
    if t in (1, 2) or info:
        try:
            path = _ensure_smb_playback_file(target, cam, date, name)
        except Exception as e:
            log_event("warn", f"Không tải được bản ghi SMB để phát: {mask_secrets(str(e))}", cam)
            raise HTTPException(status_code=502, detail="Không tải được bản ghi từ SMB")
        finally:
            target.close()
        if info:
            data = {"transcode": _recording_needs_transcode(path)}
            if hls:
                data["hls"] = _start_playback_hls(cam, path, ss)
            return JSONResponse(data)
        if t == 2:
            force_url = (f"/api/smb/play/{urllib.parse.quote(cam)}/{date}/"
                         f"{urllib.parse.quote(name)}?t=1&ss={max(0, ss):.3f}")
            return _playback_response(path, force_url)
        return _TranscodedRecordingResponse(_play_transcode_cmd(path, ss), cam)
    try:
        fh = target.open_recording(date, cam, name)
        size = fh.seek(0, os.SEEK_END)
        fh.seek(0)
    except Exception as e:
        target.close()
        log_event("warn", f"Không mở được bản ghi SMB: {mask_secrets(str(e))}", cam)
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi trên SMB")
    start, length, partial = _range_bounds(request.headers.get("range", ""), size)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store",
               "Content-Length": str(length)}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"

    def iter_file():
        try:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1024 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk
        finally:
            try:
                fh.close()
            finally:
                target.close()

    return StreamingResponse(iter_file(), status_code=206 if partial else 200,
                             media_type="video/mp4", headers=headers)


@app.get("/api/play/{cam_id}/{fname}")
def play_recording(cam_id: str, fname: str, t: int = 0, ss: float = 0, info: int = 0, hls: int = 0):
    """Phát bản ghi. t=0: file gốc; t=1: buộc H.264/AAC; t=2: tự dò codec
    và chỉ chuyển mã khi trình duyệt không phát được (HEVC, H.264 10-bit...)."""
    safe_cam = os.path.basename(cam_id)
    safe_name = os.path.basename(fname)
    path = ensure_local_recording(safe_cam, safe_name)
    if not path:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    if info:
        data = {"transcode": _recording_needs_transcode(path)}
        if hls:
            data["hls"] = _start_playback_hls(safe_cam, path, ss)
        return JSONResponse(data)

    if not t:
        return FileResponse(path, media_type="video/mp4")
    if t == 2:
        force_url = (f"/api/play/{urllib.parse.quote(safe_cam)}/"
                     f"{urllib.parse.quote(safe_name)}?t=1&ss={max(0, ss):.3f}")
        return _playback_response(path, force_url)
    return _TranscodedRecordingResponse(_play_transcode_cmd(path, ss), safe_cam)

# ================= API bảo mật: đăng nhập, tài khoản con, 2FA, nhật ký =================
class LoginReq(BaseModel):
    username: str
    password: str
    otp: Optional[str] = ""

class PasswordReq(BaseModel):
    old_password: Optional[str] = ""
    new_password: str

class TotpCodeReq(BaseModel):
    code: str

class UserCreateReq(BaseModel):
    username: str
    password: str
    role: str = "user"
    perms: Optional[Dict[str, Any]] = None
    cameras: Optional[List[str]] = None
    enabled: bool = True

class UserUpdateReq(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    perms: Optional[Dict[str, Any]] = None
    cameras: Optional[List[str]] = None
    enabled: Optional[bool] = None


def _user_changed(name: str, detail: str = ""):
    conn = db_conn()
    conn.execute("INSERT INTO auth_events (ts, username, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
                 (int(time.time()), name, "-", "user_update", detail))
    conn.commit()


@app.post("/api/login")
def api_login(req: LoginReq, request: Request):
    ip = client_ip(request)
    locked = login_locked(ip)
    if locked > 0:
        audit("login_locked", req.username.strip(), ip, f"khoá {locked}s")
        return JSONResponse({"detail": f"Quá nhiều lần sai. Thử lại sau {locked // 60 + 1} phút."},
                            status_code=429, headers={"Retry-After": str(locked)})
    username = req.username.strip()
    user = None
    if secrets.compare_digest(username, NVR_USER) and secrets.compare_digest(req.password, NVR_PASS):
        user = root_user()
    else:
        row = get_user(username)
        if row is not None and row["enabled"] and verify_password(req.password, row["salt"], row["pass_hash"]):
            if row["totp_enabled"]:
                if not (req.otp or "").strip():
                    return JSONResponse({"detail": "Nhập mã 2FA", "otp_required": True}, status_code=401)
                if not totp_verify(row["totp_secret"], req.otp or ""):
                    note_login_fail(ip)
                    audit("login_fail", username, ip, "mã 2FA sai")
                    return JSONResponse({"detail": "Mã 2FA không đúng", "otp_required": True}, status_code=401)
            user = _row_to_user(row)
        else:
            # Chống dò username: vẫn tốn thời gian hash như khi sai mật khẩu
            verify_password(req.password, "00" * 16, "0" * 64)
    if user is None:
        note_login_fail(ip)
        audit("login_fail", username, ip)
        return JSONResponse({"detail": "Sai tài khoản hoặc mật khẩu"}, status_code=401)
    clear_login_fails(ip)
    token = create_session(user["username"], ip)
    if not user["is_root"]:
        try:
            conn = db_conn()
            conn.execute("UPDATE users SET last_login=? WHERE username=?", (int(time.time()), user["username"]))
            conn.commit()
        except Exception:
            pass
    audit("login_ok", user["username"], ip)
    resp = JSONResponse({"status": "ok", "user": user})
    secure = request.url.scheme == "https" or os.environ.get("NVR_COOKIE_SECURE", "").lower() in ("1", "true", "on")
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                    samesite="lax", path="/", secure=secure)
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        destroy_session(token)
    u = getattr(request.state, "user", None) or {}
    if token and not u.get("is_root"):
        audit("logout", u.get("username", ""), client_ip(request))
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    if not AUTH_ENABLED:
        return {"username": NVR_USER, "role": "admin", "perms": dict(ALL_PERMS), "cameras": [],
                "is_root": True, "enabled": True, "totp_enabled": False, "auth_enabled": False}
    u = dict(request.state.user)
    u["auth_enabled"] = True
    return u


@app.post("/api/me/password")
def api_change_password(req: PasswordReq, request: Request):
    u = request.state.user
    if u.get("is_root"):
        raise HTTPException(status_code=400, detail="Tài khoản gốc đổi mật khẩu bằng biến môi trường NVR_PASS trong docker-compose")
    if len(req.new_password or "") < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu mới cần ít nhất 6 ký tự")
    row = get_user(u["username"])
    if row is None or not verify_password(req.old_password or "", row["salt"], row["pass_hash"]):
        audit("pw_change_fail", u["username"], client_ip(request))
        raise HTTPException(status_code=403, detail="Mật khẩu hiện tại không đúng")
    salt, ph = hash_password(req.new_password)
    conn = db_conn()
    conn.execute("UPDATE users SET salt=?, pass_hash=? WHERE username=?", (salt, ph, u["username"]))
    conn.execute("DELETE FROM sessions WHERE username=?", (u["username"],))
    conn.commit()
    audit("pw_change", u["username"], client_ip(request))
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me/sessions")
def api_my_sessions(request: Request):
    """Danh sách phiên đăng nhập của tài khoản hiện tại (không lộ token thật)."""
    u = request.state.user
    uname = u.get("username") or NVR_USER
    cur = getattr(request.state, "session_token", "") or ""
    cur_hash = hashlib.sha256(cur.encode()).hexdigest() if cur else ""
    rows = db_conn().execute(
        "SELECT token_hash, created, expires, ip FROM sessions WHERE username=? ORDER BY created DESC",
        (uname,)).fetchall()
    out = [{
        "id": r[0][:12],
        "current": bool(cur_hash) and secrets.compare_digest(r[0], cur_hash),
        "created": r[1], "expires": r[2], "ip": r[3] or "",
    } for r in rows]
    return {"sessions": out, "count": len(out)}

@app.post("/api/me/sessions/revoke")
def api_revoke_sessions(request: Request, others_only: bool = True):
    """Thu hồi phiên: mặc định giữ phiên hiện tại, xoá mọi phiên khác của tài khoản."""
    u = request.state.user
    uname = u.get("username") or NVR_USER
    cur = getattr(request.state, "session_token", "") or ""
    cur_hash = hashlib.sha256(cur.encode()).hexdigest() if cur else ""
    conn = db_conn()
    if others_only and cur_hash:
        cur_ = conn.execute("DELETE FROM sessions WHERE username=? AND token_hash<>?", (uname, cur_hash))
    else:
        cur_ = conn.execute("DELETE FROM sessions WHERE username=?", (uname,))
    conn.commit()
    audit("revoke_sessions", uname, client_ip(request), f"others_only={others_only}")
    return {"status": "ok", "revoked": cur_.rowcount}

@app.post("/api/me/totp/init")
def api_totp_init(request: Request):
    u = request.state.user
    if u.get("is_root"):
        raise HTTPException(status_code=400, detail="2FA áp dụng cho tài khoản con; tài khoản gốc bảo vệ bằng NVR_PASS")
    secret = new_totp_secret()
    conn = db_conn()
    conn.execute("UPDATE users SET totp_secret=?, totp_enabled=0 WHERE username=?", (secret, u["username"]))
    conn.commit()
    return {"secret": secret, "uri": totp_uri(u["username"], secret)}


@app.post("/api/me/totp/enable")
def api_totp_enable(req: TotpCodeReq, request: Request):
    u = request.state.user
    row = get_user(u["username"])
    if row is None or not row["totp_secret"]:
        raise HTTPException(status_code=400, detail="Chưa tạo mã 2FA, bấm tạo trước")
    if not totp_verify(row["totp_secret"], req.code):
        raise HTTPException(status_code=403, detail="Mã 2FA không đúng, thử lại")
    conn = db_conn()
    conn.execute("UPDATE users SET totp_enabled=1 WHERE username=?", (u["username"],))
    conn.commit()
    audit("totp_enable", u["username"], client_ip(request))
    return {"status": "ok"}


@app.post("/api/me/totp/disable")
def api_totp_disable(req: TotpCodeReq, request: Request):
    u = request.state.user
    row = get_user(u["username"])
    if row is None or not row["totp_enabled"]:
        raise HTTPException(status_code=400, detail="2FA chưa bật")
    if not totp_verify(row["totp_secret"], req.code):
        raise HTTPException(status_code=403, detail="Mã 2FA không đúng")
    conn = db_conn()
    conn.execute("UPDATE users SET totp_enabled=0, totp_secret='' WHERE username=?", (u["username"],))
    conn.commit()
    audit("totp_disable", u["username"], client_ip(request))
    return {"status": "ok"}


@app.get("/api/users")
def api_users():
    users = []
    root = root_user()
    root["last_login"] = 0
    users.append(root)
    try:
        c = db_conn().cursor()
        c.execute("SELECT * FROM users ORDER BY username")
        for row in c.fetchall():
            users.append(_row_to_user(row))
    except Exception:
        pass
    return {"users": users, "cameras": list(CONFIG.get("cameras", {}).keys())}


@app.post("/api/users")
def api_user_create(req: UserCreateReq, request: Request):
    name = req.username.strip()
    if not _valid_username(name):
        raise HTTPException(status_code=400, detail="Tên đăng nhập 3-32 ký tự, chỉ chữ/số/._-")
    if name == NVR_USER:
        raise HTTPException(status_code=400, detail="Trùng tài khoản gốc")
    if get_user(name) is not None:
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu cần ít nhất 6 ký tự")
    role = req.role if req.role in ("admin", "user") else "user"
    salt, ph = hash_password(req.password)
    conn = db_conn()
    conn.execute(
        "INSERT INTO users (username, pass_hash, salt, role, perms, cameras, enabled, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, ph, salt, role, json.dumps(_clean_perms(req.perms)), json.dumps(req.cameras or []),
         1 if req.enabled else 0, int(time.time())),
    )
    conn.commit()
    audit("user_create", name, client_ip(request), f"vai trò={role}")
    row = get_user(name)
    if row is None:
        raise HTTPException(status_code=500, detail="Không tạo được tài khoản")
    return {"status": "ok", "user": _row_to_user(row)}


@app.post("/api/users/{name}")
def api_user_update(name: str, req: UserUpdateReq, request: Request):
    if name == NVR_USER:
        raise HTTPException(status_code=400, detail="Không sửa được tài khoản gốc (dùng biến môi trường)")
    row = get_user(name)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    fields, values = [], []
    if req.role is not None:
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Vai trò không hợp lệ")
        fields.append("role=?"); values.append(req.role)
    if req.perms is not None:
        fields.append("perms=?"); values.append(json.dumps(_clean_perms(req.perms)))
    if req.cameras is not None:
        fields.append("cameras=?"); values.append(json.dumps([str(x) for x in req.cameras]))
    if req.enabled is not None:
        fields.append("enabled=?"); values.append(1 if req.enabled else 0)
    if req.password is not None:
        if len(req.password) < 6:
            raise HTTPException(status_code=400, detail="Mật khẩu cần ít nhất 6 ký tự")
        salt, ph = hash_password(req.password)
        fields += ["salt=?", "pass_hash=?"]; values += [salt, ph]
    if not fields:
        raise HTTPException(status_code=400, detail="Không có gì để cập nhật")
    values.append(name)
    conn = db_conn()
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE username=?", values)
    if req.password is not None or req.enabled is False:
        conn.execute("DELETE FROM sessions WHERE username=?", (name,))
    conn.commit()
    audit("user_update", name, client_ip(request))
    row = get_user(name)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    return {"status": "ok", "user": _row_to_user(row)}


@app.delete("/api/users/{name}")
def api_user_delete(name: str, request: Request):
    if name == NVR_USER:
        raise HTTPException(status_code=400, detail="Không xoá được tài khoản gốc")
    conn = db_conn()
    cur = conn.execute("DELETE FROM users WHERE username=?", (name,))
    conn.execute("DELETE FROM sessions WHERE username=?", (name,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    audit("user_delete", name, client_ip(request))
    return {"status": "ok"}


@app.post("/api/users/{name}/totp_off")
def api_user_totp_off(name: str, request: Request):
    if get_user(name) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    conn = db_conn()
    conn.execute("UPDATE users SET totp_enabled=0, totp_secret='' WHERE username=?", (name,))
    conn.commit()
    audit("totp_disable", name, client_ip(request), "admin tắt 2FA")
    return {"status": "ok"}


@app.get("/api/audit")
def api_audit(limit: int = 100):
    limit = max(1, min(int(limit), 500))
    out = []
    try:
        c = db_conn().cursor()
        c.execute("SELECT ts, username, ip, action, detail FROM auth_events ORDER BY id DESC LIMIT ?", (limit,))
        out = [{"t": r[0], "username": r[1], "ip": r[2], "action": r[3], "detail": r[4]} for r in c.fetchall()]
    except Exception:
        pass
    return {"events": out, "recent": list(security_events)[-limit:]}


@app.get("/api/security/status")
def api_security_status():
    warnings = []
    n_users = n_admins = n_totp = n_sessions = 0
    if not AUTH_ENABLED:
        warnings.append("Xác thực đang TẮT (NVR_AUTH=off) — bất kỳ ai vào mạng đều xem/khoá/xoá được bản ghi.")
    else:
        if NVR_PASS == "admin":
            warnings.append("Mật khẩu tài khoản gốc vẫn là mặc định 'admin' — đổi NVR_PASS trong docker-compose.")
        try:
            c = db_conn().cursor()
            c.execute("SELECT COUNT(*) FROM users")
            n_users = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1")
            n_admins = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE totp_enabled=1")
            n_totp = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM sessions WHERE expires > ?", (int(time.time()),))
            n_sessions = c.fetchone()[0]
        except Exception:
            n_users = n_admins = n_totp = n_sessions = 0
        if n_admins == 0 and n_users > 0:
            warnings.append("Không còn tài khoản con nào là quản trị viên.")
    return {
        "auth_enabled": AUTH_ENABLED,
        "root_user": NVR_USER,
        "users": n_users if AUTH_ENABLED else 0,
        "active_sessions": n_sessions if AUTH_ENABLED else 0,
        "totp_users": n_totp if AUTH_ENABLED else 0,
        "warnings": warnings,
    }


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"), headers={"Cache-Control": "no-store"})

app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")
app.mount("/snapshots", StaticFiles(directory=SNAPSHOTS_DIR), name="snapshots")
app.mount("/hls/_play", StaticFiles(directory=PLAYBACK_HLS_DIR), name="playback_hls")
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/version")
def get_version():
    return {"name": "LiteNVR Pro", "version": APP_VERSION}

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-store"})
