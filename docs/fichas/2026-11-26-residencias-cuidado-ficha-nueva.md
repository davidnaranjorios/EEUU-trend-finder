# Ficha del fenómeno 6231 (residencias de cuidado) — formato nuevo (ADR 0036)

Corrida de referencia `19979e2620e64e45beae7b9f420c39ab`, fenómeno `p848db8edc766`. «¿Quién necesita…?» y «¿Cuánto gastan…?» salen sin evidencia porque `market/io_flows` (insumo-producto de BEA, ADR 0032) no está en R2: falta correr `ingesta-mercado.yml`. «¿Qué puede salir mal?» sin evidencia por la muestra de `text_structure` (límite del ADR 0032).

### Contexto de industria

- cr4: 6.80 Percent · 2022 · market/naics_metrics:cr4 (econcensus:CR4:6231) (directo)
- cr8: 10.30 Percent · 2022 · market/naics_metrics:cr8 (econcensus:CR8:6231) (directo)
- empresas: 10,663.00 Number · 2022 · market/naics_metrics:firms (econcensus:FIRM:6231) (directo)
- ingresos_industria: 135,396,289.00 Thousands of dollars · 2022 · market/naics_metrics:revenue (econcensus:RCPTOT:6231) (directo)
- entry_rate: 9.03 Percent · 2023 · market/naics_metrics:entry_rate (bds:ESTABS_ENTRY_RATE:6231) (directo)
- exit_rate: 7.32 Percent · 2023 · market/naics_metrics:exit_rate (bds:ESTABS_EXIT_RATE:6231) (directo)
- entry_rate_economia_total: 10.61 Percent · 2023 · market/naics_metrics:entry_rate (directo)
- exit_rate_economia_total: 9.40 Percent · 2023 · market/naics_metrics:exit_rate (directo)
- nivel_vs_pico_previo:bls:CES6562310001: -1.17 % frente al máximo previo a la última recesión pico 2018-03 · 2026-10 · bls:CES6562310001 (directo)
- precio_industria_vs_deflactor: -6.41 puntos porcentuales precio +24.4% · deflactor +30.8% · 2020-03 a 2026-09 · bls:PCU6231--6231-- vs fred:CPIAUCSL (directo)

### Las 9 preguntas

**¿Qué cambió?** — 🟡 indicio · Posible escasez de personal
  - cantidad: 1.00 series si · 1 de 1 series de producción; trayectorias: acelera; índice de precios de 6231 para una serie de 623 · 2021-07-01 a 2026-04-01 · fred:REV623AMSA (indirecto)
  - precio: -0.42 pp/año no · precio +3.78%/año · deflactor +4.20%/año · 2021-07 a 2026-09 · bls:PCU6231--6231-- vs fred:CPIAUCSL (directo)
  - productividad: 2.56 % por año (ingreso real por trabajador) no · por trabajador +2.56%/año · por establecimiento -1.76%/año · empleo -3.49%/año · deflactor: PCU 6231 · posible escasez de personal: el empleo cae más de 2%/año, la productividad no se presenta como mejora · insumos: bls:CES6562310001 rebote_bajo_pico, fred:QEXP623AUSY acelera · 2017 a 2022 · market/naics_metrics:revenue,employment (directo)
  - composicion: 0.00 criterios en la cola no · 3 de 4 criterios evaluables · CR4 -2.1 pp (P51, corte 5.3: fuera) · CR8 -2.2 pp (P49, corte 5.0: fuera) · empresas pequeñas +2.9 pp (P69, corte 4.4: fuera) · entrada vs economía total -1.6 pp (P21, corte 5.4: fuera) · salida vs economía total -2.1 pp (P47, corte 4.3: fuera) · 2017 a 2022; 2023 · market/naics_metrics (directo)

**¿Qué lo prueba?** — ✅ demostrado · 2 fuente(s) independiente(s)
  - serie:bls:CES6562310001: 0.04 q (BH) · 2021-11-01 a 2026-10-01 · bls:CES6562310001 (directo)
  - serie:fred:QEXP623AUSY: 0.04 q (BH) · 2021-07-01 a 2026-04-01 · fred:QEXP623AUSY (directo)
  - serie:fred:REV623AMSA: 0.02 q (BH) · 2021-07-01 a 2026-04-01 · fred:REV623AMSA (directo)

**¿Quién necesita algo como consecuencia?** — — sin evidencia
  - sin evidencia: ninguna cifra responde la pregunta

**¿Qué no resuelve la oferta?** — 🟡 indicio · Posible escasez de personal: el empleo cae y la productividad no es una mejora
  - produccion_real_por_trabajador: 2.56 % por año (ingreso real por trabajador) por trabajador +2.56%/año · por establecimiento -1.76%/año · empleo -3.49%/año · deflactor: PCU 6231 · posible escasez de personal: el empleo cae más de 2%/año, la productividad no se presenta como mejora · insumos: bls:CES6562310001 rebote_bajo_pico, fred:QEXP623AUSY acelera · 2017 a 2022 · market/naics_metrics:revenue,employment (directo)
  - nomina_por_trabajador: 44,479.14 dólares por trabajador al año economía: 68,342 dólares · 2023 · market/naics_metrics:payroll,employment (directo)
  - nomina_vs_economia: 0.65 veces el promedio de la economía 44,479 frente a 68,342 dólares · 2023 · market/naics_metrics:payroll,employment (directo)

**¿Quién la atiende hoy?** — ✅ demostrado
  - composicion: 0.00 criterios en la cola 3 de 4 criterios evaluables · CR4 -2.1 pp (P51, corte 5.3: fuera) · CR8 -2.2 pp (P49, corte 5.0: fuera) · empresas pequeñas +2.9 pp (P69, corte 4.4: fuera) · entrada vs economía total -1.6 pp (P21, corte 5.4: fuera) · salida vs economía total -2.1 pp (P47, corte 4.3: fuera) · 2017 a 2022; 2023 · market/naics_metrics (directo)
  - salida_vs_economia: 0.78 veces la tasa de salida de la economía 7.317 % frente a 9.396 % de la economía (-2.079 pp) · 2023 · market/naics_metrics:exit_rate (directo)
  - cambio_cr4: -2.10 puntos porcentuales 8.9 % en 2017 a 6.8 % en 2022 · 2017 a 2022 · market/naics_metrics:cr4 (directo)

**¿Cuánto gastan hoy los compradores en esa necesidad?** — — sin evidencia
  - sin evidencia: ninguna cifra responde la pregunta

**¿Qué cuesta entrar?** — 🟡 indicio
  - ingreso_por_establecimiento: 7,542.55 ingresos/establecimiento · 2022 · market/naics_metrics:revenue/establishments_ec (directo)
  - ingreso_por_empresa: 12,697.77 ingresos/empresa · 2022 · market/naics_metrics:revenue/firms (directo)
  - ingresos_menos_gastos: 9,287.00 misma unidad de las series ingresos 97,898 − gastos 88,611 (+9.5 % de los ingresos) · 2026-04 · fred:REV623AMSA − fred:QEXP623AUSY (indirecto)

**¿Qué puede salir mal?** — — sin evidencia
  - sin evidencia: ninguna cifra responde la pregunta

**¿Cuánto de esto está demostrado?** — ✅ demostrado: 2 · 🟡 indicio: 3 · ⚪ hipótesis: 0 · — sin evidencia: 3
