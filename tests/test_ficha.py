"""Ficha (ADR 0036): contexto aparte, 9 preguntas literales con nivel y Confianza por niveles."""

from __future__ import annotations

from src.rank import ficha
from src.report.render import _ficha


def dim(dimension, metric, value=None, text=None, level="directo", unit=None, period="2022"):
    return {
        "dimension": dimension, "metric": metric, "value": value, "value_text": text,
        "unit": unit, "period": period, "source_ref": "x", "level": level, "note": None,
    }  # fmt: skip


def cons(consequence, metric, value=None, text=None, level="directo", unit=None):
    return {
        "consequence": consequence, "metric": metric, "value": value, "value_text": text,
        "unit": unit, "period": "2022", "source_ref": "x", "level": level, "note": None,
    }  # fmt: skip


def chg(kind, status, note="", level="directo", value=1.0):
    return {
        "change_type": kind, "status": status, "value": value, "unit": "u", "period": "p",
        "source_ref": "s", "note": note, "level": level,
    }  # fmt: skip


DIMS = [
    dim("competencia", "cr4", 6.8), dim("competencia", "cr8", 10.3),
    dim("competencia", "empresas", 10663.0), dim("demanda", "ingresos_industria", 1.3e8),
    dim("entrada", "entry_rate", 9.0), dim("entrada", "exit_rate", 7.3),
    dim("brecha", "nivel_vs_pico_previo:bls:X", -1.2),
    dim("brecha", "precio_industria_vs_deflactor", -6.4),
    dim("demanda", "crecimiento_real:fred:REV", 0.65),  # cifra de Evidencia: no va a Demanda
    dim("evidencia", "q:serie:bls:X", 0.02), dim("evidencia", "q:serie:fred:Y", 0.03),
    dim("dinero", "ingreso_por_establecimiento", 7542.0),
]  # fmt: skip


def build(dims=DIMS, change=(), conseq=(), sources=2):
    return ficha.build({"sources": [["a"]] * sources}, dims, list(change), list(conseq))


def ans(f, key):
    return next(a for a in f["answers"] if a["dimension"] == key)


def test_nine_literal_questions_in_order():
    f = build()
    assert [a["question"] for a in f["answers"]] == [
        "¿Qué cambió?", "¿Qué lo prueba?", "¿Quién necesita algo como consecuencia?",
        "¿Qué no resuelve la oferta?", "¿Quién la atiende hoy?",
        "¿Cuánto gastan hoy los compradores en esa necesidad?", "¿Qué cuesta entrar?",
        "¿Qué puede salir mal?", "¿Cuánto de esto está demostrado?",
    ]  # fmt: skip


def test_industry_context_is_separate_from_the_nine_dimensions():
    f = build()
    labels = {x["label"] for x in f["context"]}
    assert {"cr4", "cr8", "empresas", "ingresos_industria", "entry_rate", "exit_rate"} <= labels
    assert any(x.startswith("nivel_vs_pico_previo") for x in labels)
    assert "precio_industria_vs_deflactor" in labels
    for a in f["answers"]:  # ninguna pregunta repite cifras del contexto
        assert not {x["label"] for x in a["figures"]} & {"cr4", "cr8", "empresas", "entry_rate"}


def test_no_figure_means_sin_evidencia():
    f = build(dims=[], sources=0)
    for k in (
        "fenomeno",
        "evidencia",
        "demanda",
        "brecha",
        "competencia",
        "dinero",
        "entrada",
        "riesgo",
    ):
        assert ans(f, k)["level"] == "sin_evidencia"


def test_demand_comes_from_buyers_never_from_evidence_figures():
    f = build()  # hay cifras de evidencia y de crecimiento real, pero ningún comprador
    assert ans(f, "demanda")["level"] == "sin_evidencia" and ans(f, "demanda")["figures"] == []
    f = build(conseq=[cons("compradores", "demanda_hogares", 80.0, level="directo")])
    assert ans(f, "demanda")["level"] == "demostrado"
    f = build(conseq=[cons("compradores", "comprador_1", 5.0, "x", level="indirecto")])
    assert ans(f, "demanda")["level"] == "indicio"


def test_money_is_buyers_spending_not_industry_revenue():
    assert ans(build(), "dinero")["level"] == "sin_evidencia"  # ingresos_industria es contexto
    f = build(conseq=[cons("compradores", "gasto_total", 5e5, unit="millones")])
    assert ans(f, "dinero")["level"] == "demostrado"
    assert ans(f, "dinero")["figures"][0]["label"] == "gasto_total"


def test_evidence_needs_independent_sources_to_be_demostrado():
    assert ans(build(sources=2), "evidencia")["level"] == "demostrado"
    assert ans(build(sources=1), "evidencia")["level"] == "indicio"


def test_staff_shortage_flag_from_productivity_note_marks_change_and_gap_as_hypothesis():
    prod = chg("productividad", "no", note="empleo -4%/año · posible escasez de personal: x")
    f = build(change=[prod])
    assert ans(f, "fenomeno")["note"] == "Posible escasez de personal"
    assert ans(f, "brecha")["level"] == "hipotesis" and f["scarcity"]
    f = build(change=[prod], conseq=[cons("escasez", "nomina_vs_economia", 0.8, level="indirecto")])
    assert ans(f, "brecha")["level"] == "indicio"  # una cifra gana a la hipótesis
    f = build(change=[prod], conseq=[cons("escasez", "vacantes", 5.0, level="directo")])
    assert ans(f, "brecha")["level"] == "demostrado"


def test_confidence_counts_levels_of_the_other_questions_not_figures():
    f = build(
        change=[chg("cantidad", "si")],
        conseq=[cons("compradores", "demanda_x", 1.0), cons("compradores", "gasto_total", 9.0)],
    )
    c = ans(f, "confianza")["counts"]
    assert sum(c.values()) == 8 and c["demostrado"] >= 4
    assert c["demostrado"] == sum(a["level"] == "demostrado" for a in f["answers"])


def test_report_renders_context_and_questions():
    html = _ficha({"ficha": build(conseq=[cons("compradores", "demanda_x", 1.0)])})
    assert "Contexto de industria" in html and "¿Quién necesita algo como consecuencia?" in html
    assert "Sin evidencia" in html and "✅ demostrado" in html
