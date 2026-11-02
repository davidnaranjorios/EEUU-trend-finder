"""ADR 0003: scripts de infraestructura (sintaxis, shellcheck) y unidades systemd."""

import configparser
import shutil
import subprocess

import pytest

from src.config import REPO_ROOT

INFRA = REPO_ROOT / "infra"
SCRIPTS = sorted(INFRA.glob("*.sh"))
UNITS = sorted((INFRA / "systemd").iterdir())


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(INFRA / "systemd" / name)
    return parser


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax_and_strict_mode(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert "set -euo pipefail" in text
    assert script.stat().st_mode & 0o111, "debe ser ejecutable"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck no instalado")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(script):
    subprocess.run(["shellcheck", str(script)], check=True)


def test_initial_download_is_resumable_service():
    u = unit("trend-initial-download.service")
    assert u["Service"]["User"] == "trend"  # nunca root
    assert u["Service"]["Restart"] == "on-failure"
    assert "initial_download.done" in u["Unit"]["ConditionPathExists"]
    assert u["Service"]["ExecStart"].endswith("infra/run_tramos.sh")


def test_weekly_timer_waits_for_initial_download():
    timer = unit("trend-weekly-update.timer")
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Timer"]["Unit"] == "trend-weekly-update.service"
    svc = unit("trend-weekly-update.service")
    assert svc["Service"]["User"] == "trend"
    assert svc["Unit"]["ConditionPathExists"].endswith("initial_download.done")


def test_tramos_sync_after_each_tramo_and_only_target_groups():
    text = (INFRA / "run_tramos.sh").read_text()
    assert "download --max-releases" in text and "vintages --max-series" in text
    # Cada tramo cierra con un sync a R2.
    loop = text[text.index("tramos() {") : text.index("\n}", text.index("tramos() {"))]
    assert "sync_r2" in loop
    assert "observations" not in text  # la descarga inicial va por v2, no serie por serie


def test_sync_refuses_empty_volume_and_keeps_deleted():
    text = (INFRA / "sync_r2.sh").read_text()
    assert "no tiene Parquet" in text and "--backup-dir" in text
    assert "rclone config" not in text  # sin archivo de configuración con secretos


def test_provision_hardening():
    text = (INFRA / "provision.sh").read_text()
    for needle in (
        "PasswordAuthentication no",
        "PermitRootLogin no",
        "ufw default deny incoming",
        "sshd -t",
        "chmod 600",
        "nofail",
        "blkid",  # no formatea un volumen que ya tiene datos
    ):
        assert needle in text, needle


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="sin systemd-analyze")
@pytest.mark.parametrize("path", UNITS, ids=lambda p: p.name)
def test_systemd_units_parse(path):
    out = subprocess.run(
        ["systemd-analyze", "verify", "--man=false", str(path)],
        capture_output=True,
        text=True,
    )
    # Fuera del VPS las rutas /home/trend/... no existen: es el único error aceptable.
    errors = [
        line
        for line in (out.stdout + out.stderr).splitlines()
        if line.strip() and "No such file or directory" not in line
    ]
    assert errors == []


# --- creación de recursos por API (contra servidores falsos locales) ---------------------------


class _FakeApi:
    """Hetzner Cloud y R2 en memoria: lo justo para hcloud_create.sh y r2_create_bucket.sh."""

    def __init__(self):
        self.objs = {"ssh_keys": [], "firewalls": [], "volumes": [], "servers": []}
        self.buckets = set()
        self.posts = []
        self.auth = []

    def handle(self, method, path, body):
        import re
        from urllib.parse import parse_qs, urlparse

        u = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if m := re.fullmatch(r"/client/v4/accounts/acc/r2/buckets(?:/(.+))?", p):
            if method == "GET":
                return (200, {}) if m.group(1) in self.buckets else (404, {})
            self.buckets.add(body["name"])
            return 200, {"success": True}
        p = p.removeprefix("/v1")
        if p == "/server_types":
            return 200, {"server_types": [{"id": 1}] if q.get("name") == "cpx32" else []}
        if m := re.fullmatch(r"/(ssh_keys|firewalls|volumes|servers)", p):
            kind = m.group(1)
            if method == "GET":
                return 200, {kind: [o for o in self.objs[kind] if o["name"] == q.get("name")]}
            self.posts.append(kind)
            obj = {"id": len(self.posts) * 10, **body}
            if kind == "volumes":
                obj["location"] = {"name": body["location"]}
                obj["server"] = None
            if kind == "servers":
                obj.update(status="running", public_net={"ipv4": {"ip": "203.0.113.7"}})
                for v in self.objs["volumes"]:
                    if v["id"] in body["volumes"]:
                        v["server"] = obj["id"]
            self.objs[kind].append(obj)
            return 201, {kind[:-1]: obj}
        if m := re.fullmatch(r"/(servers|volumes)/(\d+)", p):
            kind, oid = m.group(1), int(m.group(2))
            return 200, {kind[:-1]: next(o for o in self.objs[kind] if o["id"] == oid)}
        return 404, {"error": p}


@pytest.fixture
def fake_api():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    api = _FakeApi()

    class H(BaseHTTPRequestHandler):
        def _do(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            api.auth.append(self.headers.get("Authorization"))
            code, out = api.handle(method, self.path, body)
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield api
    srv.shutdown()


def _run(script, env):
    import os

    full = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    return subprocess.run(
        ["bash", str(INFRA / script)], env={**full, **env}, capture_output=True, text=True
    )


def test_hcloud_create_is_idempotent(fake_api):
    env = {
        "HCLOUD_TOKEN": "tok",
        "HCLOUD_API": fake_api.url + "/v1",
        "SSH_PUBKEY": "ssh-ed25519 AAAA test",
        "POLL_S": "0",
    }
    first = _run("hcloud_create.sh", env)
    assert first.returncode == 0, first.stderr
    assert "IP=203.0.113.7" in first.stdout and "VOLUME_ID=30" in first.stdout
    assert fake_api.posts == ["ssh_keys", "firewalls", "volumes", "servers"]
    server = fake_api.objs["servers"][0]
    assert server["server_type"] == "cpx32" and server["volumes"] == [30]
    assert server["firewalls"] == [{"firewall": 20}] and server["automount"] is False
    assert fake_api.objs["firewalls"][0]["rules"][0]["port"] == "22"
    second = _run("hcloud_create.sh", env)
    assert second.returncode == 0, second.stderr
    assert fake_api.posts == ["ssh_keys", "firewalls", "volumes", "servers"]  # nada nuevo
    assert set(fake_api.auth) == {"Bearer tok"}


def test_hcloud_create_rejects_bad_input(fake_api):
    env = {"HCLOUD_TOKEN": "t", "HCLOUD_API": fake_api.url + "/v1", "POLL_S": "0"}
    assert _run("hcloud_create.sh", {**env, "SSH_PUBKEY": "no-es-llave"}).returncode != 0
    bad_type = _run("hcloud_create.sh", {**env, "SSH_PUBKEY": "ssh-ed25519 A", "SERVER_TYPE": "x"})
    assert bad_type.returncode != 0 and "no ofrece" in bad_type.stderr
    assert fake_api.posts == []


def test_r2_bucket_create_is_idempotent(fake_api):
    env = {
        "CLOUDFLARE_API_TOKEN": "cf",
        "CLOUDFLARE_ACCOUNT_ID": "acc",
        "CLOUDFLARE_API": fake_api.url + "/client/v4",
    }
    assert "creado" in _run("r2_create_bucket.sh", env).stdout
    assert "ya existe" in _run("r2_create_bucket.sh", env).stdout
    assert fake_api.buckets == {"eeuu-trend-finder"}
