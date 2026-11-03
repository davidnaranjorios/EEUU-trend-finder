"""SEC EDGAR: Item 1A e Item 7 de los 10-K, por trimestre de presentación (ADR 0005).

Referencia de diseño: Hanley & Hoberg (2019) usan el texto de los 10-K (riesgos) para detectar
riesgos emergentes. Aquí solo se extraen las secciones por su estructura (encabezados "Item
1A", "Item 7"), sin ninguna palabra de contenido.

Acceso (verificado el 2026-11-03 en sec.gov, "Accessing EDGAR Data"): máximo 10 solicitudes
por segundo y User-Agent con nombre y correo (`SEC_USER_AGENT`, nunca en el repo).

Flujo de un trimestre:
1. `full-index/<año>/QTR<n>/form.gz`: lista de presentaciones del trimestre.
2. Documento principal de cada 10-K: `primaryDocument` de submissions (API por CIK si son
   pocos; si no, `submissions.zip`, una descarga por corrida). Si no aparece, se usa la
   presentación completa (.txt) y se toma su primer documento del tipo del formulario.
3. Descarga a `edgar.requests_per_second` y análisis en workers (HTML -> texto -> secciones
   -> n-gramas).
"""

from __future__ import annotations

import gzip
import io
import json
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from lxml import html as lhtml

from src.ingest.http import ApiClient, ApiError, RateLimiter
from src.ingest.text import CollectContext, DocResult, SectionResult, analyze_section

SOURCE = "edgar"
SECTIONS = ("item_1a", "item_7")

# Encabezados al inicio de línea. "Item 7" no casa con "Item 7A" (\b exige fin de palabra).
_H = r"^[^\w\n]{0,10}item\W{0,10}"
SECTION_PATTERNS = {
    "item_1a": (re.compile(_H + r"1a\b", re.I | re.M), re.compile(_H + r"(1b|2)\b", re.I | re.M)),
    "item_7": (re.compile(_H + r"7\b", re.I | re.M), re.compile(_H + r"(7a|8)\b", re.I | re.M)),
}
INDEX_LINE_RE = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<company>.*?)\s{2,}(?P<cik>\d+)\s{2,}"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s{2,}(?P<file>\S+)\s*$"
)
BLOCK_TAG_RE = re.compile(r"^(p|div|br|tr|li|h[1-6]|table|center|section|article|pre)$", re.I)
WS_RE = re.compile(r"\s+")
HTML_TAG_RE = re.compile(r"<(html|body|div|p|table|font|br)[\s>/]", re.I)
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.I)
DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S | re.I)
DOC_TYPE_RE = re.compile(r"<TYPE>\s*([^\s<]+)", re.I)
DOC_TEXT_RE = re.compile(r"<TEXT>(.*?)(?:</TEXT>|$)", re.S | re.I)


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    cik: str
    date_filed: date
    path: str  # edgar/data/<cik>/<accession>.txt


# --- Índice ------------------------------------------------------------------------------------


def parse_form_index(text: str) -> list[Filing]:
    """Filas de `form.idx` (ancho fijo, separadas por 2+ espacios) tras la línea de guiones."""
    out: list[Filing] = []
    body = text.split("\n-----", 1)[-1].split("\n", 1)[-1] if "\n-----" in text else text
    for line in body.splitlines():
        m = INDEX_LINE_RE.match(line.rstrip())
        if not m:
            continue
        path = m["file"]
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        out.append(Filing(accession, m["form"], m["cik"], date.fromisoformat(m["date"]), path))
    return out


def quarter_of(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(period: str) -> tuple[date, date]:
    year, q = int(period[:4]), int(period[-1])
    start = date(year, 3 * (q - 1) + 1, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
    return start, end - timedelta(days=1)


# --- Texto -------------------------------------------------------------------------------------


def html_to_text(payload: bytes) -> str:
    """Texto visible de un documento HTML/iXBRL, con saltos de línea en los bloques."""
    doc = lhtml.document_fromstring(payload)
    for el in list(doc.iter()):
        tag = el.tag if isinstance(el.tag, str) else ""
        hidden = HIDDEN_STYLE_RE.search(el.get("style") or "") if tag else None
        if tag.lower() in ("script", "style", "ix:header") or hidden:
            el.drop_tree()
    # En HTML los saltos de línea del fuente son solo espacio: se colapsan, y los bloques
    # (párrafos, filas, divs) terminan en salto de línea.
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.text and el.tag.lower() != "pre":
            el.text = WS_RE.sub(" ", el.text)
        if el.tail:
            el.tail = WS_RE.sub(" ", el.tail)
        if BLOCK_TAG_RE.match(el.tag):
            el.tail = "\n" + (el.tail or "")
    return doc.text_content()


def looks_like_html(text: str) -> bool:
    """HTML real, no texto plano con marcas SGML de EDGAR (<PAGE>, <TYPE>...)."""
    return bool(HTML_TAG_RE.search(text[:20000]))


def main_document(submission: str, form: str) -> str | None:
    """Primer documento del tipo `form` dentro de una presentación completa (.txt)."""
    for block in DOCUMENT_RE.findall(submission):
        m = DOC_TYPE_RE.search(block)
        if m and m.group(1).upper() == form.upper():
            t = DOC_TEXT_RE.search(block)
            return t.group(1) if t else block
    return None


def extract_sections(text: str) -> dict[str, str]:
    """Item 1A e Item 7: entre cada encabezado de inicio y el primer encabezado de fin que le
    sigue se toma el tramo más largo (así el índice del documento, con tramos de una línea,
    nunca gana a la sección real)."""
    out: dict[str, str] = {}
    for name, (start_re, end_re) in SECTION_PATTERNS.items():
        ends = [m.start() for m in end_re.finditer(text)]
        best = ""
        for m in start_re.finditer(text):
            nxt = next((e for e in ends if e > m.end()), None)
            if nxt is not None and nxt - m.start() > len(best):
                best = text[m.start() : nxt]
        if best:
            out[name] = best
    return out


def analyze_filing(
    filing: Filing, payload: bytes, kind: str, tmp_dir: str, min_chars: int, n_max: int
) -> DocResult:
    """Worker: documento -> secciones -> n-gramas. `kind` es 'primary' o 'submission'."""
    doc = DocResult(filing.accession, filing.date_filed, filing.cik)
    text = payload.decode("utf-8", errors="replace")
    if kind == "submission":
        text = main_document(text, filing.form) or ""
    if looks_like_html(text):
        try:
            text = html_to_text(text.encode("utf-8"))
        except (ValueError, TypeError) as exc:  # lxml: documento vacío o ilegible
            doc.sections.append(SectionResult("*", "excluido", f"html_ilegible={exc}"[:200]))
            return doc
    found = extract_sections(text)
    for name in SECTIONS:
        if name not in found:
            doc.sections.append(SectionResult(name, "sin_seccion"))
            continue
        tmp = Path(tmp_dir) / name / f"{filing.accession}.txt.gz"
        doc.sections.append(
            analyze_section(name, found[name], min_chars=min_chars, n_max=n_max, tmp_path=tmp)
        )
    return doc


# --- Fuente ------------------------------------------------------------------------------------


class EdgarSource:
    name = SOURCE

    def __init__(
        self,
        cfg: dict[str, Any],
        www: ApiClient,
        data: ApiClient,
        raw_dir: Path,
    ) -> None:
        self.cfg = cfg
        self.www = www
        self.data = data
        self.raw_dir = raw_dir
        self.forms = set(cfg["forms"])

    @classmethod
    def from_config(
        cls, cfg: dict[str, Any], user_agent: str, raw_dir: Path, **client_kw: Any
    ) -> EdgarSource:
        e = cfg[SOURCE]
        limiter = RateLimiter(e["requests_per_second"] * 60)
        common = dict(
            auth_params={},
            requests_per_minute=e["requests_per_second"] * 60,
            max_retries=e["max_retries"],
            timeout_s=e["timeout_s"],
            backoff_base_s=e["backoff_base_s"],
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            limiter=limiter,
            **client_kw,
        )
        www = ApiClient(e["base_url"], **common)
        data = ApiClient(e["data_url"], **common)
        return cls(e, www, data, raw_dir)

    def close(self) -> None:
        self.www.close()
        self.data.close()

    # Periodos
    def periods(self, today: date) -> list[str]:
        out = []
        for year in range(self.cfg["start_year"], today.year + 1):
            for q in range(1, 5):
                p = f"{year}Q{q}"
                if quarter_bounds(p)[0] <= today:
                    out.append(p)
        return out

    def is_closed(self, period: str, today: date) -> bool:
        return today > quarter_bounds(period)[1] + timedelta(days=self.cfg["closed_grace_days"])

    # Un trimestre
    def collect(self, period: str, ctx: CollectContext) -> list[DocResult]:
        year, q = period[:4], period[-1]
        raw = self.www.get_bytes(f"/Archives/edgar/full-index/{year}/QTR{q}/form.gz")
        text = _maybe_gunzip(raw).decode("latin-1")
        entries = parse_form_index(text)
        docs: list[DocResult] = []
        family = [f for f in entries if f.form.upper().startswith("10-K")]
        by_acc: dict[str, Filing] = {}
        for f in sorted(family, key=lambda f: (f.accession, int(f.cik))):
            by_acc.setdefault(f.accession, f)  # co-registrantes: mismo accession, varios CIK
        targets = [f for f in by_acc.values() if f.form in self.forms]
        for f in by_acc.values():
            if f.form not in self.forms:
                docs.append(
                    DocResult(
                        f.accession,
                        f.date_filed,
                        f.cik,
                        [SectionResult("*", "excluido", f"formulario={f.form}")],
                    )
                )
        if ctx.sample is not None and ctx.sample < len(targets):
            rng = random.Random(f"{ctx.seed}:{period}")
            targets = sorted(rng.sample(targets, ctx.sample), key=lambda f: f.accession)
        primary = self.primary_documents(targets)

        def tasks() -> Any:
            for f in targets:
                name = primary.get(f.accession)
                if name and name.lower().endswith((".htm", ".html", ".txt")):
                    folder = f"/Archives/edgar/data/{int(f.cik)}/{_nodash(f.accession)}"
                    url, kind = f"{folder}/{name}", "primary"
                else:
                    url, kind = f"/{f.path}", "submission"
                try:
                    payload = self.www.get_bytes(url)
                except ApiError as exc:
                    if exc.status in (403, 429) or exc.status is None:
                        raise  # User-Agent rechazado o red caída: no seguir a ciegas
                    reason = f"error_descarga=HTTP {exc.status}"
                    excluded = [SectionResult("*", "excluido", reason)]
                    failed.append(DocResult(f.accession, f.date_filed, f.cik, excluded))
                    continue
                yield (f, payload, kind, str(ctx.tmp_dir), ctx.min_chars, ctx.n_max)

        failed: list[DocResult] = []
        analyzed = list(ctx.run_tasks(analyze_filing, tasks()))
        return docs + analyzed + failed

    # Documento principal
    def primary_documents(self, filings: list[Filing]) -> dict[str, str]:
        wanted = {f.accession for f in filings}
        ciks = sorted({int(f.cik) for f in filings})
        out: dict[str, str] = {}
        if len(ciks) <= self.cfg["submissions_api_max_ciks"]:
            for cik in ciks:
                try:
                    sub = self.data.get_json(f"/submissions/CIK{cik:010d}.json", {})
                except ApiError as exc:
                    if exc.status == 404:
                        continue
                    raise
                out.update(_primary_from(sub, wanted))
                for extra in sub.get("filings", {}).get("files", []):
                    if wanted <= out.keys():
                        break
                    more = self.data.get_json(f"/submissions/{extra['name']}", {})
                    out.update(_primary_from(more, wanted))
            return out
        zpath = self._submissions_zip()
        with zipfile.ZipFile(zpath) as zf:
            # ~1 M de archivos en el zip: índice por CIK una sola vez (no un recorrido por CIK).
            by_cik: dict[str, list[str]] = {}
            for name in zf.namelist():
                by_cik.setdefault(name[3:13], []).append(name)
            for cik in ciks:
                for name in sorted(by_cik.get(f"{cik:010d}", [])):
                    out.update(_primary_from(json.loads(zf.read(name)), wanted))
        return out

    def _submissions_zip(self) -> Path:
        path = self.raw_dir / SOURCE / "submissions.zip"
        fresh = path.exists() and (
            datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
            < timedelta(hours=self.cfg["submissions_zip_max_age_hours"])
        )
        if not fresh:
            self.www.download("/Archives/edgar/daily-index/bulkdata/submissions.zip", path)
        return path


def _primary_from(sub: dict[str, Any], wanted: set[str]) -> dict[str, str]:
    """accession -> primaryDocument, del JSON de submissions (principal o archivo extra)."""
    block = sub.get("filings", {}).get("recent", sub)
    accs = block.get("accessionNumber") or []
    docs = block.get("primaryDocument") or []
    return {a: d for a, d in zip(accs, docs, strict=False) if a in wanted and d}


def _nodash(accession: str) -> str:
    return accession.replace("-", "")


def _maybe_gunzip(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw
