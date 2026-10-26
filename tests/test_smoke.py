import base64
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
SRC = os.path.join(APP_DIR, "main.py")


def load_app(env=None):
    os.environ["GO2RTC"] = "off"
    os.environ["NVR_AUTH"] = "on"
    os.environ["NVR_USER"] = "user1"
    os.environ["NVR_PASS"] = "pass1"
    if env:
        os.environ.update(env)
    sandbox = tempfile.mkdtemp(prefix="litenvr_test_")
    shm = os.path.join(sandbox, "shm")
    os.makedirs(shm, exist_ok=True)
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    src = src.replace('STORAGE_DIR = "/storage"', f'STORAGE_DIR = {sandbox!r}')
    src = src.replace('HLS_DIR = "/dev/shm/hls"', f'HLS_DIR = {os.path.join(shm, "hls")!r}')
    src = src.replace('BUFFER_DIR = "/dev/shm/buffer"', f'BUFFER_DIR = {os.path.join(shm, "buffer")!r}')
    mod_path = os.path.join(sandbox, "main.py")
    with open(mod_path, "w", encoding="utf-8") as f:
        f.write(src)
    static_src = os.path.join(APP_DIR, "static")
    if os.path.isdir(static_src):
        shutil.copytree(static_src, os.path.join(sandbox, "static"))
    cwd = os.getcwd()
    os.chdir(APP_DIR)
    try:
        spec = importlib.util.spec_from_file_location("litenvr_test_main", mod_path)
        m = importlib.util.module_from_spec(spec)
        sys.modules["litenvr_test_main"] = m
        spec.loader.exec_module(m)
    finally:
        os.chdir(cwd)
    return sandbox, m


def auth(u="user1", p="pass1"):
    tok = base64.b64encode(f"{u}:{p}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


class _AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.m.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)


class ApiTests(_AppCase):
    def test_health_and_auth(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/system").status_code, 401)
        self.assertEqual(self.client.get("/api/system", headers=auth("user1", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/api/system", headers=auth()).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(self.client.get("/api/version").status_code, 401)

    def test_version_endpoint(self):
        j = self.client.get("/api/version", headers=auth()).json()
        self.assertEqual(j["version"], self.m.APP_VERSION)
        self.assertEqual(j["name"], "LiteNVR Pro")
        sysinfo = self.client.get("/api/system", headers=auth()).json()
        self.assertEqual(sysinfo["version"], self.m.APP_VERSION)

    def test_camera_lifecycle(self):
        payload = {"cam_id": "cam1", "name": "Cam", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/x", "record_mode": "continuous"}
        self.assertEqual(self.client.post("/api/cameras", json=payload, headers=auth()).status_code, 200)
        self.assertIn("cam1", self.client.get("/api/cameras", headers=auth()).json())
        self.assertEqual(self.client.delete("/api/cameras/cam1", headers=auth()).status_code, 200)
        self.assertEqual(self.client.get("/api/cameras", headers=auth()).json(), {})

    def test_hls_ready_requires_running_live_worker(self):
        cam = "cam-hls-state"
        payload = {"cam_id": cam, "name": "HLS state", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/x", "record_mode": "off"}
        self.assertEqual(self.client.post("/api/cameras", json=payload, headers=auth()).status_code, 200)
        hls_dir = os.path.join(self.m.HLS_DIR, cam)
        os.makedirs(hls_dir, exist_ok=True)
        with open(os.path.join(hls_dir, "live.m3u8"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        st = self.m.cam_status.setdefault(cam, {})
        try:
            d = self.client.get(f"/api/status/{cam}", headers=auth()).json()
            self.assertFalse(d["hls_ready"])
            st["running"] = True
            st["_live_mode"] = True
            d = self.client.get(f"/api/status/{cam}", headers=auth()).json()
            self.assertTrue(d["hls_ready"])
            self.assertIsNotNone(d["hls_age_sec"])
            st["_live_mode"] = False
            d = self.client.get(f"/api/status/{cam}", headers=auth()).json()
            self.assertFalse(d["hls_ready"])
        finally:
            self.client.delete(f"/api/cameras/{cam}", headers=auth())
            self.m.cam_status.pop(cam, None)

    def test_webrtc_offer_proxy_adds_host_candidate(self):
        payload = {"cam_id": "cam-webrtc", "name": "Cam", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/x", "record_mode": "off"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        sdp = ("v=0\r\n"
               "o=- 0 0 IN IP4 127.0.0.1\r\n"
               "s=-\r\n"
               "t=0 0\r\n"
               "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
               "a=mid:0\r\n"
               "a=end-of-candidates\r\n")
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return sdp.encode()

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeResponse()

        old = self.m._go2rtc.get("running")
        self.m._go2rtc["running"] = True
        try:
            with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=fake_urlopen):
                r = self.client.post(
                    "/api/live/cam-webrtc/webrtc", content=sdp,
                    headers={**auth(), "Content-Type": "application/sdp",
                             "host": "192.168.5.63:9895"},
                )
        finally:
            self.m._go2rtc["running"] = old
            self.client.delete("/api/cameras/cam-webrtc", headers=auth())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("/api/webrtc?src=cam-webrtc", captured["url"])
        self.assertIn("192.168.5.63 8556", r.text)
        self.assertEqual(captured["data"], sdp.encode())

    def test_webrtc_prefers_substream(self):
        payload = {"cam_id": "cam-webrtc-sub", "name": "Cam", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/main",
                   "sub_rtsp_url": "rtsp://127.0.0.1:1/sub", "record_mode": "continuous"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=end-of-candidates\r\n"
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return sdp.encode()

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            return FakeResponse()

        old = self.m._go2rtc.get("running")
        self.m._go2rtc["running"] = True
        try:
            with mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
                r = self.client.post(
                    "/api/live/cam-webrtc-sub/webrtc", content=sdp,
                    headers={**auth(), "Content-Type": "application/sdp"})
        finally:
            self.m._go2rtc["running"] = old
            self.client.delete("/api/cameras/cam-webrtc-sub", headers=auth())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("/api/webrtc?src=cam-webrtc-sub_sub", captured["url"])
        self.assertEqual(r.headers["X-LiteNVR-WebRTC-Source"], "cam-webrtc-sub_sub")

    def test_webrtc_substream_404_retries_main(self):
        payload = {"cam_id": "cam-webrtc-404", "name": "Cam", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/main",
                   "sub_rtsp_url": "rtsp://127.0.0.1:1/sub", "record_mode": "continuous"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=end-of-candidates\r\n"
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return sdp.encode()

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            if "_sub" in req.full_url:
                raise self.m.urllib.error.HTTPError(
                    req.full_url, 404, "Not Found", {}, io.BytesIO(b"not found"))
            return FakeResponse()

        old = self.m._go2rtc.get("running")
        self.m._go2rtc["running"] = True
        try:
            with mock.patch.object(self.m.urllib.request, "urlopen", fake_urlopen):
                r = self.client.post(
                    "/api/live/cam-webrtc-404/webrtc", content=sdp,
                    headers={**auth(), "Content-Type": "application/sdp"})
        finally:
            self.m._go2rtc["running"] = old
            self.m._clear_stream_failures("cam-webrtc-404")
            self.client.delete("/api/cameras/cam-webrtc-404", headers=auth())
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("/api/webrtc?src=cam-webrtc-404_sub", calls[0])
        self.assertIn("/api/webrtc?src=cam-webrtc-404", calls[1])
        self.assertEqual(r.headers["X-LiteNVR-WebRTC-Source"], "cam-webrtc-404")

    def test_storage_motion_timeline(self):
        payload = {"cam_id": "cam1", "name": "Cam", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "motion", "video_codec": "h264"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        r = self.client.post("/api/storage", json={"retention_days": 3, "max_storage_gb": 1,
                                                   "max_disk_percent": 90, "post_motion_sec": 30},
                             headers=auth())
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/motion", json={"cam_id": "cam1", "label": "test"}, headers=auth())
        self.assertEqual(r.json()["pre_roll_sec"], 10)
        d = time.strftime("%Y-%m-%d")
        r = self.client.get(f"/api/timeline/cam1?date={d}", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("segments", r.json())

    def test_timeline_source_filter_and_smb_guard(self):
        cam = "srccam"
        self.client.post("/api/cameras", json={"cam_id": cam, "name": "Src",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x",
                                               "record_mode": "off"}, headers=auth())
        day = "2026-06-01"
        cam_dir = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(cam_dir, exist_ok=True)
        local_name = f"{day}_08-00-00.mp4"
        drive_name = f"{day}_09-00-00.mp4"
        with open(os.path.join(cam_dir, local_name), "wb") as f:
            f.write(b"")
        conn = self.m.db_conn()
        conn.execute("INSERT OR REPLACE INTO drive_uploads "
                     "(path, cam_id, name, ts, size, status, tries, next_try, updated) "
                     "VALUES (?,?,?,?,?, 'done', 0, 0, ?)",
                     (f"{cam}/{drive_name}", cam, drive_name, 0, 0, 0))
        conn.commit()
        self.client.post("/api/backup", json={"enabled": False, "kind": "folder"}, headers=auth())

        def names(source):
            r = self.client.get(f"/api/timeline/{cam}?date={day}&source={source}", headers=auth())
            self.assertEqual(r.status_code, 200, r.text)
            return [s["fname"] for s in r.json()["segments"]]

        self.assertEqual(names("local"), [local_name])
        self.assertEqual(names("drive"), [drive_name])
        self.assertEqual(names("auto"), [local_name, drive_name])
        with mock.patch.object(self.m, "_smb_list_recordings", return_value=[drive_name]):
            self.assertEqual(names("smb"), [drive_name])
        self.assertEqual(self.client.get(f"/api/timeline/{cam}?date={day}&source=bad",
                                         headers=auth()).status_code, 400)
        self.assertEqual(self.client.get(f"/api/timeline/{cam}?date=bad&source=local",
                                         headers=auth()).status_code, 400)
        self.assertEqual(self.client.get(
            f"/api/smb/play/{cam}/{day}/{local_name}", headers=auth()).status_code, 400)

        class FakeFH:
            def __init__(self, data):
                self.data = data
                self.pos = 0
                self.closed = False
            def seek(self, offset, whence=0):
                self.pos = len(self.data) if whence == os.SEEK_END else offset
                return self.pos
            def read(self, n=-1):
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk
            def close(self):
                self.closed = True

        fake_fh = FakeFH(b"0123456789")
        fake_target = mock.Mock()
        fake_target.open_recording.return_value = fake_fh
        with mock.patch.object(self.m, "_smb_target_for_playback", return_value=fake_target):
            r = self.client.get(f"/api/smb/play/{cam}/{day}/{drive_name}",
                                headers={**auth(), "Range": "bytes=2-5"})
        self.assertEqual(r.status_code, 206, r.text)
        self.assertEqual(r.content, b"2345")
        self.assertEqual(r.headers["content-range"], "bytes 2-5/10")
        self.assertTrue(fake_fh.closed)
        fake_target.close.assert_called()
        self.client.delete(f"/api/cameras/{cam}", headers=auth())

    def test_ram_hygiene_moves_thumbs_to_disk_and_cleans_orphans(self):
        self.assertFalse(self.m.THUMBS_DIR.startswith("/dev/shm"))
        self.assertTrue(self.m.THUMBS_DIR.startswith(self.m.STORAGE_DIR))
        self.assertGreaterEqual(self.m.MOTION_BUFFER_KEEP_SEC, 25)

        os.makedirs(self.m.LEGACY_THUMBS_DIR, exist_ok=True)
        with open(os.path.join(self.m.LEGACY_THUMBS_DIR, "old.jpg"), "wb") as f:
            f.write(b"x")
        hls_orphan = os.path.join(self.m.HLS_DIR, "orphan-cam")
        buf_orphan = os.path.join(self.m.BUFFER_DIR, "orphan-cam")
        os.makedirs(hls_orphan, exist_ok=True)
        os.makedirs(buf_orphan, exist_ok=True)
        with open(os.path.join(hls_orphan, "live0.ts"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(buf_orphan, "old.mp4"), "wb") as f:
            f.write(b"x")

        self.m.cleanup_tmpfs()
        self.assertFalse(os.path.exists(self.m.LEGACY_THUMBS_DIR))
        self.assertFalse(os.path.exists(hls_orphan))
        self.assertFalse(os.path.exists(buf_orphan))

    def test_release_recording_page_cache_keeps_files(self):
        cam = "cache-release"
        rec = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(rec, exist_ok=True)
        fp = os.path.join(rec, "2026-01-01_00-00-00.mp4")
        with open(fp, "wb") as f:
            f.write(b"x" * 1024)
        old = time.time() - 60
        os.utime(fp, (old, old))
        self.m.release_recording_page_cache(min_age=30, max_age=600)
        self.assertTrue(os.path.isfile(fp))

    def test_prune_thumbnail_cache(self):
        cam_dir = os.path.join(self.m.THUMBS_DIR, "cache-cam")
        os.makedirs(cam_dir, exist_ok=True)
        for i in range(3):
            with open(os.path.join(cam_dir, f"{i}.jpg"), "wb") as f:
                f.write(b"x" * 1024)
        self.assertEqual(self.m.prune_thumbnail_cache(0), 3)
        self.assertEqual(os.listdir(cam_dir), [])

    def test_motion_preroll_promotes_completed_buffer(self):
        cam = "preroll-test"
        bdir = os.path.join(self.m.buffer_root(), cam)
        rdir = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(bdir, exist_ok=True)
        now = time.time()
        paths = {}
        for offset, key in ((-30, "old"), (-15, "recent"), (0, "active")):
            t = datetime.fromtimestamp(now + offset)
            name = t.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
            path = os.path.join(bdir, name)
            with open(path, "wb") as f:
                f.write(b"x")
            os.utime(path, (now + offset, now + offset))
            paths[key] = path

        moved = self.m.promote_motion_buffer(cam, now - self.m.MOTION_PREROLL_SEC)
        self.assertEqual(moved, 1)
        self.assertTrue(os.path.isfile(os.path.join(rdir, os.path.basename(paths["recent"]))))
        self.assertTrue(os.path.isfile(paths["active"]))
        self.assertTrue(os.path.isfile(paths["old"]))

        self.m.promote_motion_buffer(cam)
        self.assertTrue(os.path.isfile(os.path.join(rdir, os.path.basename(paths["old"]))))
        self.assertTrue(os.path.isfile(paths["active"]))
        shutil.rmtree(bdir, ignore_errors=True)
        shutil.rmtree(rdir, ignore_errors=True)

    def test_play_missing_and_status(self):
        payload = {"cam_id": "cam1", "name": "Cam", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "off"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        r = self.client.get("/api/status/cam1", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("running", r.json())
        self.assertEqual(self.client.get("/api/play/cam1/nope.mp4", headers=auth()).status_code, 404)

    def test_recording_days_and_camera_test_api(self):
        payload = {"cam_id": "calcam", "name": "Cal", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "off"}
        self.assertEqual(self.client.post("/api/cameras", json=payload, headers=auth()).status_code, 200)
        cam_dir = os.path.join(self.m.RECORDINGS_DIR, "calcam")
        os.makedirs(cam_dir, exist_ok=True)
        fname = "2026-05-04_08-00-00.mp4"
        with open(os.path.join(cam_dir, fname), "wb") as f:
            f.write(b"")
        r = self.client.get("/api/recording_days/calcam?month=2026-05", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], ["2026-05-04"])
        self.assertEqual(self.client.get("/api/recording_days/calcam?month=2026-13",
                                         headers=auth()).status_code, 400)
        self.assertEqual(self.client.post("/api/cameras/test", json={"rtsp_url": ""},
                                          headers=auth()).status_code, 400)
        self.assertEqual(self.client.post("/api/cameras/test", json={"rtsp_url": "ftp://x"},
                                          headers=auth()).status_code, 400)
        self.assertFalse(self.m.camera_offline_state("calcam")["expected"])
        self.client.delete("/api/cameras/calcam", headers=auth())

    def test_stats(self):
        r = self.client.get("/api/stats", headers=auth())
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn("mem_total_mb", j)
        self.assertTrue(0 <= j["mem_percent"] <= 100)
        self.assertTrue(0 <= j["disk_percent"] <= 100)

    def test_memory_breakdown_includes_app_detail(self):
        mm = self.m._mem_breakdown()
        for key in ("app_rss_mb", "app_anon_mb", "app_file_mb", "app_peak_mb",
                    "app_threads", "notify_active", "notify_limit"):
            self.assertIn(key, mm)
        self.assertGreaterEqual(mm["notify_limit"], 1)

    def test_viewer_playback_permission(self):
        self.client.post("/api/cameras", json={"cam_id": "cam1", "name": "Cam",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        r = self.client.post("/api/users", json={
            "username": "viewer1", "password": "secret1", "role": "user",
            "perms": {"live": True, "playback": False, "events": False, "export": False, "ptz": False, "lock": False},
        }, headers=auth())
        self.assertEqual(r.status_code, 200)
        v = auth("viewer1", "secret1")
        self.assertEqual(self.client.get("/api/cameras", headers=v).status_code, 200)
        self.assertEqual(self.client.get("/api/drive/play/cam1/x.mp4", headers=v).status_code, 403)

    def test_config_export_import(self):
        self.assertEqual(self.client.get("/api/config/export", headers=auth()).status_code, 200)

    def test_camera_hass_entities_persist(self):
        payload = {"cam_id": "mo1", "name": "M", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "hass_entities": ["binary_sensor.cong", "binary_sensor.cong", " binary_sensor.san ",
                                     {"entity_id": "device_tracker.dt", "state": "home", "label": "Ai đó về"}]}
        self.client.post("/api/cameras", json=payload, headers=auth())
        stored = self.client.get("/api/cameras", headers=auth()).json()["mo1"]
        self.assertEqual([e["entity_id"] for e in stored["hass_entities"]],
                         ["binary_sensor.cong", "binary_sensor.san", "device_tracker.dt"])
        self.assertEqual(stored["hass_entities"][2]["state"], "home")
        self.client.delete("/api/cameras/mo1", headers=auth())

    def test_hass_entity_helpers(self):
        self.assertEqual(self.m._default_active_state("binary_sensor.x"), "on")
        self.assertEqual(self.m._default_active_state("device_tracker.x"), "home")
        self.assertEqual(self.m._default_active_state("sensor.x"), "")
        self.assertEqual(self.m._hass_state_options("binary_sensor.x"), ["on", "off"])
        self.assertEqual(self.m._hass_state_options("person.a"), ["home", "not_home"])
        self.assertEqual(self.m._hass_state_options("sensor.x"), [])
        self.assertIsNone(self.m._norm_hass_entity(""))
        self.assertEqual(self.m._norm_hass_entity("binary_sensor.a"),
                         {"entity_id": "binary_sensor.a", "state": "", "label": ""})
        saved = self.m.CONFIG.get("cameras")
        self.m.CONFIG["cameras"] = {"c1": {"hass_entities": [
            {"entity_id": "binary_sensor.m", "state": "on", "label": "Cổng"}]}}
        try:
            rules = self.m._hass_rules({})
            self.assertEqual(rules["binary_sensor.m"],
                             {"cam_id": "c1", "state": "on", "label": "Cổng"})
        finally:
            self.m.CONFIG["cameras"] = saved

    def test_ai_detect_creates_labelled_event(self):
        payload = {"cam_id": "aicam", "name": "AI", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "motion"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        r = self.client.post("/api/ai/detect", json={"cam_id": "aicam", "objects": ["person", "car", "car"],
                                                     "snapshot_url": "ftp://nope/x.jpg"}, headers=auth())
        self.assertEqual(r.status_code, 200)
        conn = self.m.db_conn()
        row = conn.execute("SELECT label FROM events WHERE cam_id='aicam' ORDER BY timestamp DESC").fetchone()
        self.assertEqual(row[0], "AI: Người, Xe")
        conn.execute("DELETE FROM events WHERE cam_id='aicam'")
        conn.commit()
        self.client.delete("/api/cameras/aicam", headers=auth())

    def test_ai_event_label_mapping(self):
        self.assertEqual(self.m.ai_event_label(["truck", "unknown_obj"]), "AI: Xe tải, unknown_obj")
        self.assertEqual(self.m.ai_event_label([], "Tùy chỉnh"), "Tùy chỉnh")
        self.assertEqual(self.m.ai_event_label([]), "AI: đối tượng")

    def test_notify_disk_alert_settings_persist(self):
        r = self.client.post("/api/notify", json={"disk_alert_enabled": True, "disk_warn_percent": 80,
                                                  "disk_crit_percent": 90,
                                                  "public_url": "http://192.168.5.63:9895/"}, headers=auth())
        self.assertEqual(r.status_code, 200)
        j = self.client.get("/api/notify", headers=auth()).json()
        self.assertTrue(j["disk_alert_enabled"])
        self.assertEqual(j["disk_warn_percent"], 80)
        self.assertEqual(j["disk_crit_percent"], 90)
        self.assertEqual(j["public_url"], "http://192.168.5.63:9895")
        self.client.post("/api/notify", json={"disk_alert_enabled": False}, headers=auth())

    def test_notify_threads_are_capped(self):
        deadline = time.time() + 2
        while self.m._notify_active and time.time() < deadline:
            time.sleep(0.01)
        old_slots = self.m._notify_slots
        release = threading.Event()
        started = threading.Event()
        calls = []

        def fake_notify(*args):
            calls.append(args)
            started.set()
            release.wait(3)

        self.m._notify_slots = threading.BoundedSemaphore(1)
        try:
            with mock.patch.object(self.m, "notify_motion", side_effect=fake_notify):
                self.assertTrue(self.m._start_notify_thread("c1", "a", "", 1, 2))
                self.assertTrue(started.wait(1))
                self.assertFalse(self.m._start_notify_thread("c2", "b", "", 3, 4))
                self.assertFalse(self.m._start_notify_thread("c3", "c", "", 5, 6))
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            deadline = time.time() + 2
            while self.m._notify_active and time.time() < deadline:
                time.sleep(0.01)
            self.m._notify_slots = old_slots
        self.assertEqual(self.m._notify_active, 0)

    def test_ai_settings_persist_without_exposing_key(self):
        r = self.client.post("/api/ai", json={
            "enabled": False, "provider": "openai", "api_key": "sk-test-secret",
            "model": "gpt-6-luna", "base_url": "https://api.openai.com/v1",
            "prompt": "Mô tả thử", "timeout": 20,
        }, headers=auth())
        self.assertEqual(r.status_code, 200)
        j = self.client.get("/api/ai", headers=auth()).json()
        self.assertTrue(j["has_api_key"])
        self.assertEqual(j["provider"], "openai")
        self.assertEqual(j["model"], "gpt-6-luna")
        self.assertNotIn("api_key", j)
        self.assertTrue(any(p["id"] == "gemini" for p in j["providers"]))

    def test_ai_describe_snapshot_parses_gemini_response(self):
        path = os.path.join(tempfile.mkdtemp(prefix="litenvr_ai_"), "snap.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"x" * 64)
        cfg = {"enabled": True, "provider": "gemini", "api_key": "k", "model": "gemini-test",
               "base_url": "https://gemini.test/v1beta", "prompt": "Mô tả", "timeout": 10}
        fake = {"candidates": [{"content": {"parts": [{"text": "Người áo đỏ đi bộ."}]}}]}
        with mock.patch.object(self.m, "_ai_post_json", return_value=fake) as post:
            result = self.m._ai_describe_snapshot(path, cfg)
        self.assertEqual(result["description"], "Người áo đỏ đi bộ.")
        self.assertEqual(result["model"], "gemini-test")
        self.assertTrue(post.called)
        payload = post.call_args[0][1]
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 2048)
        self.assertNotIn("temperature", payload["generationConfig"])

    def test_ai_settings_migrates_retired_default_models(self):
        old = self.m.CONFIG.get("ai")
        cases = [
            ("gemini", "gemini-2.0-flash", "gemini-3.8-flash",
             "https://generativelanguage.googleapis.com/v1beta"),
            ("groq", "meta-llama/llama-4-scout-17b-16e-instruct",
             "qwen/qwen3.8-27b", "https://api.groq.com/openai/v1"),
        ]
        try:
            for provider, retired, current, base in cases:
                with self.subTest(provider=provider):
                    self.m.CONFIG["ai"] = {
                        "provider": provider, "model": retired, "base_url": base,
                    }
                    self.assertEqual(self.m._ai_settings()["model"], current)
        finally:
            if old is None:
                self.m.CONFIG.pop("ai", None)
            else:
                self.m.CONFIG["ai"] = old

    def test_ai_settings_keeps_retired_model_on_custom_base(self):
        old = self.m.CONFIG.get("ai")
        self.m.CONFIG["ai"] = {
            "provider": "gemini", "model": "gemini-2.0-flash",
            "base_url": "https://proxy.test/v1beta",
        }
        try:
            ai_cfg = self.m._ai_settings()
            self.assertEqual(ai_cfg["model"], "gemini-2.0-flash")
            self.assertEqual(ai_cfg["base_url"], "https://proxy.test/v1beta")
        finally:
            if old is None:
                self.m.CONFIG.pop("ai", None)
            else:
                self.m.CONFIG["ai"] = old

    def test_ai_post_json_sets_user_agent(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with mock.patch.object(
                self.m.urllib.request, "urlopen", return_value=response) as urlopen:
            self.m._ai_post_json("https://example.test", {"ping": True}, {}, 5)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.headers["User-agent"], f"LiteNVR/{self.m.APP_VERSION}")

    def test_ai_check_uses_current_gemini_payload(self):
        fake = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
        with mock.patch.object(self.m, "_ai_post_json", return_value=fake) as post:
            r = self.client.post("/api/ai/check", json={
                "enabled": False, "provider": "gemini", "api_key": "key",
                "model": "models/gemini-3.8-flash",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
                "prompt": "", "timeout": 5,
            }, headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            post.call_args[0][0],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent")
        payload = post.call_args[0][1]
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 1024)
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
        self.assertNotIn("temperature", payload["generationConfig"])

    def test_ai_check_openai_uses_completion_token_limit(self):
        fake = {"choices": [{"message": {"content": "OK"}}]}
        with mock.patch.object(self.m, "_ai_post_json", return_value=fake) as post:
            r = self.client.post("/api/ai/check", json={
                "enabled": False, "provider": "openai", "api_key": "key",
                "model": "gpt-6-luna", "base_url": "https://api.openai.com/v1",
                "prompt": "", "timeout": 5,
            }, headers=auth())
        self.assertEqual(r.status_code, 200)
        payload = post.call_args[0][1]
        self.assertEqual(payload["max_completion_tokens"], 1024)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("temperature", payload)

    def test_ai_check_custom_normalizes_url_and_skips_empty_auth(self):
        fake = {"choices": [{"message": {"content": "OK"}}]}
        with mock.patch.object(self.m, "_ai_post_json", return_value=fake) as post:
            r = self.client.post("/api/ai/check", json={
                "enabled": False, "provider": "custom", "api_key": "",
                "model": "local-model",
                "base_url": "http://127.0.0.1:11434/v1/chat/completions",
                "prompt": "", "timeout": 5,
            }, headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_args[0][0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", post.call_args[0][2])
        self.assertEqual(post.call_args[0][1]["max_tokens"], 1024)
        self.assertNotIn("max_completion_tokens", post.call_args[0][1])

    def test_ai_check_uses_form_values_without_saving(self):
        old = self.m.CONFIG.get("ai")
        self.m.CONFIG["ai"] = {
            "enabled": True, "provider": "gemini", "api_key": "saved-key",
            "model": "gemini-saved", "base_url": "https://gemini.test/v1beta",
            "prompt": "Mô tả", "timeout": 10,
        }
        fake = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
        try:
            with mock.patch.object(self.m, "_ai_post_json", return_value=fake) as post:
                r = self.client.post("/api/ai/check", json={
                    "enabled": False, "provider": "gemini", "api_key": "",
                    "model": "gemini-check", "base_url": "https://gemini.test/v1beta",
                    "prompt": "", "timeout": 5,
                }, headers=auth())
            self.assertEqual(r.status_code, 200)
            j = r.json()
            self.assertTrue(j["valid"])
            self.assertEqual(j["model"], "gemini-check")
            self.assertEqual(j["message"], "OK")
            self.assertIn("gemini-check:generateContent", post.call_args[0][0])
            self.assertEqual(post.call_args[0][2]["x-goog-api-key"], "saved-key")
            self.assertEqual(self.m._ai_settings()["model"], "gemini-saved")
        finally:
            if old is None:
                self.m.CONFIG.pop("ai", None)
            else:
                self.m.CONFIG["ai"] = old

    def test_license_status_and_activation_cache(self):
        j = self.client.get("/api/license", headers=auth()).json()
        self.assertFalse(j["valid"])
        self.assertEqual(len(j["installation_id"]), 32)
        old = dict(self.m.CONFIG.get("license") or {})
        fake = {"valid": True, "token": "signed-token", "plan": "monthly",
                "expires_at": (datetime.now().astimezone()).replace(year=datetime.now().year + 1).isoformat()}
        try:
            with mock.patch.object(self.m, "_license_server_call", return_value=fake):
                self.m._license_activate("LITE-TEST-TEST-TEST")
            self.assertTrue(self.m._license_valid())
            status = self.client.get("/api/license", headers=auth()).json()
            self.assertTrue(status["valid"])
            self.assertEqual(status["plan"], "monthly")
        finally:
            self.m.CONFIG["license"] = old
            self.m.save_config(self.m.CONFIG)

    def test_license_blank_server_uses_default(self):
        old = self.m.CONFIG.get("license")
        self.m.CONFIG["license"] = {"server_url": ""}
        try:
            self.assertEqual(self.m._license_cfg()["server_url"],
                             "https://litenvr-license.vercel.app")
        finally:
            if old is None:
                self.m.CONFIG.pop("license", None)
            else:
                self.m.CONFIG["license"] = old

    def test_events_search_includes_ai_description(self):
        conn = self.m.db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot, ai_description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("cam1", int(time.time()), "2026-10-23", "12:00:00", "Chuyển động", None,
             "Người áo đỏ chở xe tải nhỏ"),
        )
        conn.commit()
        event_id = cur.lastrowid
        try:
            r = self.client.get("/api/events", params={"q": "áo đỏ"}, headers=auth())
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(any(e["id"] == event_id and "áo đỏ" in e["ai_description"]
                                for e in data["events"]))
        finally:
            conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            conn.commit()

    def test_backup_settings_persist(self):
        r = self.client.post("/api/backup", json={"enabled": True, "kind": "smb",
                                                  "smb_host": "nas.local", "smb_share": "Camera",
                                                  "smb_user": "nvr", "smb_pass": "secret",
                                                  "smb_dir": "LiteNVR", "mode": "move",
                                                  "smb_cleanup_enabled": True,
                                                  "smb_retention_days": 14,
                                                  "smb_max_gb": 250,
                                                  "smb_min_free_gb": 5,
                                                  "cameras": ["cam1"]}, headers=auth())
        self.assertEqual(r.status_code, 200)
        j = self.client.get("/api/backup", headers=auth()).json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["kind"], "smb")
        self.assertEqual(j["smb_host"], "nas.local")
        self.assertEqual(j["smb_share"], "Camera")
        self.assertTrue(j["has_smb_pass"])
        self.assertTrue(j["smb_cleanup_enabled"])
        self.assertEqual(j["smb_retention_days"], 14)
        self.assertEqual(j["smb_max_gb"], 250)
        self.assertEqual(j["smb_min_free_gb"], 5)
        self.assertEqual(j["mode"], "move")
        self.assertEqual(j["cameras"], ["cam1"])
        self.client.post("/api/backup", json={"enabled": False}, headers=auth())

    def test_events_search(self):
        os.makedirs(self.m.SNAPSHOTS_DIR, exist_ok=True)
        for name in ("s.jpg", "t.jpg"):
            with open(os.path.join(self.m.SNAPSHOTS_DIR, name), "wb") as f:
                f.write(b"x")
        conn = self.m.db_conn()
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)", ("camS", 100, "2026-01-02", "10-00-00", "Người lạ", "s.jpg"))
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)", ("camT", 200, "2026-02-03", "11-00-00", "Người quen", "t.jpg"))
        conn.commit()
        j = self.client.get("/api/events?cam_id=camS", headers=auth()).json()
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["events"][0]["label"], "Người lạ")
        self.assertEqual(j["events"][0]["snapshot"], "snapshots/s.jpg")
        self.assertEqual(j["events"][0]["time"], "10:00:00")
        self.assertEqual(j["events"][0]["sec_of_day"], 36000)
        self.assertEqual(self.client.get("/api/events?q=Người", headers=auth()).json()["count"], 2)
        self.assertEqual(self.client.get("/api/events?q=khong-co", headers=auth()).json()["count"], 0)
        j = self.client.get("/api/events?date_from=2026-02-01&date_to=2026-02-28", headers=auth()).json()
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["events"][0]["cam_id"], "camT")
        self.assertEqual(j["total"], 1)
        self.assertEqual(j["offset"], 0)

    def test_events_hide_missing_snapshot(self):
        conn = self.m.db_conn()
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)",
                     ("camV", 300, "2026-03-04", "12-00-00", "test", "missing.jpg"))
        conn.commit()
        j = self.client.get("/api/events?cam_id=camV", headers=auth()).json()
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["events"][0]["snapshot"], "")

    def test_events_star_and_label_filter(self):
        conn = self.m.db_conn()
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)", ("camStar", 300, "2026-03-01", "09-00-00", "Xe tải", "x.jpg"))
        conn.commit()
        eid = conn.execute("SELECT id FROM events WHERE cam_id='camStar'").fetchone()[0]
        r = self.client.post(f"/api/events/{eid}/star", json={"starred": True}, headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["starred"])
        only = self.client.get("/api/events?cam_id=camStar&starred=1", headers=auth()).json()
        self.assertEqual(only["count"], 1)
        self.assertTrue(only["events"][0]["starred"])
        none = self.client.get("/api/events?cam_id=camStar&starred=1&label=Không có", headers=auth()).json()
        self.assertEqual(none["count"], 0)
        self.assertEqual(self.client.post(f"/api/events/{eid}/star", json={"starred": False},
                                          headers=auth()).status_code, 200)
        self.assertEqual(self.client.get("/api/events?cam_id=camStar&starred=1", headers=auth()).json()["count"], 0)
        self.assertIn("Xe tải", self.client.get("/api/events/labels", headers=auth()).json()["labels"])
        self.assertEqual(self.client.post("/api/events/99999999/star", json={"starred": True},
                                          headers=auth()).status_code, 404)

    def test_events_pagination(self):
        conn = self.m.db_conn()
        for i in range(5):
            conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                         "VALUES (?,?,?,?,?,?)",
                         ("camPage", 1000 + i, "2026-04-01", "08:00:0%d" % i, "L%d" % i, ""))
        conn.commit()
        page = self.client.get("/api/events?cam_id=camPage&limit=2&offset=0", headers=auth()).json()
        self.assertEqual(page["count"], 2)
        self.assertEqual(page["total"], 5)
        page2 = self.client.get("/api/events?cam_id=camPage&limit=2&offset=2", headers=auth()).json()
        self.assertEqual(page2["count"], 2)
        self.assertNotEqual(page["events"][0]["id"], page2["events"][0]["id"])

    def test_camera_max_gb_and_min_free_percent(self):
        r = self.client.post("/api/cameras", json={"cam_id": "camGB", "name": "GB",
                                                   "rtsp_url": "rtsp://127.0.0.1:1/x", "max_gb": 5},
                             headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.CONFIG["cameras"]["camGB"]["max_gb"], 5.0)
        r = self.client.post("/api/storage", json={"retention_days": 7, "max_storage_gb": 0,
                                                   "max_disk_percent": 85, "post_motion_sec": 60,
                                                   "ram_buffer": True, "min_free_percent": 4}, headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.CONFIG["min_free_percent"], 4.0)
        sysinfo = self.client.get("/api/system", headers=auth()).json()
        self.assertEqual(sysinfo["min_free_percent"], 4.0)
        self.assertIn("recording_paused", sysinfo)
        self.assertGreaterEqual(sysinfo["host"]["cpu_count"], 1)
        self.assertIn("uptime_sec", sysinfo["host"])
        self.client.delete("/api/cameras/camGB", headers=auth())

    def test_session_list_and_revoke(self):
        self.m.create_session("user1", "10.0.0.1")
        self.m.create_session("user1", "10.0.0.2")
        j = self.client.get("/api/me/sessions", headers=auth()).json()
        self.assertGreaterEqual(j["count"], 2)
        self.assertFalse(any(s["current"] for s in j["sessions"]))
        r = self.client.post("/api/me/sessions/revoke", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["revoked"], 2)
        self.assertEqual(self.client.get("/api/me/sessions", headers=auth()).json()["count"], 0)

    def test_login_page_cache_headers(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertIn("Đăng nhập", r.text)
        r2 = self.client.get("/", headers=auth())
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.headers.get("cache-control"), "no-store")

    def test_thumbnail_validation(self):
        self.assertEqual(self.client.get("/api/thumb/nope/x.mp4", headers=auth()).status_code, 404)
        self.client.post("/api/cameras", json={"cam_id": "camThumb", "name": "T", "rtsp_url": "rtsp://x"},
                         headers=auth())
        self.assertEqual(self.client.get("/api/thumb/camThumb/bad.txt", headers=auth()).status_code, 400)
        self.assertEqual(self.client.get("/api/thumb/camThumb/missing.mp4", headers=auth()).status_code, 404)
        self.client.delete("/api/cameras/camThumb", headers=auth())

    def test_thumbnail_generation(self):
        try:
            ffmpeg_ok = subprocess.call(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL) == 0
        except OSError:
            ffmpeg_ok = False
        if not ffmpeg_ok:
            self.skipTest("ffmpeg không có sẵn")
        self.client.post("/api/cameras", json={"cam_id": "camThumb2", "name": "T2", "rtsp_url": "rtsp://x"},
                         headers=auth())
        rec = os.path.join(self.m.RECORDINGS_DIR, "camThumb2")
        os.makedirs(rec, exist_ok=True)
        subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                               "-t", "2", "-pix_fmt", "yuv420p", os.path.join(rec, "2026-01-01_00-00-00.mp4")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = self.client.get("/api/thumb/camThumb2/2026-01-01_00-00-00.mp4", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertGreater(len(r.content), 0)
        self.client.delete("/api/cameras/camThumb2", headers=auth())

    def test_motion_fallback_snapshot(self):
        d = self.m.SNAPSHOTS_DIR
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "camF_1.jpg"), "w") as f:
            f.write("x")
        self.m.CONFIG.setdefault("cameras", {})["camF"] = {
            "name": "F", "rtsp_url": "rtsp://x", "record_mode": "motion"}
        acquired = 0
        while self.m._snapshot_slots.acquire(blocking=False):
            acquired += 1
        try:
            self.m.handle_motion("camF", "test")
        finally:
            for _ in range(acquired):
                self.m._snapshot_slots.release()
            self.m.CONFIG["cameras"].pop("camF", None)
        row = self.m.db_conn().execute(
            "SELECT snapshot FROM events WHERE cam_id='camF' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "camF_1.jpg")

    def test_latest_snapshot_fallback(self):
        d = self.m.SNAPSHOTS_DIR
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "camX_100.jpg")
        with open(p, "w") as f:
            f.write("x")
        self.assertEqual(self.m._latest_snapshot("camX"), p)
        self.assertEqual(self.m._latest_snapshot("nope"), "")

    def test_snapshot_ignores_empty_and_partial_files(self):
        d = self.m.SNAPSHOTS_DIR
        os.makedirs(d, exist_ok=True)
        good = os.path.join(d, "camZ_1.jpg")
        empty = os.path.join(d, "camZ_2.jpg")
        partial = os.path.join(d, "camZ_3.jpg.part")
        for path, data in ((good, b"ok"), (empty, b""), (partial, b"ok")):
            with open(path, "wb") as f:
                f.write(data)
        self.assertEqual(self.m._latest_snapshot("camZ"), good)
        self.assertFalse(self.m._valid_snapshot(empty))

    def test_snapshot_capture_updates_event_after_file_ready(self):
        conn = self.m.db_conn()
        cur = conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label) "
                           "VALUES (?,?,?,?,?)", ("camY", 1, "2026-01-01", "00-00-01", "test"))
        conn.commit()
        event_id = cur.lastrowid
        target = os.path.join(self.m.SNAPSHOTS_DIR, "camY_capture.jpg")

        class FakeProc:
            returncode = 0
            def __init__(self, cmd, **kwargs):
                with open(cmd[-1], "wb") as f:
                    f.write(b"jpg")
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0
            def terminate(self):
                pass
            def kill(self):
                pass

        real = subprocess.Popen
        self.assertTrue(self.m._snapshot_slots.acquire(blocking=False))
        subprocess.Popen = FakeProc
        try:
            self.m._capture_snapshot("camY", {"rtsp_url": "rtsp://x"}, target, event_id)
        finally:
            subprocess.Popen = real
        self.assertTrue(self.m._valid_snapshot(target))
        self.assertFalse(os.path.exists(target + ".part"))
        row = conn.execute("SELECT snapshot FROM events WHERE id=?", (event_id,)).fetchone()
        self.assertEqual(row[0], "camY_capture.jpg")

    def test_motion_without_snapshot_does_not_store_missing_path(self):
        self.m.CONFIG.setdefault("cameras", {})["camG"] = {
            "name": "G", "rtsp_url": "rtsp://x", "record_mode": "motion"}
        acquired = 0
        while self.m._snapshot_slots.acquire(blocking=False):
            acquired += 1
        try:
            self.m.handle_motion("camG", "test")
        finally:
            for _ in range(acquired):
                self.m._snapshot_slots.release()
            self.m.CONFIG["cameras"].pop("camG", None)
        row = self.m.db_conn().execute(
            "SELECT snapshot FROM events WHERE cam_id='camG' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNone(row[0])

    def test_camera_retention_api(self):
        self.client.post("/api/cameras", json={"cam_id": "camRet", "name": "R", "rtsp_url": "rtsp://x",
                                               "retention_days": 3}, headers=auth())
        cams = self.client.get("/api/cameras", headers=auth()).json()
        self.assertEqual(cams["camRet"]["retention_days"], 3)
        self.client.post("/api/cameras", json={"cam_id": "camRet", "name": "R", "rtsp_url": "rtsp://x",
                                               "retention_days": -5}, headers=auth())
        self.assertEqual(self.client.get("/api/cameras", headers=auth()).json()["camRet"]["retention_days"], 0)
        self.client.delete("/api/cameras/camRet", headers=auth())

    def test_cam_retention_days(self):
        self.m.CONFIG["retention_days"] = 7
        self.m.CONFIG.setdefault("cameras", {})["cA"] = {"retention_days": 2}
        self.m.CONFIG["cameras"]["cB"] = {}
        self.assertEqual(self.m.cam_retention_days("cA"), 2)
        self.assertEqual(self.m.cam_retention_days("cB"), 7)
        self.assertEqual(self.m.cam_retention_days("missing"), 7)
        self.m.CONFIG["cameras"].pop("cA"); self.m.CONFIG["cameras"].pop("cB")

    def test_per_camera_cleanup(self):
        cfg = self.m.CONFIG
        orig = (cfg.get("retention_days"), cfg.get("max_storage_gb"), cfg.get("max_disk_percent"))
        cfg["retention_days"] = 7
        cfg["max_storage_gb"] = 0
        cfg["max_disk_percent"] = 0
        cfg.setdefault("cameras", {})["camLong"] = {"retention_days": 30}
        cfg["cameras"]["camTiny"] = {"retention_days": 1}
        try:
            old = time.time() - 3 * 86400
            for cid in ("camLong", "camTiny"):
                d = os.path.join(self.m.RECORDINGS_DIR, cid)
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, "clip.mp4")
                with open(p, "w") as f:
                    f.write("x")
                os.utime(p, (old, old))
            self.m.run_cleanup_now()
            self.assertTrue(os.path.exists(os.path.join(self.m.RECORDINGS_DIR, "camLong", "clip.mp4")))
            self.assertFalse(os.path.exists(os.path.join(self.m.RECORDINGS_DIR, "camTiny", "clip.mp4")))
        finally:
            cfg["retention_days"], cfg["max_storage_gb"], cfg["max_disk_percent"] = orig
            cfg["cameras"].pop("camLong", None); cfg["cameras"].pop("camTiny", None)


    def test_permission_paths_for_events_and_playback_users(self):
        cam = "permcam"
        self.client.post("/api/cameras", json={"cam_id": cam, "name": "Perm",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        users = {
            "eventsonly": {"live": False, "playback": False, "events": True, "export": False, "ptz": False, "lock": False},
            "playonly": {"live": False, "playback": True, "events": False, "export": False, "ptz": False, "lock": False},
            "liveonly": {"live": True, "playback": False, "events": False, "export": False, "ptz": False, "lock": False},
        }
        for name, perms in users.items():
            r = self.client.post("/api/users", json={"username": name, "password": "secret1",
                                                     "role": "user", "perms": perms,
                                                     "cameras": [cam]}, headers=auth())
            self.assertEqual(r.status_code, 200)
        snap_dir = self.m.SNAPSHOTS_DIR
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, cam + "_event.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        ev = auth("eventsonly", "secret1")
        self.assertEqual(self.client.get("/api/cameras", headers=ev).status_code, 200)
        self.assertEqual(self.client.get("/api/events", headers=ev).status_code, 200)
        self.assertEqual(self.client.get(f"/api/timeline/{cam}?date=2020-01-01", headers=ev).status_code, 403)
        self.assertEqual(self.client.post(f"/api/live/{cam}", headers=ev).status_code, 403)
        self.assertEqual(self.client.get(f"/snapshots/{cam}_event.jpg", headers=ev).status_code, 200)
        po = auth("playonly", "secret1")
        self.assertEqual(self.client.get("/api/cameras", headers=po).status_code, 200)
        self.assertEqual(self.client.get("/api/events", headers=po).status_code, 200)
        self.assertEqual(self.client.get(f"/api/timeline/{cam}?date=2020-01-01", headers=po).status_code, 200)
        self.assertEqual(self.client.post(f"/api/live/{cam}", headers=po).status_code, 403)
        self.assertEqual(self.client.get(f"/snapshots/{cam}_event.jpg", headers=po).status_code, 200)
        lo = auth("liveonly", "secret1")
        self.assertEqual(self.client.get(f"/snapshots/{cam}_event.jpg", headers=lo).status_code, 200)
        self.client.post(f"/api/live/{cam}", headers=lo)

    def test_snapshot_permission_maps_filename_to_camera(self):
        for cam in ("snapa", "snapb"):
            self.client.post("/api/cameras", json={"cam_id": cam, "name": cam,
                                                   "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        self.client.post("/api/users", json={"username": "snapuser", "password": "secret1",
                                             "role": "user",
                                             "perms": {"live": True, "playback": False, "events": False,
                                                       "export": False, "ptz": False, "lock": False},
                                             "cameras": ["snapa"]}, headers=auth())
        os.makedirs(self.m.SNAPSHOTS_DIR, exist_ok=True)
        for cam in ("snapa", "snapb"):
            with open(os.path.join(self.m.SNAPSHOTS_DIR, cam + "_x.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")
        u = auth("snapuser", "secret1")
        self.assertEqual(self.client.get("/snapshots/snapa_x.jpg", headers=u).status_code, 200)
        self.assertEqual(self.client.get("/snapshots/snapb_x.jpg", headers=u).status_code, 403)

    def test_camera_snapshot_endpoint_and_scope(self):
        for cam in ("snapapi1", "snapapi2"):
            self.client.post("/api/cameras", json={"cam_id": cam, "name": cam,
                                                   "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        self.client.post("/api/users", json={"username": "snapapiuser", "password": "secret1",
                                             "role": "user",
                                             "perms": {"live": True, "playback": False, "events": False,
                                                       "export": False, "ptz": False, "lock": False},
                                             "cameras": ["snapapi1"]}, headers=auth())
        os.makedirs(self.m.SNAPSHOTS_DIR, exist_ok=True)
        for cam in ("snapapi1", "snapapi2"):
            with open(os.path.join(self.m.SNAPSHOTS_DIR, cam + "_latest.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")
        u = auth("snapapiuser", "secret1")
        r = self.client.get("/api/cameras/snapapi1/snapshot.jpg", headers=u)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("content-type"), "image/jpeg")
        self.assertEqual(r.content, b"\xff\xd8\xff\xd9")
        self.assertEqual(self.client.get("/api/cameras/snapapi2/snapshot.jpg", headers=u).status_code, 403)
        self.assertEqual(self.client.get("/api/cameras/nope/snapshot.jpg", headers=u).status_code, 404)

    def test_hls_playlist_waits_for_first_segment(self):
        cam = "hlswaitapi"
        self.client.post("/api/cameras", json={"cam_id": cam, "name": cam,
                                                "rtsp_url": "rtsp://127.0.0.1:1/x",
                                                "record_mode": "off"}, headers=auth())
        cam_dir = os.path.join(self.m.HLS_DIR, cam)
        os.makedirs(cam_dir, exist_ok=True)
        playlist = os.path.join(cam_dir, "live.m3u8")
        def write_playlist():
            time.sleep(0.8)
            with open(playlist, "w") as f:
                f.write("#EXTM3U\n#EXT-X-VERSION:3\n")
        timer = threading.Timer(0, write_playlist)
        timer.start()
        try:
            started = time.monotonic()
            r = self.client.get(f"/hls/{cam}/live.m3u8", headers=auth())
            elapsed = time.monotonic() - started
            self.assertEqual(r.status_code, 200)
            self.assertGreaterEqual(elapsed, 0.5)
            self.assertIn("#EXTM3U", r.text)
        finally:
            timer.join()
            self.client.delete(f"/api/cameras/{cam}", headers=auth())
            shutil.rmtree(cam_dir, ignore_errors=True)

    def test_motion_ai_camera_scope(self):
        for cam in ("motiona", "motionb"):
            self.client.post("/api/cameras", json={"cam_id": cam, "name": cam,
                                                   "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        self.client.post("/api/users", json={"username": "motionuser", "password": "secret1",
                                             "role": "user",
                                             "perms": {"live": True, "playback": False, "events": False,
                                                       "export": False, "ptz": False, "lock": False},
                                             "cameras": ["motiona"]}, headers=auth())
        u = auth("motionuser", "secret1")
        self.assertEqual(self.client.post("/api/motion", json={"cam_id": "motiona"},
                                          headers=u).status_code, 200)
        self.assertEqual(self.client.post("/api/motion", json={"cam_id": "motionb"},
                                          headers=u).status_code, 403)
        self.assertEqual(self.client.post("/api/ai/detect", json={"cam_id": "motionb", "objects": ["person"]},
                                          headers=u).status_code, 403)

    def test_camera_id_rejects_traversal_and_markup(self):
        for bad in ("../evil", "a/b", "a\\b", "a'b", 'a"b', "a<b", "a`b"):
            r = self.client.post("/api/cameras", json={"cam_id": bad, "name": "Bad",
                                                       "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
            self.assertEqual(r.status_code, 400, bad)
        r = self.client.post("/api/cameras", json={"cam_id": "cam sân", "name": "Sân",
                                                   "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        self.assertEqual(r.status_code, 200)
        self.client.delete("/api/cameras/cam%20s%C3%A2n", headers=auth())

    def test_csrf_header_and_origin(self):
        from fastapi.testclient import TestClient
        token = self.m.create_session("user1", "testclient")
        c = TestClient(self.m.app)
        try:
            c.cookies.set(self.m.SESSION_COOKIE, token)
            r = c.post("/api/storage/clean")
            self.assertEqual(r.status_code, 403)
            r = c.post("/api/storage/clean", headers={"Origin": "http://evil.test"})
            self.assertEqual(r.status_code, 403)
            r = c.post("/api/storage/clean", headers={"Origin": "http://testserver"})
            self.assertEqual(r.status_code, 200)
            r = c.post("/api/storage/clean", headers={"X-NVR-CSRF": "1"})
            self.assertEqual(r.status_code, 200)
        finally:
            c.close()

    def test_basic_auth_blocked_for_totp_account(self):
        from fastapi.testclient import TestClient
        username, password, secret = "tfa1", "secret1", "JBSWY3DPEHPK3PXP"
        r = self.client.post("/api/users", json={"username": username, "password": password,
                                                 "role": "user", "perms": {}, "cameras": []},
                             headers=auth())
        self.assertEqual(r.status_code, 200)
        conn = self.m.db_conn()
        conn.execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE username=?",
                     (secret, username))
        conn.commit()
        c = TestClient(self.m.app)
        try:
            self.assertEqual(c.get("/api/system", headers=auth(username, password)).status_code, 401)
            r = c.post("/api/login", json={"username": username, "password": password})
            self.assertEqual(r.status_code, 401)
            self.assertTrue(r.json().get("otp_required"))
            r = c.post("/api/login", json={"username": username, "password": password,
                                           "otp": self.m.totp_code(secret)})
            self.assertEqual(r.status_code, 200)
        finally:
            c.close()
        self.client.delete("/api/users/" + username, headers=auth())

    def test_safe_remote_url_blocks_internal_targets(self):
        self.assertFalse(self.m._safe_remote_url("http://127.0.0.1/x.jpg"))
        self.assertFalse(self.m._safe_remote_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(self.m._safe_remote_url("http://[::1]/x.jpg"))
        self.assertTrue(self.m._safe_remote_url("http://10.0.0.5/x.jpg"))
        self.assertTrue(self.m._safe_remote_url("http://192.168.1.20/x.jpg"))

    def test_hass_mqtt_settings_persist_and_mask_password(self):
        body = {
            "enabled": False,
            "base_url": "",
            "token": "",
            "entities": {},
            "mqtt": {
                "enabled": False,
                "host": "mqtt://192.168.1.10:1883",
                "port": 1883,
                "username": "mqtt-user",
                "password": "secret",
                "tls": False,
                "discovery_prefix": "homeassistant",
                "base_topic": "litenvr",
                "snapshot_interval": 15,
            },
        }
        self.assertEqual(self.client.post("/api/hass", json=body, headers=auth()).status_code, 200)
        mq = self.client.get("/api/hass", headers=auth()).json()["mqtt"]
        self.assertEqual(mq["host"], "192.168.1.10")
        self.assertTrue(mq["has_password"])
        self.assertNotIn("password", mq)

        body["mqtt"]["password"] = ""
        self.assertEqual(self.client.post("/api/hass", json=body, headers=auth()).status_code, 200)
        self.assertEqual(self.m.CONFIG["mqtt"]["password"], "secret")

        body["mqtt"]["enabled"] = True
        body["mqtt"]["discovery_prefix"] = "bad/#"
        self.assertEqual(self.client.post("/api/hass", json=body, headers=auth()).status_code, 400)
        self.assertEqual(self.m.CONFIG["mqtt"]["discovery_prefix"], "homeassistant")

        body["mqtt"]["enabled"] = False
        body["mqtt"]["discovery_prefix"] = "homeassistant"
        body["mqtt"]["host"] = ""
        body["mqtt"]["username"] = ""
        body["mqtt"]["password"] = ""
        self.assertEqual(self.client.post("/api/hass", json=body, headers=auth()).status_code, 200)

    def test_drive_scan_skips_active_segment_and_queues_once(self):
        cam = "drivecam"
        self.client.post("/api/cameras", json={"cam_id": cam, "name": "Drive",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        cam_dir = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(cam_dir, exist_ok=True)
        now = time.time()
        old = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now - 120)) + ".mp4"
        fresh = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now)) + ".mp4"
        for name, mtime in ((old, now - 120), (fresh, now)):
            fp = os.path.join(cam_dir, name)
            with open(fp, "wb") as f:
                f.write(b"x")
            os.utime(fp, (mtime, mtime))
        self.m.CONFIG.setdefault("gdrive", {})["since_ts"] = 0
        with mock.patch.object(self.m, "cam_uploads_drive", return_value=True):
            self.assertGreaterEqual(self.m.drive_scan_once(), 1)
            self.assertEqual(self.m.drive_scan_once(), 0)
        conn = self.m.db_conn()
        self.assertIsNotNone(conn.execute("SELECT 1 FROM drive_uploads WHERE path=?",
                                          (f"{cam}/{old}",)).fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM drive_uploads WHERE path=?",
                                       (f"{cam}/{fresh}",)).fetchone())

    def test_drive_parallel_upload_setting_is_clamped(self):
        old = dict(self.m.CONFIG.get("gdrive") or {})
        try:
            body = {"enabled": False, "parallel_uploads": 12}
            self.assertEqual(self.client.post("/api/drive", json=body, headers=auth()).status_code, 200)
            self.assertEqual(self.m.CONFIG["gdrive"]["parallel_uploads"], 12)
            self.assertEqual(self.client.get("/api/drive", headers=auth()).json()["parallel_uploads"], 12)
            body["parallel_uploads"] = self.m.DRIVE_MAX_PARALLEL + 1
            self.assertEqual(self.client.post("/api/drive", json=body, headers=auth()).status_code, 200)
            self.assertEqual(self.m.CONFIG["gdrive"]["parallel_uploads"], self.m.DRIVE_MAX_PARALLEL)
            self.assertEqual(self.client.get("/api/drive", headers=auth()).json()["parallel_uploads"],
                             self.m.DRIVE_MAX_PARALLEL)
            body["parallel_uploads"] = 0
            self.assertEqual(self.client.post("/api/drive", json=body, headers=auth()).status_code, 200)
            self.assertEqual(self.m.CONFIG["gdrive"]["parallel_uploads"], 1)
        finally:
            self.m.CONFIG["gdrive"] = old

    def test_cleanup_keeps_locked_pending_and_active_files(self):
        cam = "cleancam"
        self.client.post("/api/cameras", json={"cam_id": cam, "name": "Clean",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        cam_dir = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(cam_dir, exist_ok=True)
        now = time.time()
        old_ts = now - 3 * 86400
        names = {"normal": "normal.mp4", "locked": "locked.mp4",
                 "pending": "pending.mp4", "active": "active.mp4"}
        for key, name in names.items():
            fp = os.path.join(cam_dir, name)
            with open(fp, "wb") as f:
                f.write(b"x")
            os.utime(fp, ((old_ts if key != "active" else now), (old_ts if key != "active" else now)))
        self.assertTrue(self.m.lock_recording(cam, names["locked"], "test"))
        conn = self.m.db_conn()
        conn.execute("INSERT OR REPLACE INTO drive_uploads "
                     "(path, cam_id, name, ts, size, status, tries, next_try, updated) "
                     "VALUES (?, ?, ?, ?, 1, 'pending', 0, 0, ?)",
                     (f"{cam}/{names['pending']}", cam, names["pending"], old_ts, now))
        conn.commit()
        old_cfg = (self.m.CONFIG.get("retention_days"), self.m.CONFIG.get("max_storage_gb"),
                   self.m.CONFIG.get("max_disk_percent"))
        try:
            self.m.CONFIG["retention_days"] = 1
            self.m.CONFIG["max_storage_gb"] = 0
            self.m.CONFIG["max_disk_percent"] = 0
            self.m.run_cleanup_now()
        finally:
            self.m.CONFIG["retention_days"], self.m.CONFIG["max_storage_gb"], \
                self.m.CONFIG["max_disk_percent"] = old_cfg
        self.assertFalse(os.path.exists(os.path.join(cam_dir, names["normal"])))
        for key in ("locked", "pending", "active"):
            self.assertTrue(os.path.exists(os.path.join(cam_dir, names[key])), key)

class MqttUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()

    def test_discovery_payload_and_retained_cleanup(self):
        m = self.m
        saved_cameras = m.CONFIG.get("cameras", {})
        saved_mqtt = m.CONFIG.get("mqtt", {})
        m.CONFIG["cameras"] = {"cam-1": {"name": "Cổng trước"}}
        m.CONFIG["mqtt"] = {"enabled": True, "discovery_prefix": "homeassistant",
                            "base_topic": "litenvr"}
        try:
            topic, payload = m._mqtt_discovery_payload(
                "cam-1", {"name": "Cổng trước"}, "cam_1", m.CONFIG["mqtt"])
            self.assertEqual(topic, "homeassistant/camera/litenvr/cam_1/config")
            self.assertEqual(payload["unique_id"], "litenvr_camera_cam_1")
            self.assertEqual(payload["default_entity_id"], "camera.litenvr_cam_1")
            self.assertEqual(payload["topic"], "litenvr/camera/cam_1/image")
            self.assertEqual(payload["availability_topic"], "litenvr/status")
            self.assertEqual(payload["device"]["identifiers"], ["litenvr"])

            calls = []

            class FakeClient:
                def publish(self, topic, data, qos=0, retain=False):
                    calls.append((topic, data, qos, retain))

            old_topic = "homeassistant/camera/litenvr/old/config"
            old_image = "litenvr/camera/old/image"
            m._mqtt_save_known({"old": {"topic": old_topic, "image": old_image}})
            self.assertTrue(m._mqtt_publish_discovery(FakeClient(), m.CONFIG["mqtt"]))
            topics = [c[0] for c in calls]
            self.assertIn(topic, topics)
            self.assertIn(old_topic, topics)
            self.assertIn(old_image, topics)
            old_call = calls[topics.index(old_topic)]
            self.assertEqual(old_call[1], "")
            self.assertTrue(old_call[3])
            old_image_call = calls[topics.index(old_image)]
            self.assertEqual(old_image_call[1], "")
            self.assertTrue(old_image_call[3])
            self.assertEqual(set(m._mqtt_load_known()), {"cam_1"})
        finally:
            m.CONFIG["cameras"] = saved_cameras
            m.CONFIG["mqtt"] = saved_mqtt

    def test_snapshot_publish_deduplicates(self):
        m = self.m
        saved_cameras = m.CONFIG.get("cameras", {})
        saved_mqtt = m.CONFIG.get("mqtt", {})
        m.CONFIG["cameras"] = {"cam1": {"name": "Cam"}}
        m.CONFIG["mqtt"] = {"enabled": True, "base_topic": "litenvr"}
        path = os.path.join(m.MQTT_SNAPSHOT_DIR, "cam1.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        calls = []

        class FakeClient:
            def publish(self, topic, data, qos=0, retain=False):
                calls.append((topic, data, qos, retain))
                return type("Info", (), {"rc": 0})()

        old_client = m._mqtt_client
        old_connected = m.mqtt_state.get("connected")
        m._mqtt_last_payload.clear()
        try:
            with m._mqtt_lock:
                m._mqtt_client = FakeClient()
            m.mqtt_state["connected"] = True
            self.assertTrue(m._mqtt_publish_snapshot_path("cam1", path))
            self.assertFalse(m._mqtt_publish_snapshot_path("cam1", path))
            self.assertTrue(m._mqtt_publish_snapshot_path("cam1", path, force=True))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], "litenvr/camera/cam1/image")
            self.assertEqual(calls[0][1], b"\xff\xd8\xff\xd9")
            self.assertTrue(calls[0][3])
        finally:
            with m._mqtt_lock:
                m._mqtt_client = old_client
            m.mqtt_state["connected"] = old_connected
            m._mqtt_last_payload.clear()
            m.CONFIG["cameras"] = saved_cameras
            m.CONFIG["mqtt"] = saved_mqtt


class _FakeOnvifHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, text, status=200):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        host, port = self.server.server_address[0], self.server.server_port
        base = f"http://{host}:{port}"
        if "GetSystemDateAndTime" in body:
            self._send('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                       '<s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
                       '</s:Body></s:Envelope>')
        elif "GetDeviceInformation" in body:
            self._send('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                       '<s:Body><tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
                       '<tds:Manufacturer>Dahua</tds:Manufacturer><tds:Model>Cam X</tds:Model>'
                       '</tds:GetDeviceInformationResponse></s:Body></s:Envelope>')
        elif "GetCapabilities" in body:
            self._send(f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                       f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
                       f'xmlns:tt="http://www.onvif.org/ver10/schema">'
                       f'<s:Body><tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>'
                       f'<tt:XAddr>{base}/onvif/Media</tt:XAddr>'
                       f'</tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>')
        elif "GetProfiles" in body:
            self._send('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                       'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
                       'xmlns:tt="http://www.onvif.org/ver10/schema">'
                       '<s:Body><trt:GetProfilesResponse>'
                       '<trt:Profiles token="MainTok"><tt:Name>Main</tt:Name>'
                       '<tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>'
                       '<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>'
                       '</tt:VideoEncoderConfiguration></trt:Profiles>'
                       '<trt:Profiles token="SubTok"><tt:Name>Sub</tt:Name>'
                       '<tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>'
                       '<tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>'
                       '</tt:VideoEncoderConfiguration></trt:Profiles>'
                       '</trt:GetProfilesResponse></s:Body></s:Envelope>')
        elif "GetStreamUri" in body:
            token = re.search(r"ProfileToken>([^<]+)<", body)
            stream = "main" if token and token.group(1) == "MainTok" else "sub"
            self._send('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                       'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
                       'xmlns:tt="http://www.onvif.org/ver10/schema">'
                       '<s:Body><trt:GetStreamUriResponse><trt:MediaUri>'
                       f'<tt:Uri>rtsp://{host}:8554/{stream}</tt:Uri>'
                       '</trt:MediaUri></trt:GetStreamUriResponse></s:Body></s:Envelope>')
        else:
            self._send('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                       '<s:Body><s:Fault><s:Reason><s:Text>Unknown action</s:Text></s:Reason></s:Fault>'
                       '</s:Body></s:Envelope>', status=400)


class _FakeAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if getattr(self.server, "mode", "") == "digest":
            body = b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"/>'
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Digest realm="onvif", nonce="abc"')
            self.send_header("Content-Type", "application/soap+xml")
        else:
            body = b'{"error":"Unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OnvifTests(_AppCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.onvif = ThreadingHTTPServer(("127.0.0.2", 0), _FakeOnvifHandler)
        cls.onvif_port = cls.onvif.server_port
        cls.auth_json = ThreadingHTTPServer(("127.0.0.2", 0), _FakeAuthHandler)
        cls.auth_json.mode = "json"
        cls.auth_digest = ThreadingHTTPServer(("127.0.0.2", 0), _FakeAuthHandler)
        cls.auth_digest.mode = "digest"
        cls.onvif_thread = threading.Thread(target=cls.onvif.serve_forever, daemon=True)
        cls.onvif_thread.start()
        for srv in (cls.auth_json, cls.auth_digest):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.onvif, cls.auth_json, cls.auth_digest):
            srv.shutdown()
            srv.server_close()
        super().tearDownClass()

    def test_discovery_parser_and_scan_validation(self):
        sample = (
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            '<e:Body><d:ProbeMatches><d:ProbeMatch>'
            '<d:XAddrs>http://192.168.5.8/onvif/device_service</d:XAddrs>'
            '<d:Scopes>onvif://www.onvif.org/name/Front%20Gate</d:Scopes>'
            '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
        ).encode()
        dev = self.m._parse_discovery_response(sample, "192.168.5.8")
        self.assertEqual(dev["name"], "Front Gate")
        self.assertEqual(dev["ip"], "192.168.5.8")
        self.assertEqual(dev["port"], 80)
        self.assertTrue(dev["xaddr"].endswith("/onvif/device_service"))
        self.assertEqual(self.m._onvif_guess_brand("HANET", "IP Camera"), "hanet")
        self.assertEqual(
            self.m.mask_secrets("rtsp://admin:pw@192.168.5.9:554/live"),
            "rtsp://***:***@192.168.5.9:554/live",
        )
        self.assertEqual(
            self.m.mask_secrets("rtsp://192.168.5.184:554/user:admin;pwd:s3cret"),
            "rtsp://192.168.5.184:554/user:***;pwd:***",
        )
        hosts, label = self.m._parse_scan_targets("127.0.0.2/32")
        self.assertEqual(hosts, ["127.0.0.2"])
        self.assertEqual(label, "127.0.0.2/32")
        with self.assertRaises(ValueError):
            self.m._parse_scan_targets("8.8.8.0/24")
        with self.assertRaises(ValueError):
            self.m._parse_scan_targets("10.0.0.0/8")

    def test_profile_pick_by_resolution(self):
        profiles = [
            {"token": "a", "name": "HD", "width": 1920, "height": 1080, "uri": "rtsp://a"},
            {"token": "b", "name": "Low", "width": 640, "height": 360, "uri": "rtsp://b"},
        ]
        main, sub = self.m._onvif_pick_profiles(profiles)
        self.assertEqual(main["token"], "a")
        self.assertEqual(sub["token"], "b")

    def test_probe_profiles_and_auth_urls(self):
        d = self.m.onvif_probe("127.0.0.2", self.onvif_port, "user", "pa:ss@word")
        self.assertEqual(d["brand"], "imou")
        self.assertEqual(d["onvif_port"], self.onvif_port)
        self.assertIn("rtsp://user:pa%3Ass%40word@127.0.0.2:8554/main", d["main_rtsp_url"])
        self.assertIn("rtsp://user:pa%3Ass%40word@127.0.0.2:8554/sub", d["sub_rtsp_url"])
        self.assertEqual(len(d["profiles"]), 2)

    def test_scan_unicast(self):
        d = self.m.onvif_scan_network("127.0.0.2/32", timeout=1.0, ports=[self.onvif_port])
        self.assertEqual(d["scanned"], 1)
        self.assertEqual(len(d["devices"]), 1)
        self.assertEqual(d["devices"][0]["name"], "Dahua Cam X")

    def test_scan_rejects_plain_401_and_accepts_digest(self):
        bad = self.m.onvif_scan_network("127.0.0.2/32", timeout=1.0, ports=[self.auth_json.server_port])
        self.assertEqual(bad["devices"], [])
        good = self.m.onvif_scan_network("127.0.0.2/32", timeout=1.0, ports=[self.auth_digest.server_port])
        self.assertEqual(len(good["devices"]), 1)
        self.assertTrue(good["devices"][0]["auth_required"])

    def test_auto_add_and_duplicate(self):
        payload = {
            "devices": [{"host": "127.0.0.2", "port": self.onvif_port,
                         "xaddr": f"http://127.0.0.2:{self.onvif_port}/onvif/device_service",
                         "name": "Fake cam"}],
            "username": "admin", "password": "pw",
            "record_mode": "off", "storage_target": "hdd", "room": "Test", "tags": "onvif",
        }
        r = self.client.post("/api/onvif/auto_add", json=payload, headers=auth())
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["added"], 1, j)
        cam_id = j["cam_ids"][0]
        cam = self.m.CONFIG["cameras"][cam_id]
        self.assertEqual(cam["brand"], "imou")
        self.assertEqual(cam["record_mode"], "off")
        self.assertEqual(cam["room"], "Test")
        self.assertIn("rtsp://admin:pw@127.0.0.2:8554/main", cam["rtsp_url"])
        r2 = self.client.post("/api/onvif/auto_add", json=payload, headers=auth())
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["added"], 0)
        self.assertEqual(r2.json()["results"][0]["status"], "skipped")
        self.client.delete(f"/api/cameras/{cam_id}", headers=auth())


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()

    def _cam(self, **kw):
        cam = {"name": "C", "rtsp_url": "rtsp://cam.invalid/main", "sub_rtsp_url": "rtsp://cam.invalid/sub",
               "record_mode": "continuous", "video_codec": "auto"}
        cam.update(kw)
        return cam

    def _values(self, cmd, flag):
        return [cmd[i + 1] for i, a in enumerate(cmd) if a == flag]

    def test_filter_and_decoder_threads(self):
        base = self.m._ffmpeg_base_args()
        self.assertEqual(self._values(base, "-filter_threads"), ["2"])
        self.assertEqual(self._values(base, "-filter_complex_threads"), ["2"])
        opts = self.m._input_args("rtsp://cam.invalid/main", {"rtsp_transport": "udp"})
        self.assertEqual(self._values(opts, "-threads"), ["2"])
        self.assertEqual(self._values(opts, "-rtsp_transport"), ["udp"])
        self.assertNotIn("-rtsp_transport", self.m._input_args("http://cam.invalid/x", {}))
        no_audio = self.m._input_args("rtsp://cam.invalid/main", {"rtsp_transport": "tcp", "audio_mode": "none"})
        self.assertEqual(self._values(no_audio, "-allowed_media_types"), ["video"])
        with_audio = self.m._input_args("rtsp://cam.invalid/main", {"rtsp_transport": "tcp", "audio_mode": "aac"})
        self.assertNotIn("-allowed_media_types", with_audio)

    def test_only_required_sources(self):
        base = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="copy"), include_live=False)
        self.assertEqual(base.count("-i"), 1)
        off = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="off", video_codec="copy"), include_live=True)
        self.assertEqual(off.count("-i"), 1)
        self.assertIn("sub", off[off.index("-i") + 1])

    def test_substream_failure_falls_back_to_main(self):
        cam_id = "cam-source-subfail"
        cam = self._cam(rtsp_url="rtsp://cam.invalid/main", sub_rtsp_url="rtsp://cam.invalid/sub")
        self.m.CONFIG.setdefault("cameras", {})[cam_id] = cam
        old_running = self.m._go2rtc["running"]
        self.m._go2rtc["running"] = True
        try:
            main, sub = self.m.cam_source_urls(cam_id, cam)
            self.assertTrue(main.endswith("/" + cam_id))
            self.assertTrue(sub.endswith("/" + cam_id + "_sub"))
            self.m._mark_stream_failure(cam_id, cam, "sub", "test")
            main2, sub2 = self.m.cam_source_urls(cam_id, cam)
            self.assertEqual(main2, main)
            self.assertEqual(sub2, main2)
            cmd = self.m.build_ffmpeg_cmd(cam_id, cam, video_codec="h264", include_live=True)
            self.assertEqual(cmd.count("-i"), 1)
        finally:
            self.m._go2rtc["running"] = old_running
            self.m._clear_stream_failures(cam_id)
            self.m.CONFIG["cameras"].pop(cam_id, None)

    def test_restream_failure_uses_direct_source(self):
        cam_id = "cam-source-restream"
        cam = self._cam(rtsp_url="rtsp://cam.invalid/main", sub_rtsp_url="rtsp://cam.invalid/sub")
        self.m.CONFIG.setdefault("cameras", {})[cam_id] = cam
        old_running = self.m._go2rtc["running"]
        self.m._go2rtc["running"] = True
        try:
            self.m._mark_stream_failure(cam_id, cam, "restream", "test")
            main, sub = self.m.cam_source_urls(cam_id, cam)
            self.assertEqual(main, cam["rtsp_url"])
            self.assertEqual(sub, cam["sub_rtsp_url"])
        finally:
            self.m._go2rtc["running"] = old_running
            self.m._clear_stream_failures(cam_id)
            self.m.CONFIG["cameras"].pop(cam_id, None)

    def test_runtime_live_downscale(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="copy", video_codec="h264"))
        self.assertIn("-vf", cmd)
        vf = self._values(cmd, "-vf")[0]
        self.assertIn("min(", vf)
        self.assertIn("720", vf)
        self.assertIn("ih", vf)

    def test_stream_health_progress(self):
        self.m.cam_health.clear()
        self.assertTrue(self.m._note_progress("fps=25.0", "c1"))
        self.assertTrue(self.m._note_progress("frame=120", "c1"))
        self.assertTrue(self.m._note_progress("progress=continue", "c1"))
        self.assertTrue(self.m._note_progress("stream_0_0_q=-1.0", "c1"))
        self.assertTrue(self.m._note_progress("stream_1_0_q=-1.0", "c1"))
        self.assertFalse(self.m._note_progress("some rtsp error", "c1"))
        snap = self.m.health_snapshot()
        self.assertEqual(snap["c1"]["fps"], 25.0)
        self.assertEqual(snap["c1"]["state"], "continue")
        self.assertIsNotNone(snap["c1"]["age_sec"])
        self.assertIn("-progress", self.m.build_ffmpeg_cmd("cam", self._cam()))

    def test_audio_au_headers_is_recoverable_warning(self):
        line = "[rtsp @ 0x55a114f72400] Error parsing AU headers"
        self.assertTrue(self.m._is_audio_au_warning(line))
        self.assertFalse(self.m._is_audio_au_warning("Video: h264, yuvj420p"))

    def test_hls_low_latency_flags(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="copy", video_codec="copy"), include_live=True)
        flags = self._values(cmd, "-hls_flags")[0]
        self.assertIn("independent_segments", flags)
        self.assertIn("delete_segments", flags)
        self.assertIn("temp_file", flags)
        self.assertEqual(self._values(cmd, "-hls_time"), [str(self.m.LIVE_HLS_TIME)])
        self.assertEqual(self._values(cmd, "-hls_list_size"), [str(self.m.LIVE_HLS_LIST)])
        self.assertEqual(self._values(cmd, "-hls_delete_threshold"), ["1"])
        self.assertEqual(self._values(cmd, "-hls_allow_cache"), ["0"])
        self.assertLessEqual(self.m.LIVE_HLS_TIME, 1)
        self.assertLessEqual(self.m.LIVE_HLS_LIST, 4)

    def test_transcoded_live_forces_segment_keyframes(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="copy", video_codec="h264"), include_live=True)
        self.assertIn(f"n_forced*{self.m.LIVE_HLS_TIME}", self._values(cmd, "-force_key_frames")[0])
        copy_cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="copy", video_codec="copy"), include_live=True)
        self.assertNotIn("-force_key_frames", copy_cmd)

    def test_live_hls_transcodes_audio_to_aac(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="off", audio_mode="aac",
                                                       video_codec="copy"), include_live=True)
        self.assertIn("0:a:0?", cmd)
        self.assertEqual(self._values(cmd, "-c:a")[-1], "aac")
        silent = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="off", audio_mode="none",
                                                          video_codec="copy"), include_live=True)
        self.assertIn("-an", silent)
        self.assertNotIn("0:a:0?", silent)

    def test_webrtc_candidate_injection_and_config_port(self):
        sdp = ("v=0\r\n"
               "o=- 0 0 IN IP4 127.0.0.1\r\n"
               "s=-\r\n"
               "t=0 0\r\n"
               "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
               "a=mid:0\r\n"
               "a=end-of-candidates\r\n")
        out = self.m.inject_webrtc_candidates(sdp, "192.168.5.63", 8556)
        self.assertIn("a=candidate:9101 1 udp 2130706431 192.168.5.63 8556 typ host", out)
        self.assertIn("a=candidate:9102 1 tcp 2130706430 192.168.5.63 8556 typ host", out)
        self.assertLess(out.index("a=candidate:9101"), out.index("a=end-of-candidates"))
        self.m.write_go2rtc_config()
        with open(self.m.GO2RTC_CONFIG, encoding="utf-8") as f:
            cfg = f.read()
        self.assertIn(f'listen: ":{self.m.GO2RTC_WEBRTC_PORT}"', cfg)

    def test_backup_daily_path_and_smb_unc(self):
        m = self.m
        self.assertEqual(m._recording_day("2026-10-21_13-45-00.mp4"), "2026-10-21")
        target = object.__new__(m.SmbTarget)
        target._host = "nas.local"
        target._share = "Camera"
        target._base = "LiteNVR"
        self.assertEqual(target.path("2026-10-21", "cam1", "a.mp4"),
                         "\\\\nas.local\\Camera\\LiteNVR\\2026-10-21\\cam1\\a.mp4")

    def test_smb_stat_volume_uses_share(self):
        m = self.m

        class Client:
            def __init__(self):
                self.paths = []

            def stat_volume(self, path):
                self.paths.append(path)
                return type("Vol", (), {
                    "total_size": 10_000,
                    "actual_available_size": 4_000,
                    "caller_available_size": 3_000,
                })()

        target = object.__new__(m.SmbTarget)
        target._client = Client()
        target._host = "nas.local"
        target._share = "Camera"
        target._base = "LiteNVR"
        vol = target.stat_volume()
        self.assertEqual(target._client.paths, ["\\\\nas.local\\Camera\\LiteNVR"])
        self.assertEqual(vol["total"], 10_000)
        self.assertEqual(vol["free"], 4_000)
        self.assertEqual(vol["used"], 6_000)

    def test_smb_cleanup_only_tracked_backups(self):
        m = self.m
        saved_backup = m.CONFIG.get("backup")
        conn = m.db_conn()
        old = time.time() - 10 * 86400
        p_ok = "cleanA/2020-01-01_00-00-00.mp4"
        p_lock = "cleanB/2020-01-02_00-00-00.mp4"

        class FakeSmb:
            def __init__(self):
                self.removed = []

            def path(self, *parts):
                return "\\\\nas\\Camera\\" + "\\".join(str(p) for p in parts if p)

            def stat_volume(self):
                return {"total": 100 * 1024 ** 3, "free": 0, "used": 100 * 1024 ** 3}

            def remove(self, path):
                self.removed.append(path)

            def close(self):
                pass

        try:
            conn.execute("DELETE FROM backup_done WHERE path IN (?,?)", (p_ok, p_lock))
            conn.execute("DELETE FROM locks WHERE path IN (?,?)", (p_ok, p_lock))
            conn.execute(
                "INSERT OR REPLACE INTO backup_done (path, cam_id, size, ts, dest, deleted, deleted_ts) "
                "VALUES (?,?,?,?,'smb',0,NULL)", (p_ok, "cleanA", 10, old))
            conn.execute(
                "INSERT OR REPLACE INTO backup_done (path, cam_id, size, ts, dest, deleted, deleted_ts) "
                "VALUES (?,?,?,?,'smb',0,NULL)", (p_lock, "cleanB", 10, old))
            conn.execute("INSERT OR REPLACE INTO locks (path, cam_id, created, note) VALUES (?,?,?,?)",
                         (p_lock, "cleanB", int(old), "giữ"))
            conn.commit()
            m.CONFIG["backup"] = {
                "enabled": True, "kind": "smb", "smb_host": "nas.local", "smb_share": "Camera",
                "smb_cleanup_enabled": True, "smb_retention_days": 0,
                "smb_max_gb": 0, "smb_min_free_gb": 1,
            }
            fake = FakeSmb()
            res = m.smb_cleanup(force=True, target=fake)
            self.assertEqual(res["deleted"], 1)
            self.assertEqual(fake.removed,
                             ["\\\\nas\\Camera\\2020-01-01\\cleanA\\2020-01-01_00-00-00.mp4"])
            rows = {r["path"]: r for r in conn.execute(
                "SELECT path, deleted FROM backup_done WHERE path IN (?,?)", (p_ok, p_lock)).fetchall()}
            self.assertEqual(rows[p_ok]["deleted"], 1)
            self.assertEqual(rows[p_lock]["deleted"], 0)
        finally:
            conn.execute("DELETE FROM backup_done WHERE path IN (?,?)", (p_ok, p_lock))
            conn.execute("DELETE FROM locks WHERE path IN (?,?)", (p_ok, p_lock))
            conn.commit()
            m.CONFIG["backup"] = saved_backup or {}

    def test_drive_upload_folder_groups_by_day(self):
        m = self.m
        orig_cams = m.CONFIG.get("cameras")
        orig_root = m.drive_root_folder
        orig_ensure = m.drive_ensure_folder
        calls = []
        m.CONFIG["cameras"] = {"cam1": {"name": "Cổng"}}
        m.drive_root_folder = lambda: "root-id"
        m.drive_ensure_folder = lambda name, parent="": (calls.append((name, parent)) or f"{name}-id")
        try:
            fid = m.drive_upload_folder("cam1", "2026-10-21")
        finally:
            m.CONFIG["cameras"] = orig_cams
            m.drive_root_folder = orig_root
            m.drive_ensure_folder = orig_ensure
        self.assertEqual(fid, "Cổng-id")
        self.assertEqual(calls, [("2026-10-21", "root-id"), ("Cổng", "2026-10-21-id")])

    def test_hass_rules_merges_camera_entities(self):
        saved = self.m.CONFIG.get("cameras")
        self.m.CONFIG["cameras"] = {"c1": {"hass_entities": ["binary_sensor.a"]}}
        try:
            rules = self.m._hass_rules({"c2": [{"entity_id": "binary_sensor.b", "label": "x"}]})
        finally:
            self.m.CONFIG["cameras"] = saved
        self.assertEqual(rules["binary_sensor.a"]["cam_id"], "c1")
        self.assertEqual(rules["binary_sensor.b"]["cam_id"], "c2")
        self.assertEqual(rules["binary_sensor.b"]["label"], "x")

    def test_hass_motion_triggered_only_on_edge(self):
        f = self.m._hass_motion_triggered
        self.assertTrue(f({"state": "on"}, "binary_sensor.x", "on", "off"))
        self.assertFalse(f({"state": "on"}, "binary_sensor.x", "on", "on"))
        self.assertFalse(f({"state": "on"}, "binary_sensor.x", "off", "on"))
        self.assertTrue(f({"state": ""}, "binary_sensor.x", "on", "off"))
        self.assertFalse(f({"state": ""}, "binary_sensor.x", "on", "on"))
        self.assertTrue(f({}, "device_tracker.x", "home", "not_home"))
        self.assertFalse(f({}, "device_tracker.x", "not_home", "home"))
        self.assertFalse(f({"state": "on"}, "binary_sensor.x", "ON", "ON"))

    def test_disk_health_and_alert_level(self):
        h = self.m.disk_health()
        self.assertIn("percent", h)
        self.assertTrue(h["writable"])
        self.assertIn("smart", h)
        self.assertEqual(self.m.disk_alert_level(50, 85, 95), "")
        self.assertEqual(self.m.disk_alert_level(90, 85, 95), "warn")
        self.assertEqual(self.m.disk_alert_level(99, 85, 95), "crit")

    def test_camera_storage_choices_update_smb_membership(self):
        m = self.m
        saved_cams = m.CONFIG.get("cameras")
        saved_backup = m.CONFIG.get("backup")
        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        client.__enter__()
        try:
            m.CONFIG["cameras"] = {}
            m.CONFIG["backup"] = {"enabled": False, "kind": "smb",
                                  "cameras": ["camS1", "camS2"], "cameras_explicit": True}
            base = {"brand": "custom", "rtsp_url": "rtsp://127.0.0.1:1/x",
                    "record_mode": "off"}
            client.post("/api/cameras", json={**base, "cam_id": "camS1", "name": "S1"},
                        headers=auth())
            client.post("/api/cameras", json={**base, "cam_id": "camS2", "name": "S2"},
                        headers=auth())
            r = client.post("/api/cameras",
                            json={**base, "cam_id": "camS1", "name": "S1", "storage_smb": False},
                            headers=auth())
            self.assertEqual(r.status_code, 200)
            self.assertEqual(m.CONFIG["backup"]["cameras"], ["camS2"])
            cams = client.get("/api/cameras", headers=auth()).json()
            self.assertFalse(cams["camS1"]["storage_smb"])
            self.assertTrue(cams["camS2"]["storage_smb"])
            client.post("/api/cameras",
                        json={**base, "cam_id": "camS2", "name": "S2", "storage_smb": False},
                        headers=auth())
            self.assertEqual(m.CONFIG["backup"]["cameras"], [])
            self.assertTrue(m.CONFIG["backup"]["cameras_explicit"])
            cams = client.get("/api/cameras", headers=auth()).json()
            self.assertFalse(cams["camS1"]["storage_smb"])
            self.assertFalse(cams["camS2"]["storage_smb"])
        finally:
            client.__exit__(None, None, None)
            m.CONFIG["cameras"] = saved_cams or {}
            m.CONFIG["backup"] = saved_backup or {}

    def test_backup_flag_off_does_not_seed_unconfigured_destinations(self):
        m = self.m
        saved_cams = m.CONFIG.get("cameras")
        saved_backup = m.CONFIG.get("backup")
        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        client.__enter__()
        try:
            m.CONFIG["cameras"] = {}
            base = {"brand": "custom", "rtsp_url": "rtsp://127.0.0.1:1/x",
                    "record_mode": "off"}
            client.post("/api/cameras", json={**base, "cam_id": "camOff1", "name": "Off 1"},
                        headers=auth())
            client.post("/api/cameras", json={**base, "cam_id": "camOff2", "name": "Off 2"},
                        headers=auth())
            m.CONFIG["backup"] = {"enabled": False, "kind": "smb", "smb_host": "",
                                  "smb_share": "", "cameras": [], "cameras_explicit": False}
            r = client.post("/api/cameras",
                            json={**base, "cam_id": "camOff1", "name": "Off 1",
                                  "storage_smb": False},
                            headers=auth())
            self.assertEqual(r.status_code, 200)
            self.assertEqual(m.CONFIG["backup"]["cameras"], [])
            self.assertTrue(m.CONFIG["backup"]["cameras_explicit"])
        finally:
            client.__exit__(None, None, None)
            m.CONFIG["cameras"] = saved_cams or {}
            m.CONFIG["backup"] = saved_backup or {}

    def test_storage_selection_defaults_off_when_unconfigured(self):
        m = self.m
        cfg = m._normalize_config({
            "cameras": {"c1": {}, "c2": {}},
            "backup": {"enabled": False, "kind": "smb", "cameras": []},
        })
        self.assertEqual(cfg["backup"]["cameras"], [])
        self.assertTrue(cfg["backup"]["cameras_explicit"])

        active = m._normalize_config({
            "cameras": {"c1": {}, "c2": {}},
            "backup": {"enabled": True, "kind": "smb", "smb_host": "nas",
                       "smb_share": "media", "cameras": []},
        })
        self.assertEqual(active["backup"]["cameras"], ["c1", "c2"])

        folder = m._normalize_config({
            "cameras": {"c1": {}, "c2": {}},
            "backup": {"enabled": True, "kind": "folder", "folder_path": "/backup",
                       "cameras": []},
        })
        self.assertEqual(folder["backup"]["cameras"], ["c1", "c2"])

        explicit = m._normalize_config({
            "cameras": {"c1": {}},
            "backup": {"enabled": True, "kind": "smb", "smb_host": "nas",
                       "smb_share": "media", "cameras": [], "cameras_explicit": True},
        })
        self.assertEqual(explicit["backup"]["cameras"], [])

    def test_storage_readiness_requires_enabled_service(self):
        m = self.m
        saved_backup = m.CONFIG.get("backup")
        saved_gdrive = m.CONFIG.get("gdrive")
        try:
            m.CONFIG["backup"] = {"enabled": False, "kind": "smb", "smb_host": "nas",
                                  "smb_share": "media", "cameras": [], "cameras_explicit": True}
            self.assertFalse(m._smb_storage_ready())
            m.CONFIG["backup"]["enabled"] = True
            self.assertTrue(m._smb_storage_ready())

            m.CONFIG["gdrive"] = {"enabled": False, "client_id": "id", "client_secret": "secret",
                                  "refresh_token": "refresh"}
            self.assertFalse(m._drive_storage_ready())
            m.CONFIG["gdrive"]["enabled"] = True
            self.assertTrue(m._drive_storage_ready())
        finally:
            m.CONFIG["backup"] = saved_backup or {}
            m.CONFIG["gdrive"] = saved_gdrive or {}

    def test_backup_scan_skips_all_when_selection_is_explicitly_empty(self):
        m = self.m
        cam = "camExplicit"
        rec = os.path.join(m.RECORDINGS_DIR, cam)
        os.makedirs(rec, exist_ok=True)
        fname = "2026-01-01_00-00-00.mp4"
        fpath = os.path.join(rec, fname)
        with open(fpath, "wb") as f:
            f.write(b"video")
        old = time.time() - 3600
        os.utime(fpath, (old, old))
        saved_cams = m.CONFIG.get("cameras")
        saved_backup = m.CONFIG.get("backup")
        try:
            m.CONFIG.setdefault("cameras", {})[cam] = {"name": cam}
            m.CONFIG["backup"] = {"enabled": True, "kind": "folder", "cameras": [],
                                  "cameras_explicit": True}
            self.assertNotIn(f"{cam}/{fname}", m.backup_scan())
            m.CONFIG["backup"]["cameras_explicit"] = False
            self.assertIn(f"{cam}/{fname}", m.backup_scan())
        finally:
            m.CONFIG["cameras"] = saved_cams or {}
            m.CONFIG["backup"] = saved_backup or {}

    def test_backup_camera_flag_removes_local_after_copy(self):
        m = self.m
        dest = os.path.join(m.STORAGE_DIR, "backupdest")
        os.makedirs(dest, exist_ok=True)
        rec = os.path.join(m.RECORDINGS_DIR, "camMove")
        os.makedirs(rec, exist_ok=True)
        fname = "2026-03-04_05-06-07.mp4"
        fpath = os.path.join(rec, fname)
        with open(fpath, "wb") as f:
            f.write(b"video")
        saved_cams = m.CONFIG.get("cameras")
        saved_backup = m.CONFIG.get("backup")
        try:
            m.CONFIG.setdefault("cameras", {})["camMove"] = {"storage_smb_move": True}
            m.CONFIG["backup"] = {"enabled": True, "kind": "folder", "folder_path": dest,
                                  "mode": "copy", "cameras": ["camMove"]}
            m._backup_file(f"camMove/{fname}")
        finally:
            m.CONFIG["cameras"] = saved_cams or {}
            m.CONFIG["backup"] = saved_backup or {}
        self.assertTrue(os.path.isfile(os.path.join(dest, "2026-03-04", "camMove", fname)))
        self.assertFalse(os.path.exists(fpath))

    def test_backup_scan_and_folder_copy(self):
        m = self.m
        dest = os.path.join(m.STORAGE_DIR, "backupdest")
        os.makedirs(dest, exist_ok=True)
        rec = os.path.join(m.RECORDINGS_DIR, "camB")
        os.makedirs(rec, exist_ok=True)
        fname = "2026-01-01_00-00-00.mp4"
        fpath = os.path.join(rec, fname)
        with open(fpath, "wb") as f:
            f.write(b"video")
        old = time.time() - 3600
        os.utime(fpath, (old, old))
        m.CONFIG["backup"] = {"enabled": True, "kind": "folder", "folder_path": dest,
                              "mode": "copy", "cameras": ["camB"]}
        files = m.backup_scan()
        self.assertIn(f"camB/{fname}", files)
        m._backup_file(f"camB/{fname}")
        self.assertTrue(os.path.isfile(os.path.join(dest, "2026-01-01", "camB", fname)))
        self.assertIn(f"camB/{fname}", m._backup_done())
        self.assertTrue(os.path.isfile(fpath))
        m.CONFIG["backup"]["enabled"] = False

    def test_backup_move_removes_local_for_ftp(self):
        m = self.m
        rec = os.path.join(m.RECORDINGS_DIR, "camD")
        os.makedirs(rec, exist_ok=True)
        fname = "2026-02-03_04-05-06.mp4"
        fpath = os.path.join(rec, fname)
        with open(fpath, "wb") as f:
            f.write(b"video")
        m.CONFIG["backup"] = {"enabled": True, "kind": "ftp", "ftp_host": "example.invalid",
                              "mode": "move", "cameras": ["camD"]}

        class FakeFtp:
            def mkd(self, path):
                pass

            def storbinary(self, cmd, f):
                pass

        try:
            m._backup_file(f"camD/{fname}", FakeFtp())
        finally:
            m.CONFIG["backup"]["enabled"] = False
        self.assertFalse(os.path.exists(fpath))
        self.assertIn(f"camD/{fname}", m._backup_done())

    def test_backup_scan_skips_locked_and_recent(self):
        m = self.m
        rec = os.path.join(m.RECORDINGS_DIR, "camC")
        os.makedirs(rec, exist_ok=True)
        recent = os.path.join(rec, "2026-01-01_01-00-00.mp4")
        locked = os.path.join(rec, "2026-01-01_02-00-00.mp4")
        for p in (recent, locked):
            with open(p, "wb") as f:
                f.write(b"v")
        old = time.time() - 3600
        os.utime(locked, (old, old))
        m.CONFIG["backup"] = {"enabled": True, "kind": "folder",
                              "folder_path": os.path.join(m.STORAGE_DIR, "backupdest"),
                              "cameras": ["camC"]}
        fonts = m.locked_paths
        m.locked_paths = lambda: {"camC/2026-01-01_02-00-00.mp4"}
        try:
            files = m.backup_scan()
        finally:
            m.locked_paths = fonts
        self.assertNotIn("camC/2026-01-01_01-00-00.mp4", files)
        self.assertNotIn("camC/2026-01-01_02-00-00.mp4", files)
        m.CONFIG["backup"]["enabled"] = False

    def test_event_link_builds_playback_url(self):
        m = self.m
        m.CONFIG["notify"] = {"public_url": "http://192.168.5.63:9895/"}
        link = m._event_link("cam x", datetime(2026, 10, 18, 7, 5, 9))
        self.assertIn("http://192.168.5.63:9895/?cam=cam%20x", link)
        self.assertIn("date=2026-10-18", link)
        self.assertIn("t=25509", link)
        m.CONFIG["notify"] = {"public_url": ""}
        self.assertEqual(m._event_link("c", datetime(2026, 1, 1)), "")

    def test_explicit_h264_recording_uses_h264_while_live_copy(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="copy"), video_codec="h264")
        self.assertIn("libx264", cmd)
        self.assertIn("copy", cmd)

    def test_explicit_h264_only_transcodes_live_not_recording(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="h264"), include_live=False)
        self.assertNotIn("libx264", cmd)
        self.assertIn("copy", cmd)

        live = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="h264"), include_live=True)
        self.assertIn("libx264", live)


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()

    def test_terminate_process_reaps(self):
        proc = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(60)"])
        self.m._terminate_process(proc, timeout=2)
        self.assertIsNotNone(proc.poll())

    def test_immediate_restart_skips_backoff(self):
        m = self.m
        cam = "cam-restart-flag"
        class FakeProc:
            returncode = None
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout=None):
                return None
        proc = FakeProc()
        st = m.cam_status.setdefault(cam, {})
        m.ffmpeg_processes[cam] = proc
        try:
            m._restart_camera_process(cam, proc, immediate=True)
            self.assertTrue(st.get("_restart_requested"))
            self.assertTrue(st.get("_restart_immediate"))
        finally:
            m.ffmpeg_processes.pop(cam, None)
            m.cam_status.pop(cam, None)

    def test_worker_ignores_progress_lines_and_audio_warning(self):
        m = self.m
        cam = "cam-log-classify"
        m.CONFIG["cameras"][cam] = {
            "name": "Log classify", "rtsp_url": "rtsp://127.0.0.1:1/x",
            "record_mode": "continuous", "video_codec": "copy", "audio_mode": "aac",
        }
        m.cam_status[cam] = {}
        m.codec_cache[cam] = (("stale", "tcp", False), {"codec_name": "h264"}, time.time() - 1)

        class FakeProc:
            returncode = 1
            def __init__(self, *args, **kwargs):
                self.stderr = io.StringIO(
                    "stream_0_0_q=-1.0\n"
                    "[hevc @ 0x55a114f70000] PPS id out of range: 0\n"
                    "[rtsp @ 0x55a114f72400] Error parsing AU headers\n"
                    "Last message repeated 77 times\n"
                    "stream_1_0_q=-1.0\n"
                )
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode

        try:
            with mock.patch.object(m.subprocess, "Popen", FakeProc):
                m.start_camera_worker(cam)
                token = m.worker_tokens.get(cam)
                deadline = time.time() + 4
                while time.time() < deadline and m.cam_status[cam].get("last_exit_code") is None:
                    time.sleep(0.02)
                if token:
                    token.set()
                while time.time() < deadline and m.worker_tokens.get(cam) is not None:
                    time.sleep(0.02)
            st = m.cam_status[cam]
            self.assertIn("Error parsing AU headers", "\n".join(st["log"]))
            self.assertIn("PPS id out of range", "\n".join(st["log"]))
            self.assertNotIn("stream_", "\n".join(st["log"]))
            self.assertIn("AU headers", st.get("last_warning", ""))
            self.assertNotIn("stream_", st.get("last_error", ""))
            self.assertNotIn("AU headers", st.get("last_error", ""))
            self.assertNotIn("PPS", st.get("last_error", ""))
            self.assertNotIn(cam, m.codec_cache)
            self.assertEqual(m.CODEC_CACHE_TTL_SEC, 300)
            self.assertTrue(m._is_hevc_source_warning("[hevc @ 0x1] PPS id out of range: 0"))
            self.assertFalse(m._is_hevc_source_warning("[h264 @ 0x1] PPS id out of range: 0"))
        finally:
            m.CONFIG["cameras"].pop(cam, None)
            m.cam_status.pop(cam, None)
            m.cam_health.pop(cam, None)

    def test_snapshot_slots_bounded(self):
        m = self.m
        m.cam_source_urls = lambda cam_id, cam_info: (cam_info["rtsp_url"], cam_info["rtsp_url"])
        lock = threading.Lock()
        state = {"active": 0, "max": 0}

        class FakeProc:
            def __init__(self, *a, **k):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                self.returncode = None
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                time.sleep(0.05)
                with lock:
                    if self.returncode is None:
                        state["active"] -= 1
                self.returncode = 0
                return 0
            def terminate(self):
                self.returncode = 0
            def kill(self):
                self.returncode = 0

        real = subprocess.Popen
        subprocess.Popen = FakeProc
        try:
            started = [m._start_snapshot(f"cam{i}", {"rtsp_url": "rtsp://x"}, os.path.join(self.sandbox, f"s{i}.jpg"))
                       for i in range(5)]
            self.assertGreaterEqual(started.count(False), 2)
            deadline = time.time() + 3
            while time.time() < deadline and (m._snapshot_active or m._snapshot_processes):
                time.sleep(0.05)
            self.assertEqual(m._snapshot_active, {})
            self.assertLessEqual(state["max"], 2)
            for _ in range(2):
                m._snapshot_slots.acquire(blocking=False)
            for _ in range(2):
                m._snapshot_slots.release()
        finally:
            subprocess.Popen = real


class _FakeDriveHandler(BaseHTTPRequestHandler):
    """Google Drive giả: /token + /files/<id>?alt=media, hỗ trợ Range và tự ngắt giữa chừng 1 lần."""
    DATA = bytes((i * 7) % 251 for i in range(30000))
    drop_after = 10000
    hits = []
    dropped = False

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path.endswith("/token"):
            body = b'{"access_token":"tok1","expires_in":3600}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_GET(self):
        if "/files/" not in self.path:
            self.send_error(404)
            return
        start = 0
        m = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
        if m:
            start = int(m.group(1))
        type(self).hits.append(start)
        data = self.DATA[start:]
        body_len = len(data)
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "video/mp4")
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(self.DATA) - 1}/{len(self.DATA)}")
        self.send_header("Content-Length", str(body_len))
        self.end_headers()
        if not start and not type(self).dropped:
            type(self).dropped = True
            self.wfile.write(data[:self.drop_after])
            self.wfile.flush()
            self.close_connection = True  # ngắt kết nối giữa chừng
            return
        self.wfile.write(data)
        self.wfile.flush()


class DrivePlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env_backup = {k: os.environ.get(k) for k in ("NVR_DRIVE_API_BASE", "NVR_DRIVE_OAUTH_BASE")}
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDriveHandler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.sandbox, cls.m = load_app({"NVR_DRIVE_API_BASE": base + "/drive/v3",
                                       "NVR_DRIVE_OAUTH_BASE": base})
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.m.app)
        cls.client.__enter__()
        cls.m.gd_cfg().update({"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"})

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.srv.shutdown()
        cls.srv.server_close()
        for k, v in cls._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        _FakeDriveHandler.hits = []
        _FakeDriveHandler.dropped = False
        name = f"2026-10-19_{self._testMethodName[:12]}.mp4"
        self.cam, self.name = "cam9", name
        self.rel = f"{self.cam}/{self.name}"
        self.path = os.path.join(self.m.RECORDINGS_DIR, self.cam, self.name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for p in (self.path, self.path + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass
        conn = self.m.db_conn()
        conn.execute("DELETE FROM drive_uploads WHERE path=?", (self.rel,))
        conn.execute("INSERT INTO drive_uploads(path, cam_id, name, status, drive_id) VALUES(?,?,?,?,?)",
                     (self.rel, self.cam, self.name, "done", "fid1"))
        conn.commit()
        self.m._drive_fetch_prog.pop(self.rel, None)

    def test_drive_download_resumes_after_connection_drop(self):
        prog = {"state": "downloading", "done": 0, "total": 0}
        n = self.m.drive_download_file("fid1", self.path, 0, prog)
        self.assertEqual(n, len(_FakeDriveHandler.DATA))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), _FakeDriveHandler.DATA)
        self.assertEqual(_FakeDriveHandler.hits, [0, 10000])
        self.assertEqual(prog["done"], len(_FakeDriveHandler.DATA))
        self.assertFalse(os.path.exists(self.path + ".part"))

    def test_play_status_starts_fetch_without_video_request(self):
        r = self.client.get(f"/api/drive/play/{self.cam}/{self.name}", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.json()["state"], ("downloading", "done", "local"))
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.isfile(self.path):
            time.sleep(0.02)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.client.get(f"/api/drive/play/{self.cam}/{self.name}",
                                         headers=auth()).json()["state"], "local")

    def test_play_status_error_visible_until_retry(self):
        self.m._drive_fetch_prog[self.rel] = {"state": "error", "error": "mạng lỗi",
                                              "done": 3, "total": 10}
        j = self.client.get(f"/api/drive/play/{self.cam}/{self.name}", headers=auth()).json()
        self.assertEqual(j["state"], "error")
        self.assertIn("mạng lỗi", j["error"])
        j = self.client.get(f"/api/drive/play/{self.cam}/{self.name}?retry=1", headers=auth()).json()
        self.assertIn(j["state"], ("downloading", "done", "local"))
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.isfile(self.path):
            time.sleep(0.02)
        self.assertTrue(os.path.isfile(self.path))

    def test_drive_cleanup_under_quota_does_not_load_done_rows(self):
        queries = []

        class _Cursor:
            def fetchone(self):
                return (0,)

        class _Conn:
            def execute(self, sql, params=()):
                queries.append(sql)
                return _Cursor()

        with mock.patch.object(self.m, "db_conn", return_value=_Conn()), \
                mock.patch.object(self.m, "drive_connected", return_value=True), \
                mock.patch.object(self.m, "gd_cfg", return_value={}), \
                mock.patch.object(self.m, "drive_quota", return_value={"limit": 0, "free": 0}):
            result = self.m.drive_cleanup(force=True)
        self.assertEqual(result["reason"], "ok")
        self.assertFalse(any("SELECT path, name" in q for q in queries))


class PlaybackAutoTests(_AppCase):
    def _camera_file(self, cam, fname, data=b"fake-mp4"):
        self.client.post("/api/cameras", json={
            "cam_id": cam, "name": cam, "rtsp_url": "rtsp://127.0.0.1:1/x",
            "record_mode": "off", "video_codec": "copy"}, headers=auth())
        rec = os.path.join(self.m.RECORDINGS_DIR, cam)
        os.makedirs(rec, exist_ok=True)
        path = os.path.join(rec, fname)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def tearDown(self):
        for cam in ("autoH264", "autoHEVC", "auto10bit", "autoSMB"):
            self.client.delete(f"/api/cameras/{cam}", headers=auth())
        shutil.rmtree(self.m.PLAYBACK_CACHE_DIR, ignore_errors=True)

    def test_h264_browser_safety_rejects_unsupported_profiles(self):
        safe = self.m._is_browser_safe_h264
        self.assertTrue(safe("h264", "yuv420p", "Main"))
        self.assertTrue(safe("h264", "yuvj420p", "High"))
        self.assertFalse(safe("h264", "yuv420p10le", "High 10"))
        self.assertFalse(safe("h264", "yuv422p", "High 4:2:2"))
        self.assertFalse(safe("h264", "yuv444p", "High 4:4:4 Predictive"))
        self.assertFalse(safe("hevc", "yuv420p", "Main"))

    def test_recording_decision_uses_video_and_audio_codec(self):
        with mock.patch.object(self.m, "_probe_recording", return_value={
                "video_codec": "h264", "video_profile": "High",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}):
            self.assertFalse(self.m._recording_needs_transcode("/x.mp4"))
        with mock.patch.object(self.m, "_probe_recording", return_value={
                "video_codec": "h264", "video_profile": "High",
                "video_pix_fmt": "yuv420p", "audio_codec": "opus"}):
            self.assertTrue(self.m._recording_needs_transcode("/x.mp4"))
        with mock.patch.object(self.m, "_probe_recording", return_value={}):
            self.assertTrue(self.m._recording_needs_transcode("/x.mp4"))

    def test_auto_play_direct_for_browser_safe_h264(self):
        cam = "autoH264"
        fname = "2026-07-01_08-00-00.mp4"
        self._camera_file(cam, fname)
        info = {"video_codec": "h264", "video_profile": "High",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}
        with mock.patch.object(self.m, "_probe_recording", return_value=info):
            r = self.client.get(f"/api/play/{cam}/{fname}?t=2&ss=12",
                                headers=auth(), follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b"fake-mp4")

    def test_playback_info_reports_transcode_without_streaming(self):
        cam = "autoH264"
        fname = "2026-07-01_10-00-00.mp4"
        self._camera_file(cam, fname)
        safe = {"video_codec": "h264", "video_profile": "High",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}
        with mock.patch.object(self.m, "_probe_recording", return_value=safe):
            r = self.client.get(f"/api/play/{cam}/{fname}?t=2&info=1",
                                headers=auth(), follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["transcode"])
        with mock.patch.object(self.m, "_probe_recording", return_value={
                "video_codec": "hevc", "video_profile": "Main",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}):
            r = self.client.get(f"/api/play/{cam}/{fname}?t=2&info=1",
                                headers=auth(), follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["transcode"])

    def test_ios_hls_playback_info_returns_playlist(self):
        cam = "iosHLS"
        fname = "2026-07-01_11-00-00.mp4"
        self._camera_file(cam, fname)
        with mock.patch.object(self.m, "_probe_recording", return_value={
                "video_codec": "hevc", "video_profile": "Main",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}), \
             mock.patch.object(self.m, "_start_playback_hls",
                               return_value="/hls/_play/iosHLS/tok/index.m3u8") as start:
            r = self.client.get(f"/api/play/{cam}/{fname}?t=2&ss=7&info=1&hls=1",
                                headers=auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["hls"], "/hls/_play/iosHLS/tok/index.m3u8")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[2], 7)

    def test_playback_hls_command_uses_mpegts_and_copy_when_safe(self):
        out = "/tmp/hls-play"
        with mock.patch.object(self.m, "_recording_needs_transcode", return_value=False):
            cmd = self.m._play_hls_cmd("/rec/x.mp4", 12.5, out)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-hls_segment_type") + 1], "mpegts")
        self.assertEqual(cmd[cmd.index("-hls_playlist_type") + 1], "event")
        self.assertTrue(cmd[-1].endswith("index.m3u8"))
        with mock.patch.object(self.m, "_recording_needs_transcode", return_value=True):
            cmd = self.m._play_hls_cmd("/rec/x.mp4", 0, out)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)

    def test_auto_play_redirects_hevc_and_10bit_to_transcode(self):
        cam = "autoHEVC"
        fname = "2026-07-01_09-00-00.mp4"
        self._camera_file(cam, fname)
        for info in (
            {"video_codec": "hevc", "video_profile": "Main",
             "video_pix_fmt": "yuv420p", "audio_codec": "aac"},
            {"video_codec": "h264", "video_profile": "High 10",
             "video_pix_fmt": "yuv420p10le", "audio_codec": "aac"},
        ):
            with mock.patch.object(self.m, "_probe_recording", return_value=info):
                r = self.client.get(f"/api/play/{cam}/{fname}?t=2&ss=12",
                                    headers=auth(), follow_redirects=False)
            self.assertEqual(r.status_code, 307, r.text)
            self.assertIn(f"/api/play/{cam}/{fname}?t=1&ss=12.000", r.headers["location"])

    def test_transcode_command_is_low_latency_and_seekable(self):
        cmd = self.m._play_transcode_cmd("/rec/x.mp4", 12.5)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertIn("zerolatency", cmd)
        self.assertIn("ultrafast", cmd)
        self.assertIn("0:a:0?", cmd)
        self.assertIn("frag_keyframe", " ".join(cmd))

    def test_smb_auto_play_caches_file_and_streams_h264_once(self):
        cam = "autoSMB"
        day = "2026-07-02"
        fname = f"{day}_08-00-00.mp4"
        self.client.post("/api/cameras", json={
            "cam_id": cam, "name": cam, "rtsp_url": "rtsp://127.0.0.1:1/x",
            "record_mode": "off"}, headers=auth())
        target = mock.Mock()
        target.open_recording.return_value = io.BytesIO(b"smb-video")
        info = {"video_codec": "h264", "video_profile": "Main",
                "video_pix_fmt": "yuv420p", "audio_codec": "aac"}
        with mock.patch.object(self.m, "_smb_target_for_playback", return_value=target), \
             mock.patch.object(self.m, "_probe_recording", return_value=info):
            info_res = self.client.get(f"/api/smb/play/{cam}/{day}/{fname}?t=2&info=1",
                                       headers=auth(), follow_redirects=False)
            r1 = self.client.get(f"/api/smb/play/{cam}/{day}/{fname}?t=2",
                                 headers=auth(), follow_redirects=False)
            r2 = self.client.get(f"/api/smb/play/{cam}/{day}/{fname}?t=2",
                                 headers=auth(), follow_redirects=False)
        self.assertEqual(info_res.status_code, 200, info_res.text)
        self.assertFalse(info_res.json()["transcode"])
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.content, b"smb-video")
        self.assertEqual(r2.content, b"smb-video")
        self.assertEqual(target.open_recording.call_count, 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.m.PLAYBACK_CACHE_DIR, "smb", cam, fname)))

    def test_timeline_uses_auto_playback_urls(self):
        cam = "autoH264"
        day = "2026-07-03"
        fname = f"{day}_08-00-00.mp4"
        self._camera_file(cam, fname)
        r = self.client.get(f"/api/timeline/{cam}?date={day}&source=local", headers=auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("t=2", r.json()["segments"][0]["file"])
        with mock.patch.object(self.m, "_smb_list_recordings", return_value=[fname]):
            r = self.client.get(f"/api/timeline/{cam}?date={day}&source=smb", headers=auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("api/smb/play/", r.json()["segments"][0]["file"])
        self.assertIn("t=2", r.json()["segments"][0]["file"])

    def test_prune_playback_cache_removes_expired_and_over_limit(self):
        cache = self.m.PLAYBACK_CACHE_DIR
        os.makedirs(cache, exist_ok=True)
        old = os.path.join(cache, "old.bin")
        new = os.path.join(cache, "new.bin")
        for p in (old, new):
            with open(p, "wb") as f:
                f.write(b"x")
        past = time.time() - self.m.PLAYBACK_CACHE_TTL_SEC - 60
        os.utime(old, (past, past))
        self.m.prune_playback_cache(force=True)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        with mock.patch.object(self.m, "PLAYBACK_CACHE_MB", 0):
            self.m.prune_playback_cache(force=True)
        self.assertFalse(os.path.exists(new))



if __name__ == "__main__":
    unittest.main(verbosity=2)
