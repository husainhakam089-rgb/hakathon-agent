import pytest

from app.arabic import find_matches, normalize


@pytest.mark.parametrize("a, b", [
    ("أبو علي", "ابو علي"),
    ("أبو علي", "ابوعلي"),
    ("إبراهيم", "ابراهيم"),
    ("آمنة", "امنه"),
    ("مصطفى", "مصطفي"),
    ("مُحَمَّد", "محمد"),
    ("محـــمد", "محمد"),
    ("  حجي    كريم ", "حجي كريم"),
    ("أم حسين", "امحسين"),
])
def test_normalize_equivalents(a, b):
    assert normalize(a) == normalize(b)


def test_normalize_digits():
    assert normalize("محل ٢٥") == "محل 25"
    assert normalize("۱۲") == "12"


def test_normalize_empty():
    assert normalize("") == ""
    assert normalize(None) == ""


def test_abu_inside_name_is_joined():
    assert normalize("الحجي أبو علي") == "الحجي ابوعلي"


def _customers(*names):
    return [{"id": i, "name": n, "normalized_name": normalize(n)} for i, n in enumerate(names, 1)]


def test_find_matches_contains():
    customers = _customers("الحجي كريم", "سعد")
    assert [c["name"] for c in find_matches("حجي كريم", customers)] == ["الحجي كريم"]


def test_find_matches_exact_first():
    customers = _customers("أحمد الحداد", "احمد", "أحمد علي")
    names = [c["name"] for c in find_matches("أحمد", customers)]
    assert names[0] == "احمد"
    assert set(names) == {"أحمد الحداد", "احمد", "أحمد علي"}


def test_find_matches_spelling_variants():
    customers = _customers("أبو علي")
    for q in ("ابو علي", "ابوعلي", "أبو عَلي"):
        assert len(find_matches(q, customers)) == 1


def test_find_matches_none():
    assert find_matches("زيد", _customers("أبو علي")) == []
    assert find_matches("   ", _customers("أبو علي")) == []
