"""Servidores falsos de SEC EDGAR y govinfo (Federal Register) para los tests de texto.

El contenido es sintético (tokens sin significado): los tests no dependen de ningún tema.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable

import httpx

WORDS = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor".split()


def filler(n_sentences: int, seed: int) -> str:
    out = []
    for i in range(n_sentences):
        words = [WORDS[(seed + i * 7 + j * 3) % len(WORDS)] for j in range(9)]
        out.append(" ".join(words) + ".")
    return " ".join(out)


def tenk_html(body_1a: str, body_7: str, *, hidden: str = "") -> str:
    """10-K mínimo: índice (con los mismos encabezados) + secciones reales."""
    toc = (
        "<table><tr><td>Item 1A.</td><td>Risk Factors</td><td>3</td></tr>"
        "<tr><td>Item 1B.</td><td>Unresolved</td><td>9</td></tr>"
        "<tr><td>Item 7.</td><td>Discussion</td><td>12</td></tr>"
        "<tr><td>Item 7A.</td><td>Market</td><td>20</td></tr></table>"
    )
    return (
        "<html><body>"
        f'<div style="display:none"><ix:header>{hidden}</ix:header></div>'
        f"{toc}"
        "<p>Item 1. Business</p><p>" + filler(3, 1) + "</p>"
        # Encabezado partido en dos líneas del fuente, como en muchos 10-K reales.
        "<p><b>ITEM\n1A.</b> RISK FACTORS</p><p>" + body_1a + "</p>"
        "<p>Item 1B. Unresolved</p><p>None.</p>"
        "<p>Item 7. Discussion</p><p>" + body_7 + "</p>"
        "<p>Item 7A. Market</p><p>" + filler(2, 5) + "</p>"
        "<p>Item 8. Statements</p></body></html>"
    )


def form_index(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    """form.gz: cabecera, guiones y filas de ancho fijo (form, empresa, cik, fecha, path)."""
    lines = [
        "Description:           Master Index of EDGAR Dissemination Feed by Form Type",
        "",
        "Form Type   Company Name                                                  CIK         "
        "Date Filed  File Name",
        "-" * 140,
    ]
    for form, company, cik, filed, path in rows:
        lines.append(f"{form:<17}{company:<62}{cik:<12}{filed:<12}{path}")
    return gzip.compress(("\n".join(lines) + "\n").encode("latin-1"))


class FakeEdgar:
    """Un trimestre (2024Q1) con: dos 10-K normales, uno con co-registrante, un 10-K/A, un 10-K
    cuyo documento da 404, uno con Item 1A corto y uno idéntico a otro (duplicado)."""

    def __init__(self, inject: str = "", inject_docs: int = 0) -> None:
        self.requests: list[str] = []
        self.fail_after: int | None = None  # simula un corte: error de red tras N documentos
        self.user_agents: set[str] = set()
        self.docs: dict[str, str] = {}
        rows = []
        subs: dict[int, dict[str, list[str]]] = {}
        specs = [
            ("0000000001-24-000001", "101", "10-K", "2024-02-01", "long"),
            ("0000000001-24-000002", "102", "10-K", "2024-02-15", "long"),
            ("0000000001-24-000003", "103", "10-K", "2024-03-01", "short1a"),
            ("0000000001-24-000004", "104", "10-K", "2024-03-02", "missing"),
            ("0000000001-24-000005", "105", "10-K/A", "2024-03-03", "long"),
            ("0000000001-24-000006", "106", "10-K", "2024-03-04", "copy_of_1"),
        ]
        for i, (acc, cik, form, filed, kind) in enumerate(specs):
            path = f"edgar/data/{cik}/{acc}.txt"
            rows.append((form, f"Company {cik}", cik, filed, path))
            name = f"doc{i}.htm"
            subs.setdefault(int(cik), {"accessionNumber": [], "primaryDocument": []})
            subs[int(cik)]["accessionNumber"].append(acc)
            subs[int(cik)]["primaryDocument"].append(name)
            url = f"/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{name}"
            seed = 0 if kind == "copy_of_1" else i  # copia exacta del primero
            extra = f" {inject}." if inject and (i < inject_docs or kind == "copy_of_1") else ""
            body_1a = "Not applicable." if kind == "short1a" else filler(40, seed) + extra
            if kind != "missing":
                self.docs[url] = tenk_html(body_1a, filler(30, seed + 2) + extra, hidden="xyz")
        # Co-registrante: el mismo accession listado también bajo otro CIK.
        rows.append(("10-K", "Company 101 Sub", "999", "2024-02-01", rows[0][4]))
        self.index = form_index(rows)
        self.subs = subs

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        self.user_agents.add(request.headers.get("User-Agent", ""))
        if path == "/Archives/edgar/full-index/2024/QTR1/form.gz":
            return httpx.Response(200, content=self.index)
        if path.startswith("/Archives/edgar/full-index/"):
            return httpx.Response(200, content=form_index([]))
        if path.startswith("/submissions/CIK"):
            cik = int(path.removeprefix("/submissions/CIK").removesuffix(".json"))
            if cik not in self.subs:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"cik": cik, "filings": {"recent": self.subs[cik]}})
        if path in self.docs:
            if self.fail_after is not None:
                if self.fail_after <= 0:
                    raise httpx.ConnectError("corte simulado")
                self.fail_after -= 1
            return httpx.Response(200, text=self.docs[path])
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def fr_issue(docs: list[tuple[str, str, str, str]]) -> bytes:
    """XML diario mínimo: (tipo, número FR Doc., agencia, texto)."""
    parts = {"RULE": [], "PRORULE": [], "NOTICE": [], "PRESDOCU": []}
    for tag, num, agency, text in docs:
        frdoc = f"<FRDOC>[FR Doc. {num} Filed 1-1-24; 8:45 am]</FRDOC>" if num else ""
        parts[tag].append(
            f"<{tag}><PREAMB><AGENCY>{agency}</AGENCY><SUBJECT>S</SUBJECT></PREAMB>"
            f"<SUPLINF><P>{text}</P><P>{filler(2, 3)} <E T='03'>sit</E> amet.</P></SUPLINF>"
            f"{frdoc}<BILCOD>BILLING CODE 0000-00-P</BILCOD></{tag}>"
        )
    body = "".join(
        f"<{group}>{''.join(parts[tag])}</{group}>"
        for group, tag in (
            ("RULES", "RULE"),
            ("PRORULES", "PRORULE"),
            ("NOTICES", "NOTICE"),
            ("PRESDOCS", "PRESDOCU"),
        )
    )
    return f"<?xml version='1.0'?><FEDREG><VOL>1</VOL>{body}</FEDREG>".encode()


class FakeGovinfo:
    def __init__(self, issues: dict[str, bytes]) -> None:
        self.issues = issues  # nombre de archivo (FR-AAAA-MM-DD.xml) -> XML
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path.startswith("/bulkdata/json/FR/"):
            ym = path.removeprefix("/bulkdata/json/FR/").replace("/", "-")
            files = [{"name": n, "folder": False} for n in sorted(self.issues) if n[3:10] == ym]
            return httpx.Response(200, json={"files": files})
        name = path.rsplit("/", 1)[-1]
        if name in self.issues:
            return httpx.Response(200, content=self.issues[name])
        return httpx.Response(404, text="nf")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def no_sleep() -> Callable[[float], None]:
    return lambda _s: None
