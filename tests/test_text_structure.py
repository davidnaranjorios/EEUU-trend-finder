"""Inventario de estructura del texto (ADR 0026): esquema, idempotencia, reanudación, informe."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest
from lxml import etree

from src.config import load_config
from src.ingest import store
from src.ingest.edgar import EdgarSource, inventory_items
from src.ingest.federal_register import FederalRegisterSource, cfr_refs, structure_of
from src.ingest.text import EXIT_BUDGET, TABLE_DIRS, run_source
from src.ingest.text_structure import build_report
from src.schemas import TEXT_STRUCTURE, SchemaError, validate
from tests.fake_text import FakeEdgar, FakeGovinfo, filler, fr_issue, tenk_html

TODAY = date(2024, 6, 1)


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["text"]["min_doc_count"] = 2
    c["text"]["workers"] = 0
    c["text"]["structure"]["facts_per_period"] = 6
    for s in ("edgar", "federal_register"):
        c[s]["start_year"] = 2024
        c[s]["min_chars"] = 200
    return c


def edgar(cfg, fake, tmp_path) -> EdgarSource:
    return EdgarSource.from_config(
        cfg,
        "Test User test@example.com",
        tmp_path / "raw",
        transport=fake.transport(),
        sleep=lambda _s: None,
    )


def read_structure(processed: Path, source: str) -> list[dict]:
    table = store.read_partitions(
        TEXT_STRUCTURE, processed / TABLE_DIRS["text_structure"], source=source
    )
    validate(table, TEXT_STRUCTURE)
    return sorted(table.to_pylist(), key=lambda r: r["doc_id"])


def test_schema_rejects_invalid_rows():
    row = {f.name: None for f in TEXT_STRUCTURE.schema}
    row.update(source="edgar", doc_id="a", period="2024Q1", date=date(2024, 1, 1))
    validate(pa.Table.from_pylist([row], schema=TEXT_STRUCTURE.schema), TEXT_STRUCTURE)
    bad = dict(row, date=None)  # `date` no admite nulos
    with pytest.raises(SchemaError):
        validate(pa.Table.from_pylist([bad], schema=TEXT_STRUCTURE.schema), TEXT_STRUCTURE)
    with pytest.raises(SchemaError):  # clave (source, doc_id) duplicada
        validate(pa.Table.from_pylist([row, row], schema=TEXT_STRUCTURE.schema), TEXT_STRUCTURE)


def test_inventory_items_ignores_table_of_contents_and_lists_all_items():
    from src.ingest.edgar import html_to_text

    text = html_to_text(tenk_html(filler(20, 1), filler(15, 2)).encode())
    items = inventory_items(text)
    assert {"1", "1A", "1B", "7", "7A", "8"} <= set(items)
    assert items["1A"] > 500 and items["7"] > 500  # no gana el tramo de una línea del índice


def test_edgar_structure_rows_and_idempotence(cfg, tmp_path):
    processed = tmp_path / "processed"

    def go(run_id):
        run_source(
            edgar(cfg, FakeEdgar(), tmp_path),
            cfg,
            processed_dir=processed,
            run_id=run_id,
            today=TODAY,
            only_periods=["2024Q1"],
        )
        return read_structure(processed, "edgar")

    first = go("r1")
    by = {r["doc_id"][-6:]: r for r in first}
    assert set(by) == {"000001", "000002", "000003", "000006"}  # 10-K analizados; sin 10-K/A ni 404
    one = by["000001"]
    assert (one["sic"], one["state_incorporation"], one["state_business"]) == ("2834", "DE", "CA")
    assert (one["fiscal_year_end"], one["filer_category"]) == ("1231", "Large accelerated filer")
    assert one["doc_type"] == "10-K" and one["period"] == "2024Q1"
    assert {"1A", "1B", "7", "7A", "8"} <= set(json.loads(one["items_json"]))
    assert json.loads(one["format_json"]) == {"html": True, "via": "primary"}
    assert by["000002"]["filer_category"] is None  # vacío en la fuente: NULL, no inventado
    probed = {r["doc_id"][-6:]: r["facts_n_concepts"] for r in first}
    assert probed["000001"] == 1 and probed["000002"] == 0  # 404 = sin XBRL; solo cuenta su accn
    assert go("r2") == first  # volver a correr no duplica ni cambia nada
    assert len(list((processed / "text_structure").rglob("*.parquet"))) == 1


def test_fr_structure_from_tags_only():
    xml = (
        "<RULE><PREAMB><AGENCY>Agency One</AGENCY><SUBAGY>Sub Unit</SUBAGY>"
        "<CFR>40 CFR Parts 52 and 81</CFR><SUBJECT>S</SUBJECT></PREAMB>"
        "<SUPLINF><LSTSUB><HD>List of Subjects in 40 CFR Part 52</HD>"
        "<P>Alpha beta, Gamma; Delta.</P></LSTSUB></SUPLINF></RULE>"
    )
    st = structure_of(etree.fromstring(xml), date(2024, 1, 2))
    assert st["doc_type"] == "rule" and st["agency_sub"] == "SUB UNIT"
    assert st["cfr"] == ["40 CFR 52", "40 CFR 81"]
    assert st["topics"] == ["Alpha beta", "Gamma", "Delta"]
    assert st["significant"] is None  # el XML de govinfo no trae la marca
    assert st["format"] == {
        "preamb": ["AGENCY", "CFR", "SUBAGY", "SUBJECT"],
        "lstsub": True,
    }
    assert cfr_refs(["7 CFR 1.2"]) == ["7 CFR 1.2"]
    bare = structure_of(
        etree.fromstring("<NOTICE><PREAMB><AGENCY>A</AGENCY></PREAMB></NOTICE>"), date(2024, 1, 2)
    )
    assert bare["cfr"] == [] and bare["topics"] == [] and bare["agency_sub"] is None


def fr_fake(months=("01", "02", "03", "04", "05", "06")):
    issues = {
        f"FR-2024-{m}-02.xml": fr_issue(
            [
                ("RULE", f"2024-{m}01", "AGENCY A", filler(30, int(m))),
                ("NOTICE", f"2024-{m}02", "AGENCY B", filler(30, int(m) + 1)),
            ]
        )
        for m in months
    }
    return FakeGovinfo(issues)


def fr_source(cfg, fake, tmp_path):
    return FederalRegisterSource.from_config(
        cfg, tmp_path / "raw", transport=fake.transport(), sleep=lambda _s: None
    )


def test_run_budget_resumes_and_never_repeats_a_period(cfg, tmp_path):
    """Varias corridas seguidas completan todo; un periodo ya procesado no se repite."""
    cfg["text"]["period_seconds_estimate"]["federal_register"] = 100
    cfg["text"]["period_margin"] = 1.0
    processed = tmp_path / "processed"
    fake = fr_fake()

    class Tick:  # cada periodo «gasta» 1000 s del presupuesto
        def __init__(self):
            self.t = 0.0

        def info(self, *_a, **_k):
            self.t += 1000.0

    runs, seen = [], []
    for i in range(6):
        tick = Tick()
        stats = run_source(
            fr_source(cfg, fake, tmp_path),
            cfg,
            processed_dir=processed,
            run_id=f"r{i}",
            today=TODAY,
            logger=tick,
            deadline=2500.0,
            clock=lambda tick=tick: tick.t,
        )
        runs.append(stats)
        seen += [p["period"] for p in stats["periods_done_now"]]
        if not stats["budget_exhausted"]:
            break
    assert [len(r["periods_done_now"]) for r in runs] == [3, 3]  # 5 cerrados + 1 abierto: 3 + 3
    assert runs[0]["budget_exhausted"] and runs[0]["pending"] == 3
    assert not runs[1]["budget_exhausted"] and runs[1]["pending"] == 0
    assert sorted(seen) == sorted(set(seen)) and len(seen) == 6  # ninguno se repitió
    assert len(read_structure(processed, "federal_register")) == 12
    # Una corrida más no baja nada: todo está procesado con el esquema nuevo.
    n_before = len(fake.requests)
    again = run_source(
        fr_source(cfg, fake, tmp_path), cfg, processed_dir=processed, run_id="r9", today=TODAY
    )
    assert again["periods_done_now"] == [] and len(fake.requests) == n_before
    assert EXIT_BUDGET == 12


def test_structure_report_has_coverage_sections_and_format_changes(cfg, tmp_path):
    processed = tmp_path / "processed"
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r1",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    run_source(
        fr_source(cfg, fr_fake(("01", "02")), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r2",
        today=TODAY,
        only_periods=["2024-01", "2024-02"],
    )
    md = build_report(processed, cfg)
    for part in (
        "Cobertura por campo y año",
        "Secciones (Items) por año",
        "Cuándo nacen las secciones",
        "Empresas por industria y año",
        "Cambios de formato",
        "companyfacts",
        "| `sic` | 100 |",
    ):
        assert part in md
    assert "Item 1A" in md and "`agency` | 100 |" in md


@pytest.mark.parametrize(
    ("done", "expected"), [([{"period": "2025-01", "docs": 3}], True), ([], False)]
)
def test_progress_file_only_when_a_period_was_processed(monkeypatch, tmp_path, done, expected):
    """Freno del encadenado (ADR 0026): `--progress-file` se crea solo si se avanzó un periodo."""
    import src.ingest.text as text_mod

    class Src:
        name = "federal_register"

        def close(self):
            pass

    stats = {"periods_done_now": done, "incomplete": False, "budget_exhausted": True, "pending": 2}
    monkeypatch.setattr(text_mod, "build_source", lambda *a, **k: Src())
    monkeypatch.setattr(text_mod, "run_source", lambda *a, **k: stats)
    cfg = load_config()
    cfg["logging"]["runs_file"] = str(tmp_path / "runs.jsonl")
    monkeypatch.setattr(text_mod, "load_config", lambda *a, **k: cfg)
    flag = tmp_path / "state" / ".progress"
    rc = text_mod.main(
        ["federal_register", "--processed-dir", str(tmp_path), "--progress-file", str(flag)]
    )
    assert rc == EXIT_BUDGET
    assert flag.exists() is expected


def test_run_texto_stops_chaining_without_progress():
    """Con periodos pendientes y ningún avance, run_texto.sh falla (no encadena sin fin)."""
    text = (Path(__file__).parents[1] / "infra" / "run_texto.sh").read_text()
    assert '--progress-file "$PROGRESS_FILE"' in text
    brake = text.index('! -e "$PROGRESS_FILE"')
    assert 'exit "$EXIT_NO_PROGRESS"' in text[brake : brake + 400]
    assert brake < text.index('echo "run_texto: parcial')
