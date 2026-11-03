"""Federal Register: XML diario de govinfo (GPO), por mes de publicación (ADR 0005).

Se usa el repositorio masivo de govinfo (`bulkdata/FR`, XML diario desde 2000) y no la API de
federalregister.gov: la API tope `count` en 10.000 por consulta y habría que pedir cientos de
miles de textos, mientras que el masivo trae un mes entero en un zip (~12 MB) o en los ~21 XML
diarios. Un documento es cada elemento RULE, PRORULE, NOTICE o PRESDOCU; su `section` es el
tipo (la etiqueta en minúsculas), `doc_id` el número "FR Doc." y `entity` la agencia.
"""

from __future__ import annotations

import copy
import random
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from lxml import etree

from src.ingest.http import ApiClient
from src.ingest.text import CollectContext, DocResult, analyze_section

SOURCE = "federal_register"
DOC_TAGS = ("RULE", "PRORULE", "NOTICE", "PRESDOCU")
SKIP_TAGS = ("FRDOC", "BILCOD", "PRTPAGE")
INLINE_TAGS = ("E", "SU")
FRDOC_RE = re.compile(r"FR\s*Doc\.?\s*(?:No\.?\s*)?([0-9A-Z][0-9A-Z-]*[0-9])", re.I)
ISSUE_RE = re.compile(r"FR-(\d{4}-\d{2}-\d{2})\.xml$")


def month_bounds(period: str) -> tuple[date, date]:
    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end - timedelta(days=1)


def _text_of(el: etree._Element) -> str:
    """Texto del documento sin los elementos de imprenta (número FR Doc., código, página).

    Cada elemento de bloque termina en salto de línea; los de formato en línea (E: énfasis,
    SU: superíndice) no, para no partir n-gramas.
    """
    el = copy.deepcopy(el)
    etree.strip_elements(el, *SKIP_TAGS, with_tail=False)
    for node in el.iter():
        if isinstance(node.tag, str) and node.tag not in INLINE_TAGS:
            node.tail = "\n" + (node.tail or "")
    return "".join(el.itertext())


def analyze_issue(
    xml: bytes, issue_date: date, tmp_dir: str, min_chars: int, n_max: int
) -> list[DocResult]:
    """Worker: un número diario -> documentos -> n-gramas."""
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(xml, parser)
    out: list[DocResult] = []
    if root is None:
        return out
    for i, el in enumerate(root.iter(*DOC_TAGS)):
        section = el.tag.lower()
        frdoc = " ".join(t for t in el.xpath(".//FRDOC//text()"))
        m = FRDOC_RE.search(frdoc)
        doc_id = m.group(1) if m else f"{issue_date.isoformat()}#{i}"
        agency = el.find(".//AGENCY")
        entity = " ".join(agency.itertext()).strip() if agency is not None else None
        doc = DocResult(doc_id, issue_date, entity or None)
        tmp = Path(tmp_dir) / section / f"{doc_id}.txt.gz"
        doc.sections.append(
            analyze_section(section, _text_of(el), min_chars=min_chars, n_max=n_max, tmp_path=tmp)
        )
        if not m:
            doc.sections[0].detail = "sin_numero_frdoc"
        out.append(doc)
    return out


class FederalRegisterSource:
    name = SOURCE

    def __init__(self, cfg: dict[str, Any], client: ApiClient, raw_dir: Path) -> None:
        self.cfg = cfg
        self.client = client
        self.raw_dir = raw_dir

    @classmethod
    def from_config(cls, cfg: dict[str, Any], raw_dir: Path, **client_kw: Any) -> Any:
        c = cfg[SOURCE]
        client = ApiClient(
            c["base_url"],
            {},
            requests_per_minute=c["requests_per_second"] * 60,
            max_retries=c["max_retries"],
            timeout_s=c["timeout_s"],
            backoff_base_s=c["backoff_base_s"],
            headers={"Accept": "application/json"},
            **client_kw,
        )
        return cls(c, client, raw_dir)

    def close(self) -> None:
        self.client.close()

    def periods(self, today: date) -> list[str]:
        out = []
        for year in range(self.cfg["start_year"], today.year + 1):
            for month in range(1, 13):
                if date(year, month, 1) <= today:
                    out.append(f"{year}-{month:02d}")
        return out

    def is_closed(self, period: str, today: date) -> bool:
        return today > month_bounds(period)[1] + timedelta(days=self.cfg["closed_grace_days"])

    def collect(self, period: str, ctx: CollectContext) -> list[DocResult]:
        year, month = period[:4], period[5:7]
        listing = self.client.get_json(f"/bulkdata/json/FR/{year}/{month}", {})
        files = [f for f in listing.get("files", []) if not f.get("folder")]
        issues: list[tuple[str, bytes]] = []
        zips = [f for f in files if f["name"].endswith(".zip")]
        if zips:
            path = ctx.tmp_dir / zips[0]["name"]
            self.client.download(f"/bulkdata/FR/{year}/{month}/{zips[0]['name']}", path)
            with zipfile.ZipFile(path) as zf:
                for name in sorted(zf.namelist()):
                    if ISSUE_RE.search(name):
                        issues.append((name, zf.read(name)))
            path.unlink()
        else:
            for f in sorted(files, key=lambda f: f["name"]):
                if ISSUE_RE.search(f["name"]):
                    payload = self.client.get_bytes(f"/bulkdata/FR/{year}/{month}/{f['name']}")
                    issues.append((f["name"], payload))
        tasks = [
            (
                xml,
                date.fromisoformat(ISSUE_RE.search(name).group(1)),
                str(ctx.tmp_dir),
                ctx.min_chars,
                ctx.n_max,
            )
            for name, xml in issues
        ]
        docs = [d for batch in ctx.run_tasks(analyze_issue, tasks) for d in batch]
        if ctx.sample is not None and ctx.sample < len(docs):
            rng = random.Random(f"{ctx.seed}:{period}")
            docs = rng.sample(docs, ctx.sample)
        return docs
