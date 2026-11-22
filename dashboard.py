"""Media Dashboard server — file tĩnh + API bật/tắt container Docker (LAN only).

- Static: phục vụ /var/www/html (index.html tự probe port từng dịch vụ)
- GET  /api/services                    → trạng thái container trong allowlist
- POST /api/services/{key}/start|stop   → bật/tắt thẳng qua docker.sock
Chỉ stdlib (python:3.12-alpine + mount docker.sock) — không build image
(máy hermes không build được Docker — xattr SSD, xem docs/deploy-notes kino).
"""
from __future__ import annotations

import http.client
import json
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

DOCKER_SOCK = "/var/run/docker.sock"
SELF = "media-dashboard"  # không cho tự tắt — mất điều khiển

# key (URL/API) → tên container. Thứ tự = thứ tự hiển thị.
SERVICES = {
    "kino-web": "kino-web",
    "kino-jf": "kino-jf",
    "kino-qbit": "kino-qbit",
    "navidrome": "navidrome",
    "copyparty": "copyparty",
    # stack cũ — profile-gated, container có thể chưa tạo
    "qbittorrent": "qbittorrent",
    "aria2": "aria2-pro",
    "aria-ng": "aria-ng",
    "sonarr": "sonarr",
    "radarr": "radarr",
    "bazarr": "bazarr",
    "lidarr": "lidarr",
    "prowlarr": "prowlarr",
    "flaresolverr": "flaresolverr",
    "byparr": "byparr",
}


class UnixHTTP(http.client.HTTPConnection):
    """HTTP client nói chuyện qua unix socket (docker.sock)."""

    def __init__(self, timeout: float = 60.0) -> None:
        super().__init__("localhost", timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)


def docker(method: str, path: str, timeout: float = 60.0) -> tuple[int, object]:
    conn = UnixHTTP(timeout)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return resp.status, data
    finally:
        conn.close()


def container_states() -> dict[str, str]:
    code, data = docker("GET", "/containers/json?all=1")
    states: dict[str, str] = {}
    if code == 200 and isinstance(data, list):
        for c in data:
            state = c.get("State", "unknown")
            for name in c.get("Names", []):
                states[name.lstrip("/").lower()] = state
    return states


def norm_state(state: str) -> str:
    if state == "running":
        return "running"
    if state in ("exited", "created"):
        return "stopped"
    return state or "unknown"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory="/var/www/html", **kwargs)

    def log_message(self, fmt, *args) -> None:
        pass  # server yếu — đừng ghi log từng request

    def _json(self, code: int, obj) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/api/services":
            states = container_states()
            services = [
                {"key": key, "container": name,
                 "state": norm_state(states.get(name)) if name in states else "missing"}
                for key, name in SERVICES.items()
            ]
            return self._json(200, {"services": services})
        return super().do_GET()

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "services"]:
            return self._toggle(parts[2], parts[3])
        return self._json(404, {"error": "không tìm thấy endpoint"})

    def _toggle(self, key: str, action: str) -> None:
        if action not in ("start", "stop"):
            return self._json(405, {"error": "chỉ start/stop"})
        container = SERVICES.get(key)
        if container is None:
            return self._json(404, {"error": f"dịch vụ không rõ: {key}"})
        if container == SELF:
            return self._json(403, {"error": "Không thể tắt dashboard — sẽ mất chỗ điều khiển. Tắt qua SSH nếu thật sự cần."})
        states = container_states()
        if container not in states:
            return self._json(404, {"error": f"Container '{container}' chưa được tạo — bật bằng docker compose với profile tương ứng."})
        path = f"/containers/{container}/start" if action == "start" else f"/containers/{container}/stop?t=10"
        code, data = docker("POST", path)
        if code in (204, 304):
            states = container_states()
            return self._json(200, {"ok": True, "key": key,
                                    "state": norm_state(states.get(container, "unknown"))})
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        return self._json(502, {"error": msg or f"Docker trả {code}"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 80), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
