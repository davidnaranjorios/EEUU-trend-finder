# Grafo interactivo en GitHub Pages

La página publica `docs/graph/index.html`, una exportación del grafo local de
Graphify. No extrae código ni llama a modelos de IA durante el despliegue.
El JSON generado sigue siendo local y no se versiona.

## Activación inicial

Tras fusionar el PR, en GitHub abre Settings → Pages → Build and deployment y
selecciona **GitHub Actions** como Source. Ejecuta el workflow **Publish
interactive Graphify graph** desde Actions si el primer despliegue falló antes
de habilitar Pages.

URL prevista: https://davidnaranjorios.github.io/EEUU-trend-finder/

## Actualizar cuando se solicite

En un entorno que tenga el grafo existente y Graphify 0.9.107:

```bash
graphify export html --graph graphify-out/graph.json
cp graphify-out/graph.html docs/graph/index.html
```

Revisa y publica únicamente el HTML actualizado mediante un PR. Al fusionarlo
en main, el workflow actualiza la misma URL. También puede volver a desplegarse
la versión publicada mediante workflow_dispatch, sin regenerar el HTML.
La exportación representa el JSON disponible; no actualiza por sí sola un grafo
obsoleto. Cualquier extracción nueva requiere una solicitud independiente.

El visor incluye búsqueda, filtros por comunidad y panel de conexiones. Requiere
Internet para cargar vis-network 9.1.6 desde unpkg.com. El sitio es público.

## Exportación inicial

Grafo: 3.477 nodos. Commit de código de referencia:
`845278d4e11bb88bf9e53dfec4956eb6131c149e`.
SHA-256 del JSON de origen:
`f2aa75b9a39ec077589f4c9b9c1fe9b42ceadc70ede2bb478c8062e771bfe242`.
