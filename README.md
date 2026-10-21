# EEUU-trend-finder

Descubrimiento de tendencias estructurales en datos económicos y de texto de EE. UU., sin
hipótesis previas. Ver `CLAUDE.md` para principios, arquitectura y fases.

```
cp .env.example .env   # rellenar claves
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```
