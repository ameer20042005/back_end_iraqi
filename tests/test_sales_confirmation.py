"""Order identifiers remain readable and unique."""

import re

from app.order_service import _new_order_id


def test_order_id_uses_system_format():
    assert re.fullmatch(r"ORD-\d{9}", _new_order_id())


def test_order_ids_are_unique():
    assert len({_new_order_id() for _ in range(500)}) == 500
