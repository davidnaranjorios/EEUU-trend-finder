"""Business Dynamics Statistics (Census), nivel nacional, por NAICS y año (ADR 0024).

`api.census.gov/data/timeseries/bds` con `NAICS=*&YEAR=*` en una consulta: FIRM, ESTAB,
ESTABS_ENTRY_RATE y ESTABS_EXIT_RATE. BDS publica todos los años con el NAICS 2017.
`series_id = bds:<VARIABLE>:<NAICS>`; fecha = 1 de enero del año.
"""

from __future__ import annotations

import sys

from src.ingest import common
from src.ingest.http import ApiError
from src.ingest.naics_base import (
    AGENCY,
    MSeries,
    NaicsAdapter,
    clean_code,
    make_adapter_for,
    units_of,
)

SOURCE = "bds"
__all__ = ["AGENCY", "SOURCE", "BdsAdapter", "main"]


class BdsAdapter(NaicsAdapter):
    source = SOURCE

    def _dataset_paths(self) -> list[tuple[str, str, str]]:
        out = []
        for ds in self._find_datasets("bds"):
            if "timeseries" in (ds.get("c_dataset") or []):
                out.append((self._path(ds), "", str(ds.get("modified") or "")))
        return sorted(out)

    def _load(self) -> dict[str, MSeries]:
        measures: list[str] = list(self.scfg["measures"])
        series: dict[str, MSeries] = {}
        for path, _, _ in self._dataset_paths():
            variables = self._variables(path)
            naics = self._naics_var(variables)
            if naics is None:
                raise ApiError(f"{path}: sin variable NAICS")
            self._need(variables, [*measures, "YEAR"], path)
            rows = self._query(path, [*measures, naics, "YEAR"], {naics: self._wild(), "YEAR": "*"})
            for r in rows:
                code = clean_code(r.get(naics, ""))
                if code is None:
                    continue
                for m in measures:
                    lab = variables[m]["label"]
                    self._add(
                        series,
                        f"{SOURCE}:{m}:{code}",
                        f"{lab}, NAICS {code}",
                        units_of(m, lab, variables[m].get("predicateType")),
                        f"Census/BDS/NAICS {code[:2]}",
                        r.get("YEAR", ""),
                        r.get(m),
                    )
        self.log.info("bds cargado", extra={"fields": {"series": len(series)}})
        return series


make_adapter = make_adapter_for(BdsAdapter)


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m src.ingest.bds crawl|download|update|estimate`."""
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
