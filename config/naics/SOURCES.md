# Tablas oficiales NAICS/SIC (Census Bureau)

Descargadas el 2026-11-06 de https://www.census.gov/naics/ y transformadas con
`scripts/build_naics_tables.py` (sin mapeos manuales; ADR 0012).

- `naics_2022.csv` (naics, name): `2022NAICS/2-6 digit_2022_Codes.xlsx`.
- `sic_naics_concordance.csv` (sic, naics, weight): concordancia oficial SIC 1987 → NAICS 2002
  (`concordances/1987_SIC_to_2002_NAICS.xls`) encadenada con las concordancias oficiales
  2002→2007→2012→2017→2022 (`concordances/<a>_to_<b>_NAICS.xls[x]`). `weight` = 1/n sobre los
  NAICS 2022 alcanzables desde ese SIC (uniforme: Census no publica pesos).

## Concordancia BEA ↔ NAICS y NAICS 2017 → 2022 (tarea de la auditoría f9189307, ADR 0013)

Descargadas el 2026-11-09 y transformadas con `scripts/build_bea_concordance.py` (sin mapeos
manuales).

- `bea_industry_naics.csv` (bea_code, naics, weight): BEA, «BEA Industry and Commodity Codes and
  NAICS Concordance» (versión de 2023-10; columnas «Related 2017 NAICS Codes»), enlazada desde
  https://www.bea.gov/itable/gdp-by-industry →
  https://www.bea.gov/sites/default/files/2023-10/BEA-Industry-and-Commodity-Codes-and-NAICS-Concordance.xlsx
  (sha256 `6e25267ff60ccedc0808c14153b0cdeb566a7f5e9097536c70c2b9694ef5ff47`).
  Cubre los códigos de sector, resumen, resumen ampliado, detalle y producción bruta de BEA: los
  mismos que usan `Industry` (GDPbyIndustry, UnderlyingGDPbyIndustry) y `RowCode`/`ColCode`
  (InputOutput) de la API. La tabla de BEA es NAICS **2017**: cada código relacionado (de 2 a 6
  dígitos) se expande a sus códigos de 6 dígitos de la concordancia oficial de Census y se pasa a
  NAICS 2022; después se sube al padre cuando están todos sus hijos de `naics_2022.csv`. Peso
  uniforme 1/n (BEA no publica pesos). «n.a.» (gobierno, usado, importaciones no comparables) se
  omite; «23*» (construcción) cuenta como 23. Los agregados que BEA define sin NAICS (`PVT`, `GDP`,
  `FIRE`, `31G`, `6`, `7`...) quedan sin fila: no se inventa.
- `naics_2017_to_2022.csv` (naics2017, naics2022, weight): Census, concordancia oficial
  2017 → 2022 NAICS, https://www.census.gov/naics/concordances/2017_to_2022_NAICS.xlsx
  (sha256 `4662cc7ed9e7f3fb8a968e9504a7d06e448f5b65a349996a5627439df193eb30`). Solo los códigos
  que cambiaron; sirve para leer códigos NAICS 2017 de Census (ASM, BDS, AIES). Peso 1/n.
