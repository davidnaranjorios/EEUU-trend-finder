# ADR 0020 — Padre de las series jóvenes por categoría y título

Fecha: 2026-11-11. Aprobada por David el mismo día. Amplía el ADR 0017 (`relative_growth`); no cambia
la calibración (`phi_margin`, `min_periods`, familia FDR propia, BH/BY) ni ningún principio.

## Problema
Corrida de detectores 42879132598 (run d09cc192): `relative_growth` cargó **2.977** series jóvenes y
**0 tuvieron padre**: `not_tested_by_reason = {sin_naics: 2977}`. Casi todas son de FRED, que no trae
NAICS.

## Por qué `total_encuesta` no se aplicó
`find_parent` empezaba con `if code is None: return None, "sin_naics"`: una serie sin código NAICS
salía antes de llegar a `total_encuesta`, que solo se evaluaba para series CON código. Es decir, el
total de la encuesta (BLS, BEA, Census) nunca podía usarse para las que no tienen industria, que son
la mayoría, y las 2.977 cayeron ahí. (No es que el total no existiera: la condición no lo miraba.)

## Decisión
Nuevo método `titulo_agregado` (`src/detect/parents.py`), el último de la cadena
`naics_prefijo → total_encuesta → titulo_agregado`, y que ya no exige NAICS. Solo metadatos
(título, categoría, unidades, frecuencia, ajuste, fecha de inicio); ninguna palabra del tema.
- **Dónde se busca.** FRED: la categoría oficial de la serie (`category_path`, el árbol que ya
  recorre el crawl) y, si ahí no hay candidata, sus categorías **ancestras**, hasta `max_tree_levels`
  (3) niveles; gana el nivel más cercano con candidatas. BLS, BEA y Census: la misma unidad
  (encuesta, tabla o dataset), sin árbol.
- **Qué candidata sirve.** Misma medida (fuente, frecuencia, unidades, ajuste estacional); al menos
  tan antigua como la joven (`start_date`); y un prefijo común de título de al menos
  `min_common_tokens` (2) palabras, sin palabras de función («of», «in», «the»…).
  En la **misma categoría** el título del padre debe estar contenido en el de la joven (la
  especializa); en una **categoría ancestra** basta el prefijo común y que el padre no sea más
  específico. Un título idéntico (duplicado) no cuenta.
- **Cuál se elige.** El prefijo más largo y, a igual prefijo, el título más corto (el más general). Un
  empate real entre títulos distintos es `padre_ambiguo` y **no se elige**. Si un método NAICS ya
  dio ambigüedad, no se salta a este.
- **Linaje:** `how = titulo_agregado` y `tree_level` (0 = misma categoría) en el JSON de linaje.
- Motivo de no probar: `sin_padre` (antes `sin_naics` para las series sin código) o `padre_ambiguo`.
- **Config:** `detect.relative_growth.parent.min_common_tokens` y `max_tree_levels`.

## Qué NO se hizo
- Los hermanos «total» de una categoría cuyo título no contiene al de la joven (p. ej. «All Employees:
  Total Nonfarm» frente a «All Employees: Manufacturing») solo se hallan si el total está en una
  categoría ancestra con prefijo común. Detectarlos en el mismo nivel exigiría una palabra («Total»,
  «All») o el nivel de la serie (un dato, no metadato): ninguna de las dos se añadió sin tu
  aprobación (principio 1 y «solo metadatos»).
- La elección del padre no usa ninguna correlación ni dato de la serie: así no se sesga la prueba
  (elegir el padre que más se parece a la joven haría que casi nunca difiriera).

## Cuántas esperar (límite)
Con datos sintéticos (14 jóvenes en una categoría hija y su agregado en la categoría madre): 14/14
con padre por `titulo_agregado`, `tree_level` 1, ninguna `sin_padre`. **Los conteos reales no se pueden
dar sin los metadatos de la corrida** (títulos y rutas de categoría de las 2.977): salen en
`report["relative_growth"].parent_by_method` y `not_tested_by_reason` en la próxima corrida. Lo
que sí se espera: casi todas las que antes eran `sin_naics` pasan a tener `titulo_agregado` o
`sin_padre`; las que quedan sin padre son las que no tienen en su categoría (ni en las ancestras) una
serie más antigua, de la misma medida y con un título prefijo del suyo.

## Riesgos
- Un padre mal elegido produce una señal espuria o la tapa. Por eso es 🟡 (confianza por defecto del
  detector), se registra el método y el nivel, y el FDR sigue calibrado igual (más parejas = familia más
  grande, nada más).
- Muchas jóvenes comparten un mismo padre (el agregado de la categoría): sus diferencias comparten el
  ruido del padre y están correlacionadas entre sí. Los índices de bloques compartidos de la familia
  conservan esa dependencia, que es justo para lo que están (ADR 0007).
- La potencia sigue siendo baja con 3 años (ADR 0017): las pruebas sintéticas usan un agregado suave y
  una brecha de decenas de desviaciones; con `phi_margin` 5 el estimado de φ llega al tope con 47
  periodos, y una brecha de unas pocas desviaciones no sale.
