"""Principio 1: ni src/ ni config.yaml contienen temas, términos o series de interés.

Es una heurística: atrapa listas literales largas de strings, claves temáticas en la config e
IDs con forma de serie. No sustituye la revisión humana del diff.
"""

import pytest

from src.config import DEFAULT_CONFIG_PATH, REPO_ROOT
from tests.guards import hardcoded_topic_violations_py, hardcoded_topic_violations_yaml


def test_no_hardcoded_topics():
    violations = hardcoded_topic_violations_py(REPO_ROOT / "src")
    violations += hardcoded_topic_violations_yaml(DEFAULT_CONFIG_PATH)
    assert violations == [], "\n".join(violations)


PY_TRAPS = {
    "industry_list.py": "INDUSTRIES = ['a', 'b', 'c', 'd', 'e', 'f']\n",
    "keyword_set.py": "KW = {'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'w7'}\n",
    "mapping.py": "M = {'k1': 1, 'k2': 2, 'k3': 3, 'k4': 4, 'k5': 5, 'k6': 6}\n",
    "series_id_digit.py": "SID = 'GDPC1'\n",
    "series_id_canary.py": "def f():\n    return 'UNRATE'\n",
}


@pytest.mark.parametrize("filename", sorted(PY_TRAPS))
def test_py_guard_catches_trap(tmp_path, filename):
    (tmp_path / filename).write_text(PY_TRAPS[filename], encoding="utf-8")
    assert hardcoded_topic_violations_py(tmp_path), f"la trampa {filename} no fue detectada"


YAML_TRAPS = {
    "topic_key.yaml": "detect:\n  topics: [x]\n",
    "watchlist.yaml": "watchlist: []\n",
    "long_list.yaml": "anything: [a, b, c, d, e, f]\n",
    "series_value.yaml": "baseline: A191RL1Q225SBEA\n",
    # La excepción de series de referencia no se extiende a otras rutas ni a otras claves.
    "reference_elsewhere.yaml": "report:\n  reference_series:\n    deflator: CPIAUCSL\n",
    "reference_extra_key.yaml": (
        "context:\n  reference_series:\n    deflator: CPIAUCSL\n    population: POP\n"
        "    recessions: USREC\n    favorite: GDPC1\n"
    ),
    "reference_missing_key.yaml": "context:\n  reference_series:\n    deflator: CPIAUCSL\n",
    "reference_list.yaml": (
        "context:\n  reference_series:\n    deflator: [CPIAUCSL, GDPC1]\n    population: POP\n"
        "    recessions: USREC\n"
    ),
}


@pytest.mark.parametrize("filename", sorted(YAML_TRAPS))
def test_yaml_guard_catches_trap(tmp_path, filename):
    path = tmp_path / filename
    path.write_text(YAML_TRAPS[filename], encoding="utf-8")
    assert hardcoded_topic_violations_yaml(path), f"la trampa {filename} no fue detectada"


def test_py_guard_allows_structural_code(tmp_path):
    (tmp_path / "ok.py").write_text(
        "FREQ = ('D', 'W', 'M', 'Q', 'A')\nlevel = 'INFO'\n"
        "def to_dict(s):\n"
        "    return {'a': s.a, 'b': s.b, 'c': s.c, 'd': s.d, 'e': s.e, 'f': s.f}\n",
        encoding="utf-8",
    )
    assert hardcoded_topic_violations_py(tmp_path) == []


def test_yaml_guard_allows_only_the_reference_series_exception(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text(
        "context:\n  reference_series:\n    deflator: CPIAUCSL\n    population: POP\n"
        "    recessions: USREC\n",
        encoding="utf-8",
    )
    assert hardcoded_topic_violations_yaml(path) == []


BOILERPLATE_LIST = "\n".join(f"    - frase numero{i}" for i in range(8))


def test_boilerplate_exception_is_limited_to_its_key(tmp_path):
    ok = tmp_path / "ok.yaml"
    ok.write_text(f"text_detect:\n  boilerplate:\n{BOILERPLATE_LIST}\n", encoding="utf-8")
    assert hardcoded_topic_violations_yaml(ok) == []
    # La misma lista bajo cualquier otra clave sigue fallando.
    for where in ("text_detect:\n  otra_lista", "text:\n  boilerplate", "rank:\n  boilerplate"):
        bad = tmp_path / f"bad_{abs(hash(where))}.yaml"
        bad.write_text(
            f"{where}:\n{BOILERPLATE_LIST}\n".replace("\n  ", "\n  ", 1), encoding="utf-8"
        )
        assert hardcoded_topic_violations_yaml(bad), where
    # Y en su clave solo admite frases, no ids de serie.
    series = tmp_path / "series.yaml"
    series.write_text("text_detect:\n  boilerplate:\n    - UNRATE\n", encoding="utf-8")
    assert hardcoded_topic_violations_yaml(series)
