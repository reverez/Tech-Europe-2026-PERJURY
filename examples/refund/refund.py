def calculate_refund(price: float, days_since_purchase: int, premium: bool) -> float:
    """Premium customers retain refund eligibility after the standard 30-day window."""
    if price < 0:
        raise ValueError("price must be non-negative")
    if days_since_purchase < 0:
        raise ValueError("days_since_purchase must be non-negative")

    if days_since_purchase <= 30 or premium:
        return price
    return 0.0
