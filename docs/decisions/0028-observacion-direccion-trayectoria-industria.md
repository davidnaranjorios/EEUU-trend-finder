# ADR 0028 — Lista «en observación»: dirección, trayectoria y secciones por industria

Fecha: 2026-11-17 (aprobado por David). Corrida de referencia `23b4b0615d5b4485af21c9a3a2744033`.

## Decisiones
1. **Dirección.** Si todas las series de cantidad (no poblacional) de un fenómeno son `frena_caida` o
   tienen efecto negativo, el fenómeno no puede ser «sube» por series de valor: queda «mixto»
   (`phenomena.rise_only_by_value`, aplicada por cluster y por fenómeno). Caso: `p99e0041f95f7`.
2. **Trayectoria** (`src/rank/trajectory.py`). Etiquetas simétricas para caídas, que solo rellenan lo que
   las de subida no cubren (las etiquetas existentes no cambian): `caida_sobre_valle`, `gira_a_la_baja`,
   `acelera_caida`, `frena_subida`. Etiqueta del fenómeno `no_confirmada` cuando ninguna serie cae en una
   etiqueta: marca visible con una línea de explicación, no filtra.
3. **Secciones.** En el reporte la lista se separa en «Con industria asignada» y «Sin industria (sistema
   financiero u otros)» según `naics_json` vacío. No se borra nada; no hay lista de temas.

No cambia la calibración de los detectores ni el control de falsos positivos.

## Efecto sobre la corrida de referencia (32 fenómenos en observación)
Etiquetas de trayectoria, antes → después (series recalculadas con los niveles de R2):

| dirección | etiqueta | antes | después |
|---|---|---|---|
| baja | sin etiqueta | 10 | 0 |
| baja | caida_sobre_valle / frena_subida / gira_a_la_baja | 0 | 1 / 3 / 3 |
| baja | no_confirmada | 0 | 3 |
| sube | sin etiqueta | 15 | 0 |
| sube | no_confirmada | 0 | 15 |
| sube | acelera / gira_al_alza / rebote_bajo_pico | 2 / 2 / 2 | 2 / 2 / 2 |
| sube → mixto | frena_caida | 1 (`p99e0041f95f7`) | 1 (ya «mixto») |

Secciones: 6 con industria asignada, 26 sin industria. Con la regla de dirección, 1 fenómeno pasa de
«sube» a «mixto» (`p99e0041f95f7`).
