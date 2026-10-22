import uuid

from sqlalchemy.orm import Session

from app.models import PlatformSetting

DEFAULTS: dict[str, dict] = {
    "payment": {"upi_id": None, "payee_name": None, "instructions": None, "qr_asset_id": None},
    "support": {"email": None, "phone": None, "hours": None},
    "policies": {"refund_policy": None, "terms": None, "privacy": None},
}


def get_setting(db: Session, key: str) -> dict:
    row = db.get(PlatformSetting, key)
    return {**DEFAULTS.get(key, {}), **(row.value if row else {})}


def put_setting(db: Session, key: str, value: dict, actor_id: uuid.UUID) -> dict:
    row = db.get(PlatformSetting, key)
    merged = {**get_setting(db, key), **value}
    if row:
        row.value = merged
        row.updated_by_id = actor_id
    else:
        db.add(PlatformSetting(key=key, value=merged, updated_by_id=actor_id))
    return merged
