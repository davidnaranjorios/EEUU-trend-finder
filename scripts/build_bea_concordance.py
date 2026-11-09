"""Construye config/naics/bea_industry_naics.csv desde tablas oficiales (sin mapeos manuales).

Uso (herramienta puntual, no forma parte de src/):
    uv run --no-project --with pandas --with openpyxl python \
        scripts/build_bea_concordance.py BEA_XLSX CENSUS_2017_TO_2022_XLSX NAICS_2022_CSV \
        OUT_BEA_CSV OUT_2017_CSV

Entradas (ver config/naics/SOURCES.md):
- BEA, "BEA Industry and Commodity Codes and NAICS Concordance" (hoja "NAICS Codes"): por cada
  código BEA (sector, resumen, resumen ampliado, detalle y detalle de producción bruta) los
  códigos NAICS 2017 relacionados.
- Census, concordancia oficial 2017 → 2022 NAICS.
- `naics_2022.csv` (tabla oficial de nombres, para comprimir).

Pasos: (1) código BEA → NAICS 2017 de la tabla de BEA ("23*" cuenta como 23; "n.a." se omite);
(2) cada código 2017 de cualquier largo se expande a sus códigos de 6 dígitos de la concordancia de
Census y se pasa a NAICS 2022; (3) se sube al padre cuando están todos sus hijos de la tabla 2022
(así «Farms» queda en 111 y 112 y no en 40 códigos de 6 dígitos); (4) peso uniforme 1/n.
Además escribe `naics_2017_to_2022.csv` (naics2017, naics2022, weight): los códigos de 2017 que
cambiaron en 2022, con peso 1/n; sirve para leer códigos NAICS 2017 de Census (ASM, BDS, AIES).
Un mismo código que aparece en varios niveles de BEA (p. ej. 211 como resumen y como detalle) une
sus NAICS.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

BEA_COLUMNS = ["sector", "sector_d", "summary", "summary_d", "usummary", "usummary_d", "detail"]
BEA_COLUMNS += ["detail_d", "go", "go_d", "notes", "naics", "naics_d"]
CODE_COLS = ("sector", "summary", "usummary", "detail", "go")


def bea_pairs(path: Path) -> dict[str, set[str]]:
    df = pd.read_excel(path, header=4, dtype=str)
    df.columns = BEA_COLUMNS
    out: dict[str, set[str]] = defaultdict(set)
    for _, row in df.iterrows():
        naics = str(row["naics"]).strip().rstrip("*")
        if not re.fullmatch(r"\d{2,6}", naics):
            continue  # "n.a.", vacíos y notas al pie
        for col in CODE_COLS:
            code = row[col]
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9]{1,6}", code.strip()):
                out[code.strip()].add(naics)
    return out


def census_2017_to_2022(path: Path) -> dict[str, set[str]]:
    df = pd.read_excel(path, header=None, skiprows=3, dtype=str)
    out: dict[str, set[str]] = defaultdict(set)
    for a, b in zip(df[0], df[2], strict=False):
        if (
            isinstance(a, str)
            and isinstance(b, str)
            and a.strip().isdigit()
            and b.strip().isdigit()
        ):
            out[a.strip()].add(b.strip())
    return out


def children(parent: str, names: set[str]) -> set[str]:
    """Hijos directos de `parent` en la tabla 2022 (los rangos 31-33, 44-45, 48-49 incluidos)."""
    if "-" in parent:
        lo, hi = (int(x) for x in parent.split("-"))
        return {c for c in names if len(c) == 3 and lo <= int(c[:2]) <= hi}
    if len(parent) == 2:
        return {c for c in names if len(c) == 3 and c.startswith(parent)}
    return {c for c in names if len(c) == len(parent) + 1 and c.startswith(parent)}


def parent_of(code: str, names: set[str]) -> str | None:
    if len(code) > 2:
        return (
            code[:-1] if len(code) > 3 else (code[:2] if code[:2] in names else _range(code, names))
        )
    return None


def _range(code: str, names: set[str]) -> str | None:
    two = int(code[:2])
    for n in names:
        if "-" in n:
            lo, hi = (int(x) for x in n.split("-"))
            if lo <= two <= hi:
                return n
    return None


def compress(codes: set[str], names: set[str]) -> set[str]:
    """Sube cada grupo de hermanos al padre cuando están todos sus hijos de la tabla 2022."""
    codes = set(codes)
    changed = True
    while changed:
        changed = False
        for c in sorted(codes, key=len, reverse=True):
            p = parent_of(c, names)
            if p and p in names and children(p, names) <= codes:
                codes -= children(p, names)
                codes.add(p)
                changed = True
                break
    return codes


def main(
    bea_xlsx: Path, census_xlsx: Path, names_csv: Path, out_csv: Path, out_2017_csv: Path
) -> None:
    names = set(pd.read_csv(names_csv, dtype=str)["naics"])
    to2022 = census_2017_to_2022(census_xlsx)
    rows = []
    for bea, related in sorted(bea_pairs(bea_xlsx).items()):
        c2022: set[str] = set()
        for r in related:
            six = [k for k in to2022 if k.startswith(r)]
            for k in six:
                c2022 |= to2022[k]
        # Solo códigos de la tabla oficial 2022 (los de 6 dígitos sin nombre propio se descartan).
        c2022 = {c for c in c2022 if c in names}
        final = compress(c2022, names)
        for n in sorted(final):
            rows.append((bea, n, 1 / len(final)))
    pd.DataFrame(rows, columns=["bea_code", "naics", "weight"]).to_csv(
        out_csv, index=False, float_format="%.6f"
    )
    print(f"{len({r[0] for r in rows})} códigos BEA, {len(rows)} filas -> {out_csv}")
    changed = [
        (a, b, 1 / len(bs))
        for a, bs in sorted(to2022.items())
        for b in sorted(bs)
        if bs != {a} and all(x in names for x in bs)
    ]
    pd.DataFrame(changed, columns=["naics2017", "naics2022", "weight"]).to_csv(
        out_2017_csv, index=False, float_format="%.6f"
    )
    print(f"{len({r[0] for r in changed})} códigos 2017 cambiados -> {out_2017_csv}")


if __name__ == "__main__":
    main(*(Path(a) for a in sys.argv[1:6]))
