import pytest

from app import db
from app.tools import DebtTools


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def tools(conn):
    return DebtTools(conn, large_amount_iqd=1_000_000)
