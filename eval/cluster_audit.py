"""Inventario interno de una corrida local; nunca publica ni modifica datos de entrada.

Uso: python eval/cluster_audit.py --rank-dir data/processed/rank --run-id ID --out work/audit-ID
No reconstruye gates históricos que el ranking no persistió. No usa red ni embeddings.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

TABLES = (
    "clusters",
    "rank_candidates",
    "rank_evidence",
    "rank_origin",
    "naics_links",
    "series_term_links",
    "rank_runs",
)
LIMITATION = (
    "No se persistieron todos los pares rechazados: la ausencia de un vínculo no prueba "
    "rechazo por similitud, solapamiento o co-movimiento. validated=false no identifica "
    "por sí solo cuál de esas condiciones faltó. Sin metadatos de la corrida no se "
    "reconstruyen sus umbrales desde la configuración actual."
)
RUBRIC = """# Etiquetado humano — auditoría interna

Este material contiene identificadores y posiblemente evidencia con copyright; no es un
reporte publicable. No contiene valores de series. No copiarlo a data/site ni reports.

Etiquetar cada dimensión como correcto / dudoso / incorrecto / no_evaluable y explicar el motivo:

- coherencia: ¿los miembros describen un fenómeno económico común?
- vinculo_texto_serie: ¿el término aporta evidencia pertinente, y no solo similitud verbal?
- independencia: ¿los orígenes son independientes, sin contar republicaciones dos veces?
- naics: ¿el código y el método de anclaje corresponden a la actividad descrita?

Usar no_evaluable cuando falte evidencia. El agente no preetiqueta. Los umbrales no cambian.
La muestra es estratificada y sirve para diagnóstico, no para estimar precisión poblacional
sin ponderaciones y un diseño de evaluación apropiado. Consultar inventario.json para ver
los miembros, evidencia, limitaciones y reglas registradas.
"""


def _json(value: Any, default: Any) -> Any:
    return json.loads(value) if value is not None else default


def load_local(rank_dir: Path, run_id: str) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    """Lee solo Parquet locales de la corrida; registra ausencias y hashes de entrada."""
    data, manifest = {}, {"missing_tables": [], "files": []}
    with duckdb.connect() as con:
        for table in TABLES:
            files = sorted((rank_dir / table / "rank").glob("??/*.parquet"))
            if not files:
                data[table] = []
                manifest["missing_tables"].append(table)
                continue
            rows = (
                con.execute(
                    "SELECT * FROM read_parquet(?, union_by_name=true) WHERE run_id = ?",
                    [[str(p) for p in files], run_id],
                )
                .to_arrow_table()
                .to_pylist()
            )
            data[table] = rows
            for p in files:
                manifest["files"].append(
                    {
                        "path": str(p.relative_to(rank_dir)),
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    }
                )
    return data, manifest


def inventory(data: dict[str, list[dict]], run_id: str) -> list[dict[str, Any]]:
    """Explica estados persistidos; no vuelve a decidir ni altera el ranking."""
    groups, candidates, evidence = defaultdict(list), {}, defaultdict(list)
    for r in data.get("clusters", []):
        groups[r["cluster_id"]].append(r)
    for r in data.get("rank_candidates", []):
        key = r["candidate_id"]
        if key in candidates:
            raise ValueError(f"candidato duplicado: {key}")
        candidates[key] = r
    for r in data.get("rank_evidence", []):
        evidence[r["candidate_id"]].append(r)
    meta = {(r["source"], r["series_id"]): r for r in data.get("series_meta", [])}
    origins, naics = {}, defaultdict(list)
    for r in data.get("rank_origin", []):
        origins[(r["source"], r["object_id"])] = r
    for r in data.get("naics_links", []):
        naics[(r["source"], r["object_id"])].append(r)
    run = next(iter(data.get("rank_runs", [])), {})
    params = _json(run.get("params_json"), {}).get("rank", {})
    minimum, cap = params.get("min_sources"), params.get("max_candidates")
    result = []
    for cid in sorted(set(groups) | set(candidates) | set(evidence)):
        c, ev = candidates.get(cid, {}), evidence[cid]
        keys = {(r["source"], r["object_id"]) for r in groups[cid] + ev}
        members = []
        for key in sorted(keys):
            origin = origins.get(key)
            metadata = meta.get(key, {})
            own_ev = [e for e in ev if (e["source"], e["object_id"]) == key]
            members.append(
                {
                    "source": key[0],
                    "object_id": key[1],
                    "title": metadata.get("title"),
                    "units": metadata.get("units"),
                    "metadata_copyrighted": metadata.get("copyrighted"),
                    "metadata_status": "available" if metadata else "not_available",
                    "in_cluster": any((r["source"], r["object_id"]) == key for r in groups[cid]),
                    "roles": sorted(
                        {r["role"] for r in groups[cid] if (r["source"], r["object_id"]) == key}
                    ),
                    "origin": _json(origin.get("origin_json"), []) if origin else None,
                    "origin_chain": _json(origin.get("chain_json"), []) if origin else None,
                    "origin_rule": origin.get("rule_id") if origin else None,
                    "possible_same_origin": _json(origin.get("possible_same_origin_json"), [])
                    if origin
                    else None,
                    "evidence_agencies": [_json(e.get("agencies_json"), []) for e in own_ev],
                    "public": [e.get("public") for e in own_ev],
                    "naics": sorted(naics[key], key=lambda n: (n["naics"], n["metodo"])),
                }
            )
        links = [
            r for r in data.get("series_term_links", []) if (r["source"], r["series_id"]) in keys
        ]
        links = [
            {
                **r,
                "validation_state": (
                    "no_registrado"
                    if r.get("validated") is None
                    else "validado"
                    if r["validated"]
                    else "no_validado"
                ),
            }
            for r in links
        ]
        rules = []
        status = c.get("status")
        if not c:
            rules.append({"rule": "sin_candidato_persistido", "certainty": "desconocido"})
        elif status == "una_fuente":
            rules.append(
                {
                    "rule": "triangulacion",
                    "condition": "n_sources < min_sources",
                    "observed": c.get("n_sources"),
                    "threshold": minimum,
                    "source": "src/rank/triangulate.py:assess",
                    "certainty": "estado_persistido",
                }
            )
        elif status == "fuera_del_tope":
            rules.append(
                {
                    "rule": "tope",
                    "condition": "rank > max_candidates",
                    "observed": c.get("rank"),
                    "threshold": cap,
                    "source": "src/rank/triangulate.py:score",
                    "certainty": "estado_persistido",
                }
            )
        elif status == "candidato" and c.get("published") is False:
            rules.append(
                {
                    "rule": "fuentes_publicas",
                    "condition": "n_sources_public < min_sources",
                    "observed": c.get("n_sources_public"),
                    "threshold": minimum,
                    "source": "src/rank/triangulate.py:assess",
                    "certainty": "estado_persistido",
                }
            )
        elif status != "candidato" or c.get("published") is not True:
            rules.append({"rule": "estado_no_interpretable", "certainty": "desconocido"})
        result.append(
            {
                "run_id": run_id,
                "cluster_id": cid,
                "kind": c.get("kind"),
                "membership_recorded": bool(groups[cid]),
                "status": status,
                "published": c.get("published"),
                "rank": c.get("rank"),
                "n_sources": c.get("n_sources"),
                "n_sources_public": c.get("n_sources_public"),
                "independent_groups": _json(c.get("sources_json"), None),
                "members": members,
                "publishers": sorted({m["source"] for m in members}),
                "evidence": sorted(ev, key=lambda e: (e["source"], e["object_id"])),
                "links": sorted(links, key=lambda r: (r["source"], r["series_id"], r["term"])),
                "exclusion_rules": rules,
                "naics_covered_objects": sum(bool(m["naics"]) for m in members),
                "limitations": [LIMITATION],
            }
        )
    return result


def naics_summary(data: dict[str, list[dict]], *, links_available: bool = True) -> dict[str, Any]:
    """Cuenta objetos del run completo; denominadores solo del resumen persistido."""
    run = next(iter(data.get("rank_runs", [])), {})
    params = _json(run.get("params_json"), {})
    stored = params.get("summary", {}).get("naics_coverage", {})
    objects, methods = defaultdict(set), defaultdict(lambda: defaultdict(set))
    for link in data.get("naics_links", []):
        source, object_id = link["source"], link["object_id"]
        objects[source].add(object_id)
        methods[source][link["metodo"]].add(object_id)
    sources = {}
    for source in sorted(set(stored) | set(objects)):
        recorded = stored.get(source)
        sources[source] = {
            "stored_signal_coverage": recorded,
            "signal_denominator": recorded.get("n") if recorded is not None else None,
            "mapped_objects_in_links": len(objects[source]) if links_available else None,
            "unique_objects_by_method": {
                method: len(ids) for method, ids in sorted(methods[source].items())
            }
            if links_available
            else None,
        }
    return {
        "scope": "all_naics_links_in_selected_rank_run_not_only_inventory_members",
        "links_available": links_available,
        "stored_coverage_available": "naics_coverage" in params.get("summary", {}),
        "sources": sources,
        "limitations": [
            "Un objeto puede tener varios métodos/códigos: no sumar conteos por método. "
            "Son objetos únicos, no pesos ni suma de filas.",
            "El denominador y la cobertura de señales son los persistidos en rank_runs; "
            "si faltan, permanecen desconocidos. Los links pueden incluir un universo distinto; "
            "no se divide su conteo por el denominador de señales.",
        ],
    }


def sample(rows: list[dict], seed: str = "cluster-audit-v1", size: int = 20) -> list[dict]:
    """Hasta 20; reserva un publicado y recorre estratos de estado/cobertura con orden hash."""
    if not 1 <= size <= 20:
        raise ValueError("size debe estar en 1..20")

    def order(r: dict) -> str:
        return hashlib.sha256(f"{seed}:{r['cluster_id']}".encode()).hexdigest()

    selected = sorted((r for r in rows if r["published"]), key=order)[:1]
    chosen = {r["cluster_id"] for r in selected}
    strata = defaultdict(list)
    for r in rows:
        if r["cluster_id"] not in chosen:
            strata[(r["status"] or "sin_estado", bool(r["naics_covered_objects"]))].append(r)
    strata = {k: sorted(v, key=order) for k, v in sorted(strata.items())}
    while len(selected) < min(size, len(rows)):
        for group in strata.values():
            if group and len(selected) < size:
                selected.append(group.pop(0))
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--meta-dir", type=Path, help="series_meta local opcional, solo metadatos")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", default="cluster-audit-v1")
    args = parser.parse_args(argv)
    root, out = args.rank_dir.resolve(), args.out.resolve()
    if out == root or out.is_relative_to(root) or root.is_relative_to(out):
        parser.error("salida y entrada deben estar en árboles separados")
    if args.meta_dir:
        meta_root = args.meta_dir.resolve()
        if out == meta_root or out.is_relative_to(meta_root) or meta_root.is_relative_to(out):
            parser.error("salida y metadatos deben estar en árboles separados")
    data, manifest = load_local(root, args.run_id)
    manifest["metadata"] = "not_requested"
    if args.meta_dir:
        keys = sorted(
            {
                (r["source"], r["object_id"])
                for name in ("clusters", "rank_evidence")
                for r in data[name]
            }
        )
        files = sorted(args.meta_dir.glob("*/??/*.parquet"))
        data["series_meta"] = []
        manifest["metadata"] = "available" if files else "missing_files"
        if files and keys:
            with duckdb.connect() as con:
                data["series_meta"] = (
                    con.execute(
                        "SELECT * FROM read_parquet(?, union_by_name=true) "
                        "WHERE (source, series_id) IN (SELECT unnest(?), unnest(?))",
                        [[str(p) for p in files], [k[0] for k in keys], [k[1] for k in keys]],
                    )
                    .to_arrow_table()
                    .to_pylist()
                )
        manifest["metadata_files"] = [
            {
                "path": str(p.relative_to(args.meta_dir)),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in files
        ]
    rows = inventory(data, args.run_id)
    if not rows:
        parser.error(
            "no hay clusters ni candidatos para esa corrida; no se generan resultados ficticios"
        )
    selected = sample(rows, args.seed)
    manifest.update(
        {
            "run_id": args.run_id,
            "audience": "internal_not_for_publication",
            "seed": args.seed,
            "n_inventory_records": len(rows),
            "n_persisted_clusters": len({r["cluster_id"] for r in data["clusters"]}),
            "sample_size": len(selected),
            "sample_requested": 20,
            "counts_by_status": dict(Counter(r["status"] or "sin_estado" for r in rows)),
            "naics_coverage": naics_summary(
                data, links_available="naics_links" not in manifest["missing_tables"]
            ),
            "sampling": (
                "un publicado si existe; round-robin por estado y cobertura NAICS; orden SHA256"
            ),
            "limitations": [
                LIMITATION,
                "La ausencia de filas NAICS/origen no demuestra ausencia económica.",
                "Sin título/metadatos, etiquetar coherencia puede no ser evaluable. "
                "units es desconocido si series_meta no lo conserva.",
            ],
        }
    )
    out.mkdir(parents=True, exist_ok=False)  # nunca sobrescribir etiquetas del propietario
    for name, payload in (("inventario", rows), ("muestra", selected), ("manifest", manifest)):
        (out / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    (out / "rubrica.md").write_text(RUBRIC, encoding="utf-8")
    with (out / "etiquetas.csv").open("w", encoding="utf-8", newline="") as f:
        fields = [
            "run_id",
            "cluster_id",
            "coherencia",
            "vinculo_texto_serie",
            "independencia",
            "naics",
            "motivo",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({"run_id": args.run_id, "cluster_id": r["cluster_id"]} for r in selected)
    print(json.dumps({"clusters": len(rows), "sample": len(selected), "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
