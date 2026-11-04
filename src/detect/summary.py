"""Resumen para el workflow: histograma de años de quiebre de las señales de texto.

Si casi todo cae en un año (p. ej. 2023 en EDGAR) es un artefacto del corpus, no tendencias.
"""

from __future__ import annotations

import json

from src.config import load_config
from src.detect.approval import latest_runs


def main() -> int:
    cfg = load_config()
    print("## Años de quiebre de las señales de texto\n")
    for r in latest_runs(cfg, list(cfg["text_detect"]["grid"])):
        by = json.loads(r["params_json"]).get("break_years")
        if not by:
            continue
        flag = " ⚠ posible artefacto de cobertura del corpus" if by["artifact_suspect"] else ""
        print(f"- {r['family']}: n={by['n']}, {by['by_year']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
