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
    # -x sigue los `source` (p. ej. infra/r2_env.sh), resueltos desde la raíz del repo.
    subprocess.run(["shellcheck", "-x", str(script)], check=True, cwd=REPO_ROOT)


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
    # Fuera del VPS las rutas /home/trend/... no existen: es el único error aceptable. Los
    # avisos sobre unidades del propio sistema (/usr/lib/systemd/..., p. ej. CPUAccounting= en
    # el runner de GitHub) no son de nuestras unidades y se ignoran.
    errors = [
        line
        for line in (out.stdout + out.stderr).splitlines()
        if line.strip()
        and "No such file or directory" not in line
        and not line.startswith(("/usr/lib/systemd/", "/lib/systemd/", "/etc/systemd/"))
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
    assert fake_api.buckets == {"trend-finder"}


# --- GitHub Actions (ADR 0004) --------------------------------------------------------------


def _workflow():
    import yaml

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ingesta.yml").read_text())
    wf["on"] = wf.pop(True, wf.get("on"))  # YAML 1.1 lee la clave `on` como True
    return wf


def test_workflow_schedule_and_no_overlap():
    wf = _workflow()
    assert wf["on"]["schedule"] and "workflow_dispatch" in wf["on"]
    assert wf["concurrency"]["cancel-in-progress"] is False  # nunca cortar una corrida en curso
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["ingesta"]["timeout-minutes"] < 360  # límite de 6 h por job


def test_workflow_secrets_and_order():
    job = _workflow()["jobs"]["ingesta"]
    for name in (
        "FRED_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert job["env"][name] == "${{ secrets." + name + " }}"
    runs = [s.get("run", "") for s in job["steps"]]
    pull = next(i for i, r in enumerate(runs) if "pull_r2.sh" in r)
    work = next(i for i, r in enumerate(runs) if "run_actions.sh" in r)
    assert pull < work  # primero el estado anterior, después la ingesta


def test_state_and_logs_round_trip_through_r2():
    pull = (INFRA / "pull_r2.sh").read_text()
    sync = (INFRA / "sync_r2.sh").read_text()
    assert "rclone copy" in pull and "rclone sync" not in pull  # traer nunca borra
    assert "set -- processed signals logs" in pull and 'for dir in "$@"' in pull
    assert "set -- processed signals exports logs" in sync and 'for dir in "$@"' in sync
    entry = (INFRA / "run_actions.sh").read_text()
    assert "initial_download.done" in entry and "run_tramos.sh" in entry
    assert "weekly_update.sh" in entry


ACC = "0123456789abcdef0123456789abcdef"
KEY = "k" * 32
SECRET = "s" * 64


def _r2_env(**env):
    base = {
        "PATH": "/usr/bin:/bin",
        "R2_BUCKET": "b",
        "CLOUDFLARE_ACCOUNT_ID": ACC,
        "AWS_ACCESS_KEY_ID": KEY,
        "AWS_SECRET_ACCESS_KEY": SECRET,
    }
    return subprocess.run(
        ["bash", "-c", "source infra/r2_env.sh && echo $RCLONE_CONFIG_R2_ENDPOINT"],
        cwd=REPO_ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "account",
    [
        ACC,
        f" {ACC}\n",
        f"https://{ACC}.r2.cloudflarestorage.com",
        f"{ACC}.r2.cloudflarestorage.com/",
        ACC.upper(),
    ],
)
def test_r2_endpoint_tolerates_pasted_account_id(account):
    out = _r2_env(CLOUDFLARE_ACCOUNT_ID=account)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"https://{ACC}.r2.cloudflarestorage.com"


def test_r2_endpoint_jurisdiction():
    out = _r2_env(R2_JURISDICTION="EU")
    assert out.stdout.strip() == f"https://{ACC}.eu.r2.cloudflarestorage.com"


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"CLOUDFLARE_ACCOUNT_ID": "mi-cuenta"}, "quedan 9"),
        ({"AWS_ACCESS_KEY_ID": "x" * 40}, "Token value"),
        ({"AWS_SECRET_ACCESS_KEY": SECRET + "\n "}, None),  # espacios: se limpian, pasa
    ],
)
def test_r2_credentials_shape(env, msg):
    out = _r2_env(**env)
    if msg is None:
        assert out.returncode == 0, out.stderr
    else:
        assert out.returncode != 0 and msg in out.stderr
        assert "x" * 40 not in out.stderr  # nunca se imprime el valor


def test_rclone_pinned_and_sync_verified():
    install = (INFRA / "install_rclone.sh").read_text()
    assert "sha256sum -c" in install and "downloads.rclone.org" in install
    for wf in ("ingesta.yml", "verificar-r2.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / wf).read_text()
        assert "infra/install_rclone.sh" in text and "apt-get install" not in text
    sync = (INFRA / "sync_r2.sh").read_text()
    assert "--no-update-modtime" in sync  # R2 responde 501 a la autocopia de mtime
    assert "rclone check" in sync and "--one-way" in sync


def test_verify_workflow_is_read_only():
    text = (REPO_ROOT / ".github" / "workflows" / "verificar-r2.yml").read_text()
    assert "rclone sync" not in text and "sync_r2.sh" not in text and "FRED_API_KEY" not in text


def test_workflow_summaries_read_local_copy():
    ingesta = (REPO_ROOT / ".github/workflows/ingesta.yml").read_text()
    assert "summary --r2" not in ingesta  # la ingesta nunca depende de httpfs
    verify = (REPO_ROOT / ".github/workflows/verificar-r2.yml").read_text()
    assert "infra/pull_r2.sh" in verify
    # La lectura directa por httpfs (prueba del bloque 0.1) va acotada y no tumba el job.
    step = verify[verify.index("summary --r2") - 600 : verify.index("summary --r2")]
    assert "timeout-minutes:" in step and "continue-on-error: true" in step


def test_sync_compacts_before_upload_and_export_never_blocks_sync():
    sync = (INFRA / "sync_r2.sh").read_text()
    assert sync.index("src.ingest.store compact") < sync.index("rclone sync")
    for name in ("weekly_update.sh", "run_tramos.sh"):
        text = (INFRA / name).read_text()
        assert "py -m src.export >/dev/null || export_rc=$?" in text
        export_at = text.index("src.export")
        assert "sync_r2" in text[export_at:]


def test_actions_not_on_node20():
    # Versiones con runs.using: node24 (verificado en sus action.yml, 2026-11-03).
    # upload-pages-artifact@v5 es compuesta y usa upload-artifact v7 (node24).
    allowed = {
        "actions/checkout@v6",
        "astral-sh/setup-uv@v7",
        "actions/upload-artifact@v6",
        "actions/cache@v6",
        "actions/upload-pages-artifact@v5",
        "actions/deploy-pages@v5",
    }
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text().splitlines():
            if "uses:" in line:
                assert line.split("uses:")[1].strip() in allowed, f"{wf.name}: {line.strip()}"


def test_initial_download_marker_reaches_r2():
    # En Actions la máquina es nueva en cada corrida: el marcador solo existe si se sincronizó.
    text = (INFRA / "run_tramos.sh").read_text()
    marker = text.index('touch "$STATE/initial_download.done"')
    assert "sync_r2" in text[marker:], "el último sync debe ir después del marcador"
    # Y la entrada de Actions decide con ese marcador entre inicial y semanal.
    actions = (INFRA / "run_actions.sh").read_text()
    assert "initial_download.done" in actions and "infra/weekly_update.sh" in actions


def test_install_rclone_no_sigpipe():
    # `rclone version | head -n 1` con pipefail falla al azar con 141 (SIGPIPE).
    text = (INFRA / "install_rclone.sh").read_text()
    assert "| head" not in text


TEXT_PATHS = (
    "processed/text_docs",
    "processed/term_freq",
    "processed/lineage/text_ingest",
    "processed/ingest_state/text",
)


def _text_workflow():
    import yaml

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ingesta-texto.yml").read_text())
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


def test_text_workflow_shares_lock_and_secrets():
    wf = _text_workflow()
    assert wf["concurrency"] == _workflow()["concurrency"]  # nunca dos sync a R2 a la vez
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["texto"]
    assert job["timeout-minutes"] < 360
    for name in ("SEC_USER_AGENT", "CLOUDFLARE_ACCOUNT_ID", "AWS_ACCESS_KEY_ID"):
        assert job["env"][name] == "${{ secrets." + name + " }}"
    assert "FRED_API_KEY" not in job["env"]
    runs = [s.get("run", "") for s in job["steps"]]
    pull = next(i for i, r in enumerate(runs) if "pull_r2.sh" in r)
    work = next(i for i, r in enumerate(runs) if "run_texto.sh" in r)
    assert pull < work
    assert all(p in runs[pull] for p in TEXT_PATHS)  # solo trae las tablas de texto


def test_text_tramos_sync_only_text_paths():
    text = (INFRA / "run_texto.sh").read_text()
    loop = text[text.index("tramos() {") : text.index("\n}", text.index("tramos() {"))]
    assert "sync_r2" in loop and "--max-periods" in loop
    assert all(p in text for p in TEXT_PATHS)
    assert 'infra/sync_r2.sh "${TEXT_PATHS[@]}"' in text  # nunca el processed/ completo
    pull = (INFRA / "pull_r2.sh").read_text()
    assert "rc -eq 3" in pull  # tabla aún inexistente en R2: no falla la primera corrida
