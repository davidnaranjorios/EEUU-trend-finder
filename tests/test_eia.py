from datetime import date

from src.ingest import eia
from src.rank import agencies


def test_period_date_formats():
    assert eia.period_date("202609") == date(2026, 9, 1)
    assert eia.period_date("2026Q3") == date(2026, 7, 1)
    assert eia.period_date("2026") == date(2026, 1, 1)
    assert eia.period_date("2026W1") is None and eia.period_date("") is None


def test_geo_levels():
    assert eia.geo_of("USA") == ("nacional", "USA")
    assert eia.geo_of("USA-TX")[0] == "regional"
    assert eia.geo_of(None)[0] == "sin_geografia"


def test_parse_line_skips_categories_facilities_and_garbage():
    assert eia.parse_line('{"category_id": "1", "name": "x"}') is None
    assert eia.parse_line('{"series_id": "ELEC.PLANT.X", "lat": 1, "lon": 2}') is None
    assert eia.parse_line('{"series_id": "A" truncado') is None
    assert eia.parse_line('{"series_id": "ELEC.PRICE.US-RES.M", "f": "M"}')["f"] == "M"


def test_eia_is_its_own_agency():
    keys = agencies.series_agencies("eia")
    assert keys == agencies.series_agencies("eia") and keys
    assert keys != agencies.series_agencies("bls")
