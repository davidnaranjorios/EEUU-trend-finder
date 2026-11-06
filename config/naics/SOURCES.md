# Tablas oficiales NAICS/SIC (Census Bureau)

Descargadas el 2026-11-06 de https://www.census.gov/naics/ y transformadas con
`scripts/build_naics_tables.py` (sin mapeos manuales; ADR 0012).

- `naics_2022.csv` (naics, name): `2022NAICS/2-6 digit_2022_Codes.xlsx`.
- `sic_naics_concordance.csv` (sic, naics, weight): concordancia oficial SIC 1987 → NAICS 2002
  (`concordances/1987_SIC_to_2002_NAICS.xls`) encadenada con las concordancias oficiales
  2002→2007→2012→2017→2022 (`concordances/<a>_to_<b>_NAICS.xls[x]`). `weight` = 1/n sobre los
  NAICS 2022 alcanzables desde ese SIC (uniforme: Census no publica pesos).
