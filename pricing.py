import re


def estimate_from_text(text):
    t = text.lower()
    base = 0

    if "salle de bain" in t:
        base = 4500
    elif "cuisine" in t:
        base = 6000
    elif "peinture" in t:
        base = 900
    elif "carrelage" in t:
        base = 1400
    elif "plomberie" in t:
        base = 450
    elif "électric" in t or "electric" in t:
        base = 550
    elif "rénov" in t or "renov" in t:
        base = 8000

    m2 = None
    m = re.search(r"(\d{1,3})\s*(m2|m²)", t)
    if m:
        m2 = int(m.group(1))

    if base == 0:
        return {"confidence": 0.3}

    mult = max(1, min(8, (m2 or 10) / 10))
    return {
        "confidence": 0.7,
        "low": round(base * mult * 0.9),
        "high": round(base * mult * 1.25),
    }
