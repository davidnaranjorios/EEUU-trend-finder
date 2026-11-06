"""Construye config/naics/*.csv desde las tablas oficiales de Census (sin mapeos manuales).

Uso (herramienta puntual, no forma parte de src/):
    uv run --no-project --with pandas --with xlrd --with openpyxl python \
        scripts/build_naics_tables.py RAW_DIR OUT_DIR
RAW_DIR contiene los archivos descargados de https://www.census.gov/naics/ (ver SOURCES.md).
La concordancia SIC 1987 -> NAICS 2022 encadena las concordancias oficiales
SIC87->2002->2007->2012->2017->2022; el peso de cada par es 1/n sobre los NAICS 2022 alcanzables.
"""

import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd


def pairs(path: Path, a: int, b: int, header: int) -> dict[str, set[str]]:
    df = pd.read_excel(path, header=None, skiprows=header + 1, dtype=str)
    out: dict[str, set[str]] = defaultdict(set)
    for x, y in zip(df[a], df[b], strict=False):
        if isinstance(x, str) and isinstance(y, str) and x.strip().isdigit() and y.strip().isdigit():
            out[x.strip()].add(y.strip())
    return out


def main(raw: Path, out: Path) -> None:
    names = pd.read_excel(raw / "2-6_digit_2022_Codes.xlsx", dtype=str).iloc[:, 1:3]
    names.columns = ["naics", "name"]
    names = names.dropna()
    names = names[names.naics.str.strip().str.fullmatch(r"\d{2,6}")]
    names["naics"] = names.naics.str.strip()
    names["name"] = names.name.str.strip().str.replace(r"T$", "", regex=True)
    names.drop_duplicates("naics").to_csv(out / "naics_2022.csv", index=False)

    sic = pairs(raw / "1987_SIC_to_2002_NAICS.xls", 0, 2, 0)
    steps = [
        pairs(raw / f, 0, 2, 2)
        for f in (
            "2002_to_2007_NAICS.xls",
            "2007_to_2012_NAICS.xls",
            "2012_to_2017_NAICS.xlsx",
            "2017_to_2022_NAICS.xlsx",
        )
    ]
    rows = []
    for s, codes in sic.items():
        for step in steps:
            codes = {n for c in codes for n in step.get(c, ())}
        for n in sorted(codes):
            rows.append((s.zfill(4), n, 1 / len(codes)))
    pd.DataFrame(rows, columns=["sic", "naics", "weight"]).to_csv(
        out / "sic_naics_concordance.csv", index=False, float_format="%.6f"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
