from __future__ import annotations

PRODUCT_EUROBAT_CAPTURE = "eurobat_capture"
PRODUCT_DEVIS_INTELLIGENT = "devis_intelligent"
PRODUCT_ARCHITECTURE_3D = "architecture_3d"

PRODUCT_CODES = {
    PRODUCT_EUROBAT_CAPTURE,
    PRODUCT_DEVIS_INTELLIGENT,
    PRODUCT_ARCHITECTURE_3D,
}

DEFAULT_TRIAL_DAYS = 60
DEFAULT_BILLING_DAYS = 30
DEFAULT_PLAN_CODE = "abonnement"



def normalize_product_code(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in PRODUCT_CODES:
        raise ValueError(f"Produit non supporte: {value}")
    return normalized
