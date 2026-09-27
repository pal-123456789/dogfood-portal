# tests/test_export_hardening.py
"""CSV formula-injection guard for the organizer export (src/judging/services.py).

DB-free like test_smoke.py and test_normalize_engine.py -- nothing here is marked django_db, so
CI never builds a test database. Asserts the pure neutralisation rule (CWE-1236): a cell whose
first character can start a spreadsheet formula is prefixed with an apostrophe, while benign and
non-string cells are returned unchanged. The DB-backed export path (check 7) is exercised by the
acceptance replay and src/normalize/tests.py.
"""
from judging.services import _csv_safe


def test_formula_leads_are_neutralised():
    for payload in ("=HYPERLINK(1)", "+1+2", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"):
        neutralised = _csv_safe(payload)
        assert neutralised == "'" + payload
        assert neutralised[0] == "'"


def test_benign_cells_are_untouched():
    for cell in ("Glass Signal", "prj_07", "a=b=c", "solid work", ""):
        assert _csv_safe(cell) == cell


def test_non_string_cells_pass_through_by_identity():
    for cell in (5, 0, 3, None):
        assert _csv_safe(cell) is cell
