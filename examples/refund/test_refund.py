from examples.refund.refund import calculate_refund


def test_standard_customer_inside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 14, premium=False) == 100.0


def test_standard_customer_outside_window_gets_no_refund() -> None:
    assert calculate_refund(100.0, 45, premium=False) == 0.0


def test_premium_customer_inside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 14, premium=True) == 100.0


def test_zero_price_is_valid() -> None:
    assert calculate_refund(0.0, 10, premium=False) == 0.0
