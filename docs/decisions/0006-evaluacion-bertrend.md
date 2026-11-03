# 0006 — Evaluación de BERTrend como capa opcional de temas

- Fecha: 2026-11-03
- Estado: evaluada; **no se implementa** en la Fase 3 (queda como mejora futura).

## Contexto

CLAUDE.md propone evaluar BERTrend (Boutaleb, Picault & Grosjean 2024, arXiv 2411.05930) como
capa opcional de temas sobre los n-gramas, "previa revisión de código y licencia". Esta es esa
revisión. No se agregó ninguna dependencia.

## Lo verificado (2026-11-03)

| Punto | Dato | Fuente |
|---|---|---|
| Método | Modelado neuronal de temas *online* (BERTopic por ventanas de tiempo). Un indicador de popularidad combina número de documentos y frecuencia de actualización, y clasifica cada tema en ruido, señal débil o señal fuerte. | abstract de arXiv 2411.05930 (v2, 21-nov-2024) |
| Código | `rte-france/BERTrend`, versión 0.4.26, último push 2026-10-27. El README remite al paper. | API de GitHub y README del repositorio |
| Licencia | Mozilla Public License 2.0, © 2023–2025 RTE France | README ("License Information") y API de GitHub (`MPL-2.0`) |
| Python | `requires-python = ">=3.12,<4.0"` | `pyproject.toml` |
| Dependencias | ~60 directas, entre ellas `torch`, `sentence-transformers`, `bertopic`, `hdbscan`, `umap-learn`, `streamlit`, `fastapi`, `openai`, `langchain-core` y clientes de noticias | `pyproject.toml` |
| Hardware | "Recommended: at least one GPU with 16GB RAM"; por defecto usa un servicio remoto de embeddings | README |

El enlace de arXiv no publica el repositorio de código; el repositorio de RTE France es el que
cita el paper en su README.

## Evaluación

**A favor**
- Licencia permisiva para usarlo como dependencia. MPL-2.0 es copyleft por archivo: solo
  obliga a publicar los archivos de BERTrend que se modifiquen, no el resto del proyecto.
- Está mantenido (push hace una semana) y su clasificación ruido / débil / fuerte apunta al
  mismo problema que este proyecto.

**En contra (para este proyecto, hoy)**
1. **Necesita el texto de cada documento** para calcular embeddings. La decisión del
   2026-11-03 es no guardar el texto completo (ADR 0005). Habría que calcular los embeddings en
   la ingesta y guardarlos. Del orden de 10⁶ documentos × 384–1024 floats son varios GB, y eso
   choca con la capa gratuita de R2 y con el criterio de "solo text_hash y frecuencias".
2. **Cómputo:** recomienda GPU; los runners de GitHub Actions no la tienen. La alternativa por
   defecto, un servicio remoto de embeddings, saca el texto fuera del sistema.
3. **Dependencias:** torch, streamlit, fastapi, openai y unas 60 más multiplican el tamaño
   del entorno y la superficie de mantenimiento. Además `requires-python >= 3.12` choca con el
   mínimo actual del proyecto (3.11, que usa la CI).
4. **FDR:** su clasificación de señales usa umbrales sobre un indicador de popularidad, no
   p-values. Integrarlo al ranking exigiría calibrar ese indicador con el mismo block bootstrap
   y pasarlo por BH (principio 3); no viene hecho.
5. **Principio 1:** está bien, porque los temas salen de los datos. Pero la interpretación con
   LLM que ofrece no debe alimentar `src/` sin revisión.

## Decisión

No se integra en la Fase 3. El detector de términos emergentes (parte B) trabaja sobre
`term_freq` (n-gramas de 1 a 3) con block bootstrap y BH, que cumple los principios sin texto
completo ni GPU.

Se reconsidera si se da alguna de estas condiciones:
- se decide guardar el texto o embeddings, con presupuesto de almacenamiento;
- hay cómputo con GPU (p. ej. un VPS, ADR 0003);
- en la Fase 5, la deduplicación de términos en clusters necesita algo más que similitud
  léxica. Ahí BERTopic, o un modelo de embeddings local de licencia abierta, podría agrupar
  n-gramas sin usar BERTrend completo.

Si se integra, sería como un paso opcional de `src/detect/` cuyas señales pasan por la misma
calibración y BH que las demás. El paper y el código quedan citados en las referencias de
CLAUDE.md.
