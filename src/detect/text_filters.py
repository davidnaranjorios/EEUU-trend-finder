"""Filtros y correcciones del detector de términos emergentes (revisión 2a02c981).

Funciones puras, sin listas de temas: la única lista que se lee es `text_detect.boilerplate`
(frases de boilerplate aprobadas por David; solo se pasa como argumento desde la configuración).

- `is_boilerplate` / `noun_like` / `specific_mask`: filtro de especificidad.
- `drift_adjust` / `drift_adjust_by_group`: corrige la deriva del corpus con la mediana de términos
  de prevalencia parecida (por sección).
- `emerging_stat_break`: T con el punto de quiebre estimado (máximo sobre el largo del segmento).
"""

from __future__ import annotations

import numpy as np

# Sufijos de token que no son sustantivos (adverbios, participios/gerundios sueltos al inicio no
# se tratan: "operating costs" es un sintagma nominal). Solo morfología: sin modelo ni licencia.
_NON_NOUN_SUFFIXES = ("ly",)


def tokens(term: str) -> list[str]:
    return term.split()


def is_boilerplate(term: str, phrases: list[str]) -> bool:
    """El n-grama contiene como subsecuencia contigua de tokens alguna frase de boilerplate."""
    t = tokens(term)
    for phrase in phrases:
        p = tokens(phrase)
        n = len(p)
        if n and any(t[i : i + n] == p for i in range(len(t) - n + 1)):
            return True
    return False


def noun_like(token: str, function_words: set[str]) -> bool:
    """Heurística liviana de sustantivo: no es palabra funcional detectada en los datos, tiene al
    menos 3 letras, no es un número y no termina en sufijo de adverbio."""
    if token in function_words or len(token) < 3 or not any(c.isalpha() for c in token):
        return False
    return not token.endswith(_NON_NOUN_SUFFIXES)


def has_noun(term: str, function_words: set[str]) -> bool:
    return any(noun_like(tok, function_words) for tok in tokens(term))


def specific_mask(
    terms: list[str],
    mean_share: np.ndarray,
    function_words: set[str],
    boilerplate: list[str],
    unigram_max_share: float,
) -> np.ndarray:
    """True = el término pasa el filtro de especificidad.

    Se excluyen: boilerplate; n-gramas sin ningún token con forma de sustantivo; y unigramas con
    prevalencia media alta (`>= unigram_max_share`), que son vocabulario general del corpus.
    """
    ok = np.ones(len(terms), dtype=bool)
    for i, term in enumerate(terms):
        if is_boilerplate(term, boilerplate) or not has_noun(term, function_words):
            ok[i] = False
        elif len(tokens(term)) == 1 and mean_share[i] >= unigram_max_share:
            ok[i] = False
    return ok


def drift_adjust(
    x: np.ndarray, baseline_cols: slice, n_bins: int, min_bin: int
) -> tuple[np.ndarray, np.ndarray]:
    """Resta a cada término la mediana de cambio de los términos con prevalencia base parecida.

    `x` (K, G) logit de la proporción. El nivel base de cada término es su media en
    `baseline_cols`; los términos se agrupan en `n_bins` cuantiles de ese nivel (al menos
    `min_bin` por grupo) y la deriva del grupo en cada periodo es la mediana de
    (x − nivel base). Devuelve (x corregido, deriva por término (K, G)).

    La mediana es robusta a que una minoría de términos sí emerja. El estadístico y el nulo se
    calculan los dos sobre la serie corregida: una deriva global desaparece de ambos.
    """
    k = x.shape[0]
    drift = np.zeros_like(x)
    if k == 0:
        return x, drift
    level = x[:, baseline_cols].mean(axis=1)
    n_bins = max(1, min(n_bins, k // max(min_bin, 1)))
    order = np.argsort(level, kind="stable")
    for chunk in np.array_split(order, n_bins):
        rel = x[chunk] - level[chunk, None]
        drift[chunk] = np.median(rel, axis=0)
    return x - drift, drift


def drift_adjust_by_group(
    x: np.ndarray, baseline_cols: slice, n_bins: int, min_bin: int, groups: list[str]
) -> tuple[np.ndarray, np.ndarray]:
    """`drift_adjust` dentro de cada grupo (la sección del documento), nunca entre grupos.

    Cada sección tiene su propio denominador (documentos de ese tipo por periodo). Una serie
    censurada en el piso (conteo imputado constante) se mueve solo con ese denominador; si los
    cuantiles mezclan secciones, la mediana de otra sección con un denominador distinto se le
    resta como "deriva" y la serie constante parece crecer (medido en el Federal Register: 88
    términos de `rule` corregidos con la mediana de `notice`, ADR 0021). Con una sola sección el
    resultado es idéntico a `drift_adjust`."""
    drift = np.zeros_like(x)
    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    for rows in by_group.values():
        idx = np.array(rows)
        _, d = drift_adjust(x[idx], baseline_cols, n_bins, min_bin)
        drift[idx] = d
    return x - drift, drift


def emerging_stat_break(
    x: np.ndarray, window: int, persistence: int, recent: int
) -> tuple[np.ndarray, np.ndarray]:
    """(T, L): quiebre estimado y estadístico en ese quiebre.

    L, el largo del segmento elevado al final (en [persistencia, recent]), es el que mejor ajusta
    un escalón de dos niveles —la media de los `window` periodos previos y la media de los últimos
    L— por mínimos cuadrados. T = min(últimos `persistence`) − media(previos al segmento): solo es
    grande si todos los últimos `persistence` periodos están elevados (un salto aislado no cuenta).

    El nulo calcula exactamente lo mismo, así que la selección del quiebre ya está en el p.
    """
    last = x[..., -persistence:].min(axis=-1)
    best_sse = np.full(last.shape, np.inf)
    t = np.zeros(last.shape)
    arg = np.full(last.shape, persistence, dtype=int)
    for length in range(persistence, recent + 1):
        prior = x[..., -(length + window) : -length]
        seg = x[..., -length:]
        sse = ((prior - prior.mean(axis=-1, keepdims=True)) ** 2).sum(axis=-1) + (
            (seg - seg.mean(axis=-1, keepdims=True)) ** 2
        ).sum(axis=-1)
        better = sse < best_sse
        best_sse = np.where(better, sse, best_sse)
        t = np.where(better, last - prior.mean(axis=-1), t)
        arg = np.where(better, length, arg)
    return t, arg
