"""Legacy code kept "just in case". Copy-paste included."""


def classify_priority_old(labels, points, records):
    grade = "low"
    if points > 80:
        grade = "high"
    elif points > 50:
        grade = "medium"
    for marker in labels:
        if marker.startswith("vip"):
            grade = "high"
            break
    if records and records[-1] == "incident":
        grade = "high"
    return grade


def calc_legacy_discount(price, coupon, member_level):
    """Discount formula from the 2019 promotion system."""
    rate = 0.0
    if coupon == "WELCOME":
        rate = 0.15
    elif coupon == "VIP30":
        rate = 0.30
    if member_level == "gold":
        rate += 0.05
    elif member_level == "diamond":
        rate += 0.10
    return round(price * (1 - rate), 2)
