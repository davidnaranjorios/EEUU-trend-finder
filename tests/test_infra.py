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
