# Ficha 001 — Residencias de cuidado (NAICS 6231 / 623), hecha a mano

Preparada el 2026-11-13 como **prototipo del formato** (Etapa 5 sin LLM). Fuente del fenómeno:
corrida de ranking `38942660a49941c5ae25f9ac626b13c5` (2026-11-11), fenómeno `pc168465542bf`,
puesto 2 de 2 candidatos triangulados. Datos de mercado: APIs públicas (FRED, Census CBP, BDS,
Economic Census 2022, PEP, USAspending) consultadas el 2026-11-13.

Niveles: ✅ demostrado con dato oficial · 🟡 indicio (dato indirecto o de otro nivel NAICS) ·
⚪ hipótesis (sin dato).

## Resumen en una línea

El empleo de residencias de enfermería **volvió al nivel de 2019, pero no lo supera**. Los
ingresos reales crecen ~22 % desde 2021 con menos personal que antes de la pandemia. Es
una **industria fragmentada** (las 4 mayores tienen el 6,8 % de los ingresos) y la entrada de
nuevos establecimientos está **por debajo del promedio de la economía**. El fenómeno detectado es
sobre todo una **recuperación**, no una tendencia nueva.

## Las 9 dimensiones

| # | Dimensión | Respuesta en una frase | Dato (unidad · fuente · fecha) | Nivel | Qué falta para subir de nivel |
|---|---|---|---|---|---|
| 1 | **Fenómeno** | Las residencias de cuidado crecen desde el piso de 2021–22, pero el empleo de enfermería especializada apenas recupera 2019. | Empleo 6231: 1.351,0 mil (2021-10) → 1.591,8 mil (2026-10); 2019-01: 1.599,9 mil; máximo anual 1.669,7 mil (2011) · BLS CES6562310001 vía FRED · 2026-11-13 | ✅ | — (el dato es directo). Ver nota A: es recuperación, no máximo nuevo. |
| 2 | **Evidencia** | Dos agencias independientes, 5 series con señal `slope` tras FDR (BH). | REV623AMSA q=0,021 · QEXP623AUSY q=0,043 (Census QSS, valor) · CES6562310001 q=0,023 (BLS, cantidad) · CES6562310057 q=0,047 y CES6562310017 q=0,048 (BLS, nómina) · `rank_evidence`, corrida 2026-11-11 | 🟡 | Las dos series de nómina son **valor nominal** aunque el sistema las marcó como cantidad (nota B). La cantidad real descansa en **una sola serie**. Además mezcla niveles NAICS: ingresos 623 y empleo 6231 (nota C). |
| 3 | **Demanda** | Los ingresos reales del subsector crecen; la población de 85+ crece poco. | Ingresos 623: 62.696 → 97.898 M$/trim. (2019T1 → 2026T2, desest.), **+56 % nominal**. Deflactados con PPI 6231: **+22 % real** (2021T3 → 2026T2) · Census QSS REV623AMSA + BLS PCU623110623110 · Población 85+: 6,08 M (2020) → 6,19 M (2023), +1,9 % · Census PEP | 🟡 | El deflactor es del 6231 y se aplica a todo 623. Falta gasto del comprador (BEA PCE «nursing homes») y la mezcla Medicare/Medicaid/privado. |
| 4 | **Brecha** | Hay más ingreso real con menos trabajadores que en 2019: falta oferta de personal, no de camas. | Empleo 6231 −0,5 % vs 2019-01 con ingresos reales +22 % · CBP 6231: empleo 1,623 M (2019) → 1,427 M (2023), −12 %; establecimientos +3,5 % · Vacantes en salud y asistencia social: 5,8 % (2026-09), igual que 2019-01 (máx. 8,5 % en 2021-10) · BLS JOLTS JTS6200JOR | 🟡 | JOLTS es del sector 62 completo, no del 6231. El precio (PPI 6231 +28,8 % desde 2019-01) subió **menos** que el IPC (+33,6 %): no hay brecha de precio. Falta ocupación de camas (CMS, no está en el sistema). |
| 5 | **Competencia** | Muy fragmentada; los grandes pesan por tamaño, no por concentración. | 2022: 10.663 empresas, 17.951 establecimientos · 4 mayores: **6,8 %** de ingresos; 8: 10,3 %; 20: 16,5 %; 50: 25,2 % · 278 empresas con 500+ empleados = 58,5 de 132,2 mil M$ (44 %) · Census Economic Census 2022 (`ecnsize`) | ✅ | — . Para comparar: vivienda asistida (623312) está más concentrada: 4 mayores 15,7 %, 50 mayores 38,0 %. |
| 6 | **Dinero** | Mercado de ~135 mil M$/año, con ingreso típico por empresa de 5–25 M$. | Ingresos 6231 2022: 135.396 M$ · 4.772 de 9.203 empresas (52 %) facturan entre 5 y 25 M$ · Ingreso medio por establecimiento: 7,5 M$ · Contratos federales 623110 (FY2023–FY2025): 2.643 M$ · Census EC 2022; USAspending.gov | ✅ | Margen: QEXP623AUSY permite calcular ingresos − gastos para 623, no para 6231. |
| 7 | **Entrada** | Entrar es posible pero lento: la entrada sube y supera a la salida, aunque por debajo de la economía. | BDS 6231: entrada 9,0 % / salida 7,3 % (2023); 6,6 % / 5,1 % (2019) · Economía total 2023: 10,6 % / 9,4 % · Vivienda asistida 6233 2023: entrada 8,2 % < salida 9,1 % · Census BDS | 🟡 | Capital por establecimiento (BEA activos fijos 623, ya en R2) y carga regulatoria (menciones en Federal Register) no están calculados. Licencia estatal y certificación CMS: ⚪. |
| 8 | **Riesgo** | Dependiente del pago público y del personal; el empleo cayó 15 % entre 2019 y 2022. | Empleo 6231 −15 % (1.595,8 mil → 1.355,3 mil, promedios 2019 → 2022) y tendencia de baja desde 2011 · PPI de pacientes Medicare/Medicaid +35,4 % desde 2019-01 · BLS CES, PCU6231106231101 | ⚪ | Riesgos de los 10-K del NAICS (EDGAR sin NAICS todavía) y la regla de plantilla mínima de CMS: no están en los datos. Ciclicidad frente a recesiones: no calculada. |
| 9 | **Confianza** | 3 ✅, 4 🟡, 1 ⚪ (de 8 dimensiones evaluables). | — | 🟡 | Corregir las notas B y C y repetir. |

### Hipótesis de oportunidad (⚪, para que David decida si vale la pena)

- **No** parece una oportunidad de abrir residencias: el precio real cae (PPI < IPC), la cantidad
  apenas recupera 2019 y la tendencia anterior a la pandemia era a la baja.
- **Sí** puede haberla en **la falta de personal**: más ingreso real por trabajador, con una
  industria fragmentada (10.663 empresas, la mayoría de 50–249 empleados). Por ejemplo,
  servicios de personal o de productividad para operadores medianos. Ningún dato del sistema
  mide todavía esa demanda.

## Notas para la revisión del formato

**A. Recuperación, no tendencia.** El detector compara una ventana reciente que empieza en
2021-07/2021-11, justo en el piso de la pandemia. Con eso, cualquier rebote sale como «sube».
Propuesta para la Etapa 3: una etiqueta neutral, calculada de los datos, **«nivel frente al
pico previo al choque»** (nuevo máximo / recuperado / por debajo), con el pico dentro de
una ventana configurable antes de cada recesión (USREC ya es serie de referencia).

**B. Error de clase de medida.** CES6562310057 («Aggregate weekly payrolls … thousands») y
CES6562310017 («Indexes of aggregate weekly payrolls») son **nómina en dólares**. Las clasifica
como cantidad la regla `cantidad_conteos` de `config/measure_kind.yaml`, por la unidad «thousands».
Arreglo propuesto: una regla `valor_nomina` (`title_regex: "(?i)\bpayrolls?\b"`, `kind: valor`)
antes de los conteos. Afecta a todas las series CES de nómina, no solo a esta.

**C. Nivel NAICS mezclado.** Los ingresos son del 623 completo y el empleo es del 6231. La ficha
tiene que declarar un nivel y usar todas las métricas en ese nivel. Si no hay dato, la fila baja a
🟡 y lo dice.

**D. Lo que sí funcionó del formato:** una frase, el dato con unidad, fuente y fecha, el nivel
y «qué falta» hacen que cada afirmación se pueda verificar y que se vea dónde está el hueco. El
cálculo de Competencia y Entrada salió en minutos de APIs públicas de Census con la clave que ya
existe: la Etapa 2 es factible sin fuentes de pago.

## Reproducir

Consultas usadas (API key de Census y FRED en el entorno):
- `api.census.gov/data/2022/ecnsize?get=CONCENFI_LABEL,VAL_PCT&NAICS2022=6231&CONCENFI=*`
- `api.census.gov/data/2022/ecnsize?get=EMPSZFF_LABEL,FIRM,RCPTOT&NAICS2022=6231&EMPSZFF=*`
- `api.census.gov/data/{2019,2021,2022,2023}/cbp?get=ESTAB,EMP,PAYANN&NAICS2017=6231`
- `api.census.gov/data/timeseries/bds?get=ESTABS_ENTRY_RATE,ESTABS_EXIT_RATE&NAICS=6231&YEAR=…`
- `api.census.gov/data/2023/pep/charv?get=POP,YEAR&AGE=8599`
- FRED: `CES6562310001`, `CES6562300001`, `REV623AMSA`, `PCU623110623110`,
  `PCU6231106231101`, `JTS6200JOR`, `CPIAUCSL`
- `api.usaspending.gov/api/v2/search/spending_by_category/naics/` (contratos A–D, FY2023–FY2025)
