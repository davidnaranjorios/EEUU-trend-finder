"""Fase 3, parte A: ingesta de texto (EDGAR y Federal Register) — esquema, idempotencia,
reanudación, extracción de secciones y frecuencias por periodo."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pytest

from src.config import load_config
from src.ingest import store
from src.ingest.edgar import EdgarSource, extract_sections, html_to_text, parse_form_index
from src.ingest.federal_register import FederalRegisterSource
from src.ingest.http import ApiClient, ApiError
from src.ingest.text import TABLE_DIRS, TextState, build_source, run_source
from src.ingest.text_terms import hash_ngram, iter_ngrams, ngram_hashes, normalize
from src.schemas import TERM_FREQ, TEXT_DOCS, TEXT_INGEST, validate
from tests.fake_text import FakeEdgar, FakeGovinfo, filler, fr_issue, tenk_html

TODAY = date(2024, 6, 1)  # 2024Q1 cerrado (con 7 días de gracia); 2024Q2 abierto


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["text"]["min_doc_count"] = 2
    c["text"]["workers"] = 0
    c["edgar"]["start_year"] = 2024
    c["edgar"]["min_chars"] = 200
    c["federal_register"]["start_year"] = 2024
    c["federal_register"]["min_chars"] = 200
    return c


def edgar(cfg, fake, tmp_path) -> EdgarSource:
    return EdgarSource.from_config(
        cfg,
        "Test User test@example.com",
        tmp_path / "raw",
        transport=fake.transport(),
        sleep=lambda _s: None,
    )


def read_all(processed: Path, source: str) -> dict[str, list[dict]]:
    out = {}
    for spec in (TEXT_DOCS, TERM_FREQ, TEXT_INGEST):
        table = store.read_partitions(spec, processed / TABLE_DIRS[spec.name], source=source)
        validate(table, spec)
        rows = table.to_pylist()
        for r in rows:
            r.pop("run_id", None)
            r.pop("decided_at", None)
        key = spec.key
        out[spec.name] = sorted(rows, key=lambda r: tuple(str(r[k]) for k in key if k in r))
    return out


def parquet_files(processed: Path) -> list[str]:
    """Particiones con Parquet (el nombre del archivo cambia al reescribir; la cuenta no)."""
    return sorted(str(p.parent.relative_to(processed)) for p in processed.rglob("*.parquet"))


# --- Unidades ----------------------------------------------------------------------------------


def test_ngrams_do_not_cross_punctuation_and_hash_is_stable():
    grams = set(iter_ngrams(normalize("Alpha Beta. Gamma delta"), 3))
    assert "alpha beta" in grams and "gamma delta" in grams
    assert "beta gamma" not in grams  # el punto corta el n-grama
    assert hash_ngram("alpha beta") == hash_ngram("alpha beta")
    h = ngram_hashes(normalize("a b a b"), 2)
    assert len(h) == len(np.unique(h))  # un documento cuenta una vez por n-grama


def test_extract_sections_ignores_table_of_contents():
    body_1a, body_7 = filler(20, 1), filler(15, 2)
    text = html_to_text(tenk_html(body_1a, body_7, hidden="oculto").encode())
    secs = extract_sections(text)
    assert set(secs) == {"item_1a", "item_7"}
    assert normalize(body_1a)[:40] in normalize(secs["item_1a"])
    assert "unresolved" not in normalize(secs["item_1a"])  # termina en Item 1B
    assert "market" not in normalize(secs["item_7"])  # termina en Item 7A
    assert "oculto" not in text  # ix:header oculto


def test_parse_form_index_handles_fixed_width():
    fake = FakeEdgar()
    rows = parse_form_index(__import__("gzip").decompress(fake.index).decode("latin-1"))
    assert len(rows) == 7
    assert rows[0].accession == "0000000001-24-000001" and rows[0].form == "10-K"
    assert {r.form for r in rows} == {"10-K", "10-K/A"}


def test_edgar_requires_user_agent(monkeypatch, cfg):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(SystemExit, match="SEC_USER_AGENT"):
        build_source("edgar", cfg, Path("unused"))


def test_api_client_bytes_and_download_retry(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"payload")

    client = ApiClient(
        "https://x.test",
        {},
        requests_per_minute=6000,
        max_retries=2,
        timeout_s=5,
        backoff_base_s=0.01,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert client.get_bytes("/a") == b"payload"
    calls["n"] = 0
    assert client.download("/b", tmp_path / "f.bin") == 7
    assert (tmp_path / "f.bin").read_bytes() == b"payload"
    assert not list(tmp_path.glob(".*.tmp"))
    with pytest.raises(ApiError) as exc:
        ApiClient(
            "https://x.test",
            {},
            requests_per_minute=6000,
            max_retries=0,
            timeout_s=5,
            backoff_base_s=0.01,
            transport=httpx.MockTransport(lambda r: httpx.Response(403)),
        ).get_bytes("/c")
    assert exc.value.status == 403


# --- EDGAR -------------------------------------------------------------------------------------


def test_edgar_period_schema_lineage_and_counts(cfg, tmp_path):
    fake = FakeEdgar(inject="zeta omega kappa", inject_docs=2)
    processed = tmp_path / "processed"
    src = edgar(cfg, fake, tmp_path)
    stats = run_source(
        src, cfg, processed_dir=processed, run_id="r1", today=TODAY, only_periods=["2024Q1"]
    )
    assert stats["periods_done_now"][0]["docs"] == 6
    assert fake.user_agents == {"Test User test@example.com"}
    t = read_all(processed, "edgar")

    status = {
        (r["doc_id"][-6:], r["section"]): (r["status"], r["detail"]) for r in t["text_ingest"]
    }
    assert status[("000005", "*")] == ("excluido", "formulario=10-K/A")
    assert status[("000004", "*")] == ("excluido", "error_descarga=HTTP 404")
    assert status[("000003", "item_1a")][0] == "muy_corta"
    assert status[("000006", "item_1a")] == ("duplicado", "duplicado_de=0000000001-24-000001")
    assert status[("000001", "item_7")][0] == "ok"
    # El co-registrante (mismo accession, otro CIK) no se cuenta dos veces.
    assert sum(1 for r in t["text_docs"] if r["doc_id"].endswith("000001")) == 2
    assert {r["entity"] for r in t["text_docs"] if r["doc_id"].endswith("000001")} == {"101"}

    freq = {(r["section"], r["term"]): r for r in t["term_freq"]}
    inj = freq[("item_1a", "zeta omega kappa")]
    assert (inj["doc_count"], inj["total_docs"]) == (2, 2)  # 1A: docs 1 y 2 (3 corto, 6 dup.)
    assert inj["n_entities"] == 2  # CIK 101 y 102
    assert freq[("item_7", "zeta omega kappa")]["total_docs"] == 3  # 7: docs 1, 2 y 3
    assert all(r["period"] == "2024Q1" and r["doc_count"] >= 2 for r in t["term_freq"])
    assert "xyz" not in {r["term"] for r in t["term_freq"]}  # texto oculto excluido


def test_edgar_rerun_is_idempotent(cfg, tmp_path):
    processed = tmp_path / "processed"
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r1",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    first, files = read_all(processed, "edgar"), parquet_files(processed)
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r2",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    assert read_all(processed, "edgar") == first
    assert len(parquet_files(processed)) == len(files) == 3  # una partición por tabla


def test_edgar_resumes_after_interruption(cfg, tmp_path):
    clean = tmp_path / "clean"
    run_source(edgar(cfg, FakeEdgar(), tmp_path), cfg, processed_dir=clean, run_id="c", today=TODAY)
    processed = tmp_path / "processed"
    broken = FakeEdgar()
    broken.fail_after = 2  # se corta la red a mitad del trimestre
    broken_cfg = copy.deepcopy(cfg)
    broken_cfg["edgar"]["max_retries"] = 0
    with pytest.raises(ApiError):
        run_source(
            edgar(broken_cfg, broken, tmp_path),
            broken_cfg,
            processed_dir=processed,
            run_id="b",
            today=TODAY,
        )
    assert not TextState(processed, "edgar").is_done("2024Q1")
    assert parquet_files(processed) == []  # nada a medias
    stats = run_source(
        edgar(cfg, FakeEdgar(), tmp_path), cfg, processed_dir=processed, run_id="r", today=TODAY
    )
    assert read_all(processed, "edgar") == read_all(clean, "edgar")
    assert TextState(processed, "edgar").is_done("2024Q1")
    assert not TextState(processed, "edgar").is_done("2024Q2")  # abierto: se reprocesa
    assert stats["pending"] == 0


def test_closed_periods_are_skipped_and_pending_counted(cfg, tmp_path):
    processed = tmp_path / "processed"
    fake = FakeEdgar()

    def go(run_id, **kw):
        src = edgar(cfg, fake, tmp_path)
        return run_source(src, cfg, processed_dir=processed, run_id=run_id, today=TODAY, **kw)

    stats = go("a", max_periods=1)
    assert [p["period"] for p in stats["periods_done_now"]] == ["2024Q1"]
    assert stats["pending"] == 1  # queda 2024Q2 (abierto) por procesar en esta corrida
    fake.requests.clear()
    stats = go("b", max_periods=1)
    assert [p["period"] for p in stats["periods_done_now"]] == ["2024Q2"]
    assert stats["pending"] == 0
    assert not any("QTR1" in r for r in fake.requests)  # el cerrado no se vuelve a pedir
    # El abierto ya se procesó en esta corrida: el bucle de tramos termina.
    assert go("c", max_periods=1)["periods_done_now"] == []


def test_submissions_zip_matches_api(cfg, tmp_path):
    api = tmp_path / "api"
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=api,
        run_id="a",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    zcfg = copy.deepcopy(cfg)
    zcfg["edgar"]["submissions_api_max_ciks"] = 0  # fuerza submissions.zip
    fake = FakeEdgar()
    zipped = tmp_path / "zip"
    run_source(
        edgar(zcfg, fake, tmp_path),
        zcfg,
        processed_dir=zipped,
        run_id="z",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    assert not any(r.startswith("/submissions/") for r in fake.requests)
    assert read_all(zipped, "edgar") == read_all(api, "edgar")


def test_parallel_workers_match_inline(cfg, tmp_path):
    inline = tmp_path / "inline"
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=inline,
        run_id="i",
        today=TODAY,
        only_periods=["2024Q1"],
    )
    pool = tmp_path / "pool"
    run_source(
        edgar(cfg, FakeEdgar(), tmp_path),
        cfg,
        processed_dir=pool,
        run_id="p",
        today=TODAY,
        only_periods=["2024Q1"],
        workers=2,
    )
    assert read_all(pool, "edgar") == read_all(inline, "edgar")


# --- Federal Register --------------------------------------------------------------------------


def fr_fake() -> FakeGovinfo:
    a = [
        ("RULE", "2024-00001", "AGENCY ONE", filler(6, 1) + " zeta omega."),
        ("NOTICE", "2024-00002", "AGENCY TWO", filler(6, 2) + " zeta omega."),
        ("NOTICE", "2024-00003", "AGENCY TWO", filler(6, 4) + " zeta omega."),
        ("NOTICE", "", "AGENCY TWO", filler(6, 5)),  # sin número FR Doc.
    ]
    b = [
        ("NOTICE", "2024-00004", "AGENCY TWO", filler(6, 2) + " zeta omega."),  # = 00002
        ("PRESDOCU", "2024-00005", "", filler(6, 6)),
        ("PRORULE", "2024-00006", "AGENCY THREE", "x."),  # muy corta
    ]
    return FakeGovinfo(
        {
            "FR-2024-01-02.xml": fr_issue(a),
            "FR-2024-01-03.xml": fr_issue(b),
            "FR-2024-02-01.xml": fr_issue([("RULE", "2024-00100", "AGENCY ONE", filler(6, 1))]),
        }
    )


def fr_source(cfg, fake, tmp_path) -> FederalRegisterSource:
    return FederalRegisterSource.from_config(
        cfg, tmp_path / "raw", transport=fake.transport(), sleep=lambda _s: None
    )


def test_federal_register_month(cfg, tmp_path):
    processed = tmp_path / "processed"
    run_source(
        fr_source(cfg, fr_fake(), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r",
        today=TODAY,
        only_periods=["2024-01"],
    )
    t = read_all(processed, "federal_register")
    status = {r["doc_id"]: (r["section"], r["status"], r["detail"]) for r in t["text_ingest"]}
    assert status["2024-00001"] == ("rule", "ok", None)
    assert status["2024-00004"] == ("notice", "duplicado", "duplicado_de=2024-00002")
    assert status["2024-00006"][1] == "muy_corta"
    assert status["2024-01-02#3"] == ("notice", "ok", "sin_numero_frdoc")
    docs = {r["doc_id"]: r for r in t["text_docs"]}
    assert docs["2024-00002"]["entity"] == "AGENCY TWO"
    assert docs["2024-00005"]["date"] == date(2024, 1, 3)
    freq = {(r["section"], r["term"]): r for r in t["term_freq"]}
    assert (
        freq[("notice", "zeta omega")]["doc_count"],
        freq[("notice", "zeta omega")]["total_docs"],
    ) == (2, 3)
    # Los dos avisos con el término son de la misma agencia: amplitud 1.
    assert freq[("notice", "zeta omega")]["n_entities"] == 1
    assert all(1 <= r["n_entities"] <= r["doc_count"] for r in t["term_freq"])
    terms = {r["term"] for r in t["term_freq"]}
    assert not any("billing" in term or "filed" in term for term in terms)  # marcas de imprenta
    assert "sit amet" in terms  # el énfasis en línea (<E>) no parte el n-grama


def test_federal_register_rerun_is_idempotent(cfg, tmp_path):
    processed = tmp_path / "processed"
    for run_id in ("a", "b"):
        run_source(
            fr_source(cfg, fr_fake(), tmp_path),
            cfg,
            processed_dir=processed,
            run_id=run_id,
            today=TODAY,
        )
        if run_id == "a":
            first, files = read_all(processed, "federal_register"), parquet_files(processed)
    assert read_all(processed, "federal_register") == first
    assert parquet_files(processed) == files


def test_catalog_views_over_text_tables(cfg, tmp_path):
    from src.catalog import connect

    processed = tmp_path / "processed"
    run_source(
        fr_source(cfg, fr_fake(), tmp_path),
        cfg,
        processed_dir=processed,
        run_id="r",
        today=TODAY,
        only_periods=["2024-01"],
    )
    con = connect(processed)
    n, total = con.execute(
        "SELECT count(*), max(total_docs) FROM term_freq WHERE section = 'notice'"
    ).fetchone()
    assert n > 0 and total == 3
    assert con.execute("SELECT count(*) FROM text_docs").fetchone()[0] == 5
    assert con.execute("SELECT count(*) FROM text_ingest").fetchone()[0] == 7
    assert con.execute("SELECT count(*) FROM series").fetchone()[0] == 0  # vistas vacías ok


def test_migrate_adds_null_n_entities_without_download(tmp_path):
    """Particiones anteriores al esquema 2: la migración agrega n_entities = NULL; idempotente."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.ingest.text import migrate

    processed = tmp_path / "processed"
    part = processed / "term_freq" / "edgar" / "2010Q1"
    part.mkdir(parents=True)
    old = pa.table(
        {
            "source": ["edgar"],
            "section": ["item_1a"],
            "period": ["2010Q1"],
            "term": ["zeta"],
            "doc_count": pa.array([7], pa.int64()),
            "total_docs": pa.array([9], pa.int64()),
        }
    )
    pq.write_table(old, part / "part-old.parquet")
    assert migrate(processed) == {"particiones": 1, "migradas": 1}
    rows = read_all(processed, "edgar")["term_freq"]
    assert rows == [
        {
            "source": "edgar",
            "section": "item_1a",
            "period": "2010Q1",
            "term": "zeta",
            "doc_count": 7,
            "total_docs": 9,
            "n_entities": None,
        }
    ]
    assert migrate(processed) == {"particiones": 1, "migradas": 0}
    assert len(list(part.glob("*.parquet"))) == 1


def test_old_schema_periods_are_reprocessed_only_where_configured(cfg, tmp_path):
    from datetime import UTC, datetime

    state = TextState(tmp_path, "federal_register")
    state.mark("2024-01", closed=True, run_id="r", stats={"docs": 1, "rows": {}})
    state.periods["2024-01"].pop("schema")  # como lo dejó la versión 1
    now = datetime.now(UTC)
    assert state.needs_run("2024-01", 20, now, min_schema=2)  # FR: se re-procesa
    assert not state.needs_run("2024-01", 20, now, min_schema=1)  # EDGAR: no
    assert cfg["federal_register"]["reprocess_below_schema"] == 2
    assert cfg["edgar"]["reprocess_below_schema"] == 1
    state.mark("2024-01", closed=True, run_id="r2", stats={"docs": 1, "rows": {}})
    assert not state.needs_run("2024-01", 20, now, min_schema=2)
