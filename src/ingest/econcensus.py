"""Censo Económico (Census), nivel nacional: tamaño y concentración por NAICS (ADR 0024).

Dos tablas por vintage: `ecnbasic` (FIRM, ESTAB, RCPTOT, EMP) y `ecnsize`:
- concentración (`CONCENFI=*`): porcentaje de los ingresos de las N mayores empresas, con N sacado
  de la etiqueta oficial (`concentration.label_regex`): `econcensus:CR<N>:<NAICS>`;
- distribución por tamaño (`EMPSZFF=*`): empresas por clase de empleo, con la etiqueta de la clase
  en el título: `econcensus:FIRM_SIZE:<NAICS>:<código de clase>`.
Solo se usan las vintages desde `min_vintage` (el esquema anterior es otro). Un valor suprimido
o negativo no es dato. `NAICS*=*` trae todo el árbol en una consulta por vintage y tabla.
"""

from __future__ import annotations

import re
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

SOURCE = "econcensus"
__all__ = ["AGENCY", "SOURCE", "EconCensusAdapter", "main"]


class EconCensusAdapter(NaicsAdapter):
    source = SOURCE

    def _dataset_paths(self) -> list[tuple[str, str, str]]:
        mv = int(self.scfg["min_vintage"])
        return sorted([*self._dataset_list("ecnbasic", mv), *self._dataset_list("ecnsize", mv)])

    def _load(self) -> dict[str, MSeries]:
        measures: list[str] = list(self.scfg["measures"])
        conc, size = self.scfg["concentration"], self.scfg["size"]
        label_re = re.compile(conc["label_regex"], re.IGNORECASE)
        series: dict[str, MSeries] = {}
        for ds_name in ("ecnbasic", "ecnsize"):
            for path, vintage, _ in self._dataset_list(ds_name, int(self.scfg["min_vintage"])):
                variables = self._variables(path)
                naics = self._naics_var(variables)
                if naics is None:
                    raise ApiError(f"{path}: sin variable NAICS")
                wild = {naics: self._wild()}
                if ds_name == "ecnbasic":
                    self._need(variables, measures, path)
                    for r in self._query(path, [*measures, naics], wild):
                        code = clean_code(r.get(naics, ""))
                        for m in measures if code else []:
                            lab = variables[m]["label"]
                            self._add(
                                series,
                                f"{SOURCE}:{m}:{code}",
                                f"{lab}, NAICS {code}",
                                units_of(m, lab, variables[m].get("predicateType")),
                                f"Census/Economic Census/NAICS {code[:2]}",
                                vintage,
                                r.get(m),
                            )
                    continue
                # ecnsize: concentración y tamaño de empresa
                cv, cval = conc["code_var"], conc["value"]
                self._need(variables, [cv, cval, size["code_var"], size["value"]], path)
                for r in self._query(path, [cv, f"{cv}_LABEL", cval, naics], {**wild, cv: "*"}):
                    code = clean_code(r.get(naics, ""))
                    m = label_re.search(r.get(f"{cv}_LABEL", ""))
                    if not code or not m:
                        continue
                    n = m.group(1)
                    self._add(
                        series,
                        f"{SOURCE}:CR{n}:{code}",
                        f"{variables[cval]['label']}, {r[f'{cv}_LABEL']}, NAICS {code}",
                        "Percent",
                        f"Census/Economic Census/NAICS {code[:2]}",
                        vintage,
                        r.get(cval),
                    )
                sv, sval = size["code_var"], size["value"]
                for r in self._query(path, [sv, f"{sv}_LABEL", sval, naics], {**wild, sv: "*"}):
                    code = clean_code(r.get(naics, ""))
                    cls = (r.get(sv) or "").strip()
                    if not code or not cls:
                        continue
                    self._add(
                        series,
                        f"{SOURCE}:FIRM_SIZE:{code}:{cls}",
                        f"{variables[sval]['label']}, {r.get(f'{sv}_LABEL', cls)}, NAICS {code}",
                        units_of(sval, variables[sval]["label"], None),
                        f"Census/Economic Census/NAICS {code[:2]}",
                        vintage,
                        r.get(sval),
                    )
        self.log.info("econcensus cargado", extra={"fields": {"series": len(series)}})
        return series


make_adapter = make_adapter_for(EconCensusAdapter)


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m src.ingest.econcensus crawl|download|update|estimate`."""
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
