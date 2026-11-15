"""What an ad still needs before it can be activated, and how complete it is (My ads + submit)."""

from .richtext import text_of


def missing_fields(ad) -> list[str]:
    checks = (
        ("title", len((ad.title or "").strip()) >= 3), ("description", bool(text_of(ad.description or "").strip())),
        ("categories", bool(ad.category)), ("areas", bool(ad.region)), ("offers to", bool(ad.audiences)),
        ("gender", bool(ad.gender)), ("age", bool(ad.age)),
    )
    return [label for label, ok in checks if not ok]


def complete_percent(ad, picture_count: int) -> int:
    """Same checks as the editor's progress bar (components/AdEditor.tsx → completeness)."""
    checks = [
        len((ad.title or "").strip()) >= 3, len(text_of(ad.description or "")) >= 300, bool(ad.categories or ad.category),
        bool(ad.regions or ad.region), bool(ad.city.strip()), bool(ad.audiences), bool(ad.phone.strip() or ad.contact_email.strip()),
        bool(ad.gender), (ad.age or 0) >= 18, bool(ad.height_cm), bool(ad.body_type), bool(ad.eye_color), bool(ad.hair_color),
        bool(ad.bust_size), picture_count > 0, picture_count >= 3, bool(ad.services), bool(ad.contact_hours), ad.tags.exists(),
    ]
    return round(sum(checks) / len(checks) * 100)
