"""County Business Patterns (Census), nivel nacional: ESTAB, EMP y PAYANN por NAICS (ADR 0024).

Una vintage por año (`api.census.gov/data/{año}/cbp`). La variable NAICS cambia por vintage
(NAICS1997/2002/2012/2017/2022) y se descubre en `variables.json`; las vintages sin variable NAICS
(antes de 1998, con SIC) no se usan y se cuentan. Un código de NAICS se une entre vintages tal cual
lo publica la API: los cambios de revisión del NAICS no se armonizan (ver el ADR).
`series_id = cbp:<VARIABLE>:<NAICS>`; fecha = 1 de enero del año de referencia.
"""

from __future__ import annotations

import sys

from src.ingest import common
from src.ingest.naics_base import (
    AGENCY,
    MSeries,
    NaicsAdapter,
    clean_code,
    make_adapter_for,
    units_of,
)

SOURCE = "cbp"
__all__ = ["AGENCY", "SOURCE", "CbpAdapter", "main"]


class CbpAdapter(NaicsAdapter):
    source = SOURCE

    def _dataset_paths(self) -> list[tuple[str, str, str]]:
        return self._dataset_list("cbp")

    def _load(self) -> dict[str, MSeries]:
        measures: list[str] = list(self.scfg["measures"])
        series: dict[str, MSeries] = {}
        skipped = 0
        for path, vintage, _ in self._dataset_paths():
            variables = self._variables(path)
            naics = self._naics_var(variables)
            if naics is None:
                skipped += 1  # vintage con SIC, sin NAICS
                continue
            self._need(variables, measures, path)
            rows = self._query(path, [*measures, naics], {naics: self._wild()})
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
                        f"Census/CBP/NAICS {code[:2]}",
                        vintage,
                        r.get(m),
                    )
        self.log.info(
            "cbp cargado", extra={"fields": {"series": len(series), "sin_naics": skipped}}
        )
        return series


make_adapter = make_adapter_for(CbpAdapter)


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m src.ingest.cbp crawl|download|update|estimate`."""
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
