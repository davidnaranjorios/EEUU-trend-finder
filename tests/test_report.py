"""Fase 5: sitio del reporte. Criterios: ningún candidato con una sola fuente y ninguna serie con
copyright en el reporte publicado."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from src.rank.run import rank_run
from src.report.build import build_site, verify_site
from src.report.charts import line_chart
from src.report.guard import CopyrightLeak, assert_publishable, forbidden_sets, scan
from tests.fakes_rank import build


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fase5")
    fx = build(tmp)
    rank_run(fx.cfg, "rank1")
    out = build_site(fx.cfg)
    return fx, Path(out["site_dir"]), out


def _all_text(site_dir: Path) -> str:
    return "\n".join(
        p.read_text("utf-8", errors="replace") for p in sorted(site_dir.rglob("*")) if p.is_file()
    )


def test_no_copyrighted_series_in_published_site(site):
    fx, site_dir, out = site
    text = _all_text(site_dir)
    copyrighted = {k: v for k, v in fx.ids.items() if k == "cr" or k.startswith("crfill")}
    assert copyrighted
    for name, (_, sid) in copyrighted.items():
        assert not re.search(rf"\b{re.escape(sid)}\b", text), f"id {sid} publicado"
        assert fx.titles[name] not in text, f"título de {name} publicado"
    assert out["copyrighted_forbidden"] >= len(copyrighted)


def test_published_candidates_have_two_public_sources(site):
    fx, site_dir, out = site
    index = (site_dir / "index.html").read_text("utf-8")
    report = json.loads(next((site_dir / "archivo").glob("*/report.json")).read_text("utf-8"))
    cands = report["candidates"]["series"] + report["candidates"]["text"]
    assert {c["kind"] for c in cands} == {"series", "text"}
    for c in cands:
        assert c["n_sources"] >= 2
        assert c["candidate_id"] in index
    linked = report["candidates"]["series"]
    assert [s["id"] for c in linked for s in c["series"]] == [fx.ids["linked"][1]]
    assert linked[0]["terms"][0]["source"] == "edgar"
    # La serie duplicada (una sola fuente) y la que no tiene agencia no se publican.
    for name in ("dup_fred", "dup_bls", "noagency"):
        assert f">{fx.titles[name]}<" not in index
    assert out["n_published"] == 2


def test_unpublished_candidates_go_to_private_file_only(site):
    fx, site_dir, _ = site
    reports = Path(fx.cfg["report"]["reports_dir"])
    private = json.loads(next(reports.glob("*/private_candidates.json")).read_text("utf-8"))
    assert len(private["series"]) == 1  # el del par serie con copyright + Federal Register
    assert private["series"][0]["hidden_series"] == 1
    assert not list(site_dir.rglob("private_candidates.json"))


def test_dashboard_and_explorer(site):
    fx, site_dir, out = site
    dash = (site_dir / "dashboard.html").read_text("utf-8")
    for heading in ("Estado del sistema", "Embudo de elegibilidad", "Cobertura", "Explorador"):
        assert heading in dash
    idx = json.loads((site_dir / "data" / "explorer" / "index.json").read_text("utf-8"))
    assert idx["n_cards"] == 404  # 400 de relleno + 4 con señal sin copyright
    assert idx["copyrighted_withheld"] == 0  # ya filtradas en la consulta (NOT copyrighted)
    assert set(idx["by_source"]) == {"fred", "bls", "bea", "census"}
    cards = [
        row
        for b in idx["buckets"]
        for row in json.loads(
            (site_dir / "data" / "explorer" / f"{b:02d}.json").read_text("utf-8")
        )["rows"]
    ]
    lnk = next(c for c in cards if c[1] == fx.ids["linked"][1])
    assert lnk[7] >= 1 and lnk[8]  # términos enlazados y candidato publicado
    assert (site_dir / "archivo.html").exists()


def test_guard_catches_leaks(site, tmp_path):
    fx, site_dir, _ = site
    copy = tmp_path / "site"
    shutil.copytree(site_dir, copy)
    cr_id, cr_title = fx.ids["cr"][1], fx.titles["cr"]
    ids, titles = forbidden_sets([(cr_id, cr_title)], [])
    assert_publishable(copy, ids, titles)
    (copy / "leak.html").write_text(f"<p>Serie {cr_id}</p>", "utf-8")
    with pytest.raises(CopyrightLeak):
        assert_publishable(copy, ids, titles)
    (copy / "leak.html").unlink()
    (copy / "data" / "x.json").write_text(json.dumps({"t": cr_title.upper()}), "utf-8")
    assert scan(copy, ids, titles)
    (copy / "data" / "x.json").unlink()
    chart = line_chart([], [], label=cr_title)
    (copy / "c.html").write_text(f"<div>{chart}</div><div title='x'>{cr_title} | Index</div>")
    assert scan(copy, ids, titles)


def test_guard_allows_ids_shared_with_public_series():
    ids, titles = forbidden_sets(
        [("X1", "Same title"), ("X2", "Only private")], [("X1", "Same title")]
    )
    assert ids == {"X2"} and titles == {"only private"}


def test_rebuild_is_idempotent(site):
    fx, site_dir, out = site
    before = sorted(p.relative_to(site_dir) for p in site_dir.rglob("*") if p.is_file())
    again = build_site(fx.cfg)
    after = sorted(p.relative_to(site_dir) for p in site_dir.rglob("*") if p.is_file())
    assert before == after and again["n_published"] == out["n_published"]


def test_report_workflow_publishes_only_after_guarded_build():
    import yaml

    from src.config import REPO_ROOT

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "reporte.yml").read_text())
    assert wf["concurrency"]["group"] == "reporte"  # no compite con la ingesta
    steps = [s.get("name", "") for s in wf["jobs"]["reporte"]["steps"]]
    assert steps.index("Reporte") < steps.index("Verificar copyright")
    assert steps.index("Verificar copyright") < steps.index("Publicar en Cloudflare Pages")
    assert steps.index("Verificar copyright") < steps.index("Preparar GitHub Pages")
    sync = next(s for s in wf["jobs"]["reporte"]["steps"] if s.get("name") == "Sincronizar a R2")
    assert "processed/series " not in sync["run"] and "processed/rank" in sync["run"]
    assert wf["jobs"]["pages"]["needs"] == "reporte"


def test_verify_site_rereads_copyright_list(site, tmp_path):
    fx, site_dir, _ = site
    out = verify_site(fx.cfg, site_dir)
    assert out["leaks"] == 0 and out["forbidden_ids"] >= 11
    copy = tmp_path / "site"
    shutil.copytree(site_dir, copy)
    (copy / "x.html").write_text(f"<td>{fx.ids['crfill3'][1]}</td>", "utf-8")
    with pytest.raises(CopyrightLeak):
        verify_site(fx.cfg, copy)
