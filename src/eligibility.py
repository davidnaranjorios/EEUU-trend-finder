"""Embudo de elegibilidad: reglas neutrales sobre metadatos, con linaje por serie.

Cada serie del catálogo recibe exactamente un grupo y la regla que lo decidió; nada se descarta
sin dejar el motivo en la tabla `eligibility` (ADR 0002). Las reglas solo miran metadatos
objetivos (frecuencia, fechas, ajuste estacional, unidades, tipo geográfico), nunca el tema.

Uso: `uv run python -m src.eligibility run | funnel`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa

from src.config import load_config
from src.ingest.fred import (
    FRED_CATALOG,
    FREQ_MAP,
    PERIOD_DAYS,
    SERIES_ATTRS,
    SOURCE,
    Paths,
    _resolve,
    paths_from_config,
)
from src.ingest.store import read_table, upsert
from src.log import get_logger, run
from src.schemas import ELIGIBILITY

# Tags del grupo geot que marcan nivel país y nivel supranacional. El resto de tags geot
# (estado, condado, área metropolitana...) es subnacional.
NATION_TAG = "nation"
TRANSNATIONAL_TAG = "transnational"

# Bytes por fila en Parquet, medido sobre 2,1 M filas descargadas (31 MB).
PARQUET_BYTES_PER_ROW = 15
# Bytes por observación en el JSON de v2, medido en una página de 500.000 obs (21,5 MB).
V2_JSON_BYTES_PER_OBS = 43
# Ritmos medidos en vivo (2026-10-20/21).
V1_SERIES_PER_MIN = 84
V2_SECONDS_PER_PAGE = 3.4  # 2,7 s de respuesta + espaciado del rate limit


def classify(
    catalog: Iterable[dict[str, Any]],
    attrs: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    today: date,
) -> list[dict[str, Any]]:
    """Embudo de FRED: traduce fred_catalog + tags geográficos al catálogo neutral."""
    rows = []
    for r in catalog:
        geo_level, geo_detail = geo_level_of(attrs.get(r["series_id"]))
        rows.append(
            {
                **r,
                "frequency": FREQ_MAP.get(r["frequency_short"] or ""),
                "frequency_raw": r["frequency_short"],
                "geo_level": geo_level,
                "geo_detail": geo_detail,
            }
        )
    return classify_rows(rows, cfg, today)


def classify_rows(
    catalog: Iterable[dict[str, Any]], cfg: dict[str, Any], today: date
) -> list[dict[str, Any]]:
    """Una decisión por serie del catálogo neutral (todas las fuentes).

    Cada fila trae `series_id, title, frequency` (código del esquema o None), `frequency_raw`,
    `units, seasonal_adjustment, observation_start, observation_end, discontinued,
    copyrighted, geo_level, geo_detail`. Devuelve `group_name`, `rule`, `detail`, `geo_level`,
    `frequency` y `copyrighted` por serie, ordenadas por id.
    """
    el = cfg["eligibility"]
    max_stale = el["max_staleness_days"]
    min_days = el["min_history_years"] * 365.25
    out: dict[str, dict[str, Any]] = {}
    active: list[dict[str, Any]] = []

    for r in catalog:
        decision = row_rule(r, max_stale, today)
        if decision is not None:
            out[r["series_id"]] = decision
        else:
            active.append({**r, "_freq": r["frequency"], "_base": _base(r)})

    # Regla 4: una versión por variable (mismo título normalizado).
    rank = _variant_rank(el["variants"], min_days)
    by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in active:
        by_title[normalize_title(r["title"])].append(r)
    keepers = []
    for members in by_title.values():
        members.sort(key=rank)
        keeper = members[0]
        keepers.append(keeper)
        for other in members[1:]:
            out[other["series_id"]] = {
                **other["_base"],
                "group_name": "excluida",
                "rule": "variante",
                "detail": f"variante_de={keeper['series_id']}",
            }

    # Reglas 5 y 6: nivel geográfico y grupo de series nuevas.
    for r in keepers:
        geo_level, geo_detail = r["geo_level"] or "sin_geografia", r["geo_detail"]
        history = (r["observation_end"] - r["observation_start"]).days
        decision = {**r["_base"], "geo_level": geo_level, "detail": geo_detail}
        if geo_level == "sin_geografia":
            decision.update(group_name="sin_geografia", rule="geografia")
        elif history < min_days:
            decision.update(group_name="nuevas", rule="historia_corta")
            decision["detail"] = f"anios={history / 365.25:.1f};{geo_detail}"
        else:
            decision.update(group_name=geo_level, rule="geografia")
        out[r["series_id"]] = decision
    return [out[sid] for sid in sorted(out)]


def _base(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "series_id": r["series_id"],
        "frequency": r["frequency"],  # NULL si no está soportada; el código va en `detail`
        "copyrighted": bool(r["copyrighted"]),
        "geo_level": None,
        "detail": None,
    }


def row_rule(r: dict[str, Any], max_stale: dict[str, int], today: date) -> dict[str, Any] | None:
    """Reglas 1–3 (descontinuada, frecuencia, sin fechas, inactiva): solo miran la fila.

    Devuelve la decisión de exclusión, o None si la serie sigue en el embudo.
    """
    freq = r["frequency"]
    base = _base(r)
    end = r["observation_end"]
    if r["discontinued"]:
        return {**base, "group_name": "excluida", "rule": "descontinuada"}
    if freq is None:
        return {
            **base,
            "group_name": "excluida",
            "rule": "frecuencia_no_soportada",
            "detail": r["frequency_raw"],
        }
    if r["observation_start"] is None or end is None:
        return {**base, "group_name": "excluida", "rule": "sin_fechas"}
    if (today - end).days > max_stale[freq]:
        return {
            **base,
            "group_name": "excluida",
            "rule": "inactiva",
            "detail": f"ultimo_dato={end.isoformat()}",
        }
    return None


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").casefold()).strip()


def geo_level_of(attrs: dict[str, Any] | None) -> tuple[str, str | None]:
    """nacional / regional / internacional / sin_geografia, según los tags geot y `usa`."""
    if not attrs or not attrs["geo_types"]:
        return "sin_geografia", ("usa" if attrs and attrs["is_usa"] else None)
    types = set(attrs["geo_types"])
    detail = ",".join(sorted(types))
    if not attrs["is_usa"] or (TRANSNATIONAL_TAG in types and NATION_TAG not in types):
        return "internacional", detail
    if NATION_TAG in types:
        return "nacional", detail
    return "regional", detail


def _variant_rank(variants: dict[str, Any], min_days: float) -> Any:
    sa_order = {code: i for i, code in enumerate(variants["seasonal_preference"])}
    freq_order = {f: i for i, f in enumerate(variants["frequency_preference"])}
    transformed = re.compile(variants["transformed_units_regex"])

    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        history = (r["observation_end"] - r["observation_start"]).days
        return (
            sa_order.get(r["seasonal_adjustment"] or "", len(sa_order)),
            bool(transformed.search(r["units"] or "")),
            history < min_days,
            freq_order[r["_freq"]],
            -history,
            r["series_id"],
        )

    return key


# --- embudo y tamaño de descarga ------------------------------------------------------------


def funnel(
    decisions: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    attrs: dict[str, dict[str, Any]],
    v2_page_limit: int,
) -> dict[str, Any]:
    """Conteos por regla y por grupo, y tamaño estimado de descarga de cada grupo."""
    by_rule = Counter(d["rule"] for d in decisions if d["group_name"] == "excluida")
    by_group = Counter(d["group_name"] for d in decisions)
    nuevas_geo = Counter(d["geo_level"] for d in decisions if d["group_name"] == "nuevas")
    cat = {r["series_id"]: r for r in catalog}

    # Observaciones por release (todas las series del release: v2 devuelve el release entero).
    release_obs: Counter[int] = Counter()
    for sid, a in attrs.items():
        n = _est_obs(cat.get(sid))
        for rid in a["release_ids"] or []:
            release_obs[rid] += n

    sizes = {}
    for group in ("nacional", "regional", "internacional", "nuevas", "sin_geografia"):
        members = [d["series_id"] for d in decisions if d["group_name"] == group]
        obs = sum(_est_obs(cat[s]) for s in members)
        rels = {rid for s in members for rid in (attrs.get(s, {}).get("release_ids") or [])}
        pages = sum(max(1, math.ceil(release_obs[r] / v2_page_limit)) for r in rels)
        sizes[group] = {
            "series": len(members),
            "copyrighted": sum(cat[s]["copyrighted"] for s in members),
            "obs_estimadas": obs,
            "parquet_mb": round(obs * PARQUET_BYTES_PER_ROW / 1e6, 1),
            "v1_horas": round(len(members) / V1_SERIES_PER_MIN / 60, 1),
            "v2_releases": len(rels),
            "v2_paginas": pages,
            "v2_minutos": round(pages * V2_SECONDS_PER_PAGE / 60, 1),
            "v2_transferencia_gb": round(
                sum(release_obs[r] for r in rels) * V2_JSON_BYTES_PER_OBS / 1e9, 2
            ),
        }
    return {
        "total": len(decisions),
        "excluidas_por_regla": dict(by_rule.most_common()),
        "grupos": dict(by_group.most_common()),
        "nuevas_por_nivel": dict(nuevas_geo.most_common()),
        "tamano_por_grupo": sizes,
    }


def _est_obs(r: dict[str, Any] | None) -> int:
    if not r or not r["observation_start"] or not r["observation_end"]:
        return 0
    freq = FREQ_MAP.get(r["frequency_short"] or "")
    if freq is None:
        return 0
    return int((r["observation_end"] - r["observation_start"]).days / PERIOD_DAYS[freq]) + 1


# --- E/S ----------------------------------------------------------------------------------------


def load_inputs(paths: Paths) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog = read_table(FRED_CATALOG, paths.catalog, source=SOURCE).to_pylist()
    attrs = {
        r["series_id"]: r
        for r in read_table(SERIES_ATTRS, paths.series_attrs, source=SOURCE).to_pylist()
    }
    return catalog, attrs


def write_lineage(
    decisions: list[dict[str, Any]],
    run_id: str,
    decided_at: datetime,
    paths: Paths,
    n_buckets: int,
) -> int:
    rows = [{**d, "run_id": run_id, "source": SOURCE, "decided_at": decided_at} for d in decisions]
    res = upsert(
        pa.Table.from_pylist(rows, schema=ELIGIBILITY.schema),
        ELIGIBILITY,
        paths.eligibility,
        source=SOURCE,
        n_buckets=n_buckets,
    )
    return res.inserted


def latest_decisions(paths: Paths, run_id: str | None = None) -> list[dict[str, Any]]:
    """Decisiones de la última corrida del embudo (o de `run_id`)."""
    table = read_table(ELIGIBILITY, paths.eligibility, source=SOURCE)
    if table.num_rows == 0:
        raise RuntimeError("no hay corridas del embudo; corre `python -m src.eligibility run`")
    rows = table.to_pylist()
    if run_id is None:
        run_id = max(rows, key=lambda r: (r["decided_at"], r["run_id"]))["run_id"]
    return [r for r in rows if r["run_id"] == run_id]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.eligibility")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="aplica las reglas, registra el linaje e imprime el embudo")
    show = sub.add_parser("funnel", help="embudo de la última corrida (o de --run-id)")
    show.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    get_logger("eligibility", cfg["logging"]["level"])
    paths = paths_from_config(cfg)
    catalog, attrs = load_inputs(paths)
    if args.cmd == "run":
        with run([SOURCE], _resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage("eligibility"):
                decisions = classify(catalog, attrs, cfg, datetime.now(UTC).date())
                now = datetime.now(UTC).replace(microsecond=0)
                inserted = write_lineage(
                    decisions, record.run_id, now, paths, cfg["fred"]["n_buckets"]
                )
                result = funnel(decisions, catalog, attrs, cfg["download"]["v2_page_limit"])
            record.n_series[SOURCE] = len(decisions)
            record.details = {"command": "eligibility", "funnel": result, "rows": inserted}
        result = {"run_id": record.run_id, **result}
    else:
        decisions = latest_decisions(paths, args.run_id)
        result = {
            "run_id": decisions[0]["run_id"],
            **funnel(decisions, catalog, attrs, cfg["download"]["v2_page_limit"]),
        }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
