"""
Seed script to create initial test users in the database.

Usage:
    python seed.py

This will create 3 client users if they don't already exist:
    - user1@mts.com / 1234
    - user2@mts.com / 1234
    - user3@mts.com / 1234

It also creates an admin user if ADMIN_EMAIL / ADMIN_PASSWORD are set in .env.
"""

import hashlib
import os
import secrets
from pathlib import Path

from database import SessionLocal
from models import UserAccount

BASE_DIR = Path(__file__).resolve().parent


def _load_local_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def _hash_password(raw_password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${derived.hex()}"


SEED_USERS = [
    {"email": "user1@mts.com", "name": "User One", "phone": "+33600000001", "password": "1234"},
    {"email": "user2@mts.com", "name": "User Two", "phone": "+33600000002", "password": "1234"},
    {"email": "user3@mts.com", "name": "User Three", "phone": "+33600000003", "password": "1234"},
]

SEED_ADMIN = {
    "email": "admin@mts.com",
    "name": "Admin",
    "phone": "",
    "password": "1234",
    "role": "admin",
}


def seed_users():
    db = SessionLocal()
    try:
        created = 0
        skipped = 0
        for u in SEED_USERS:
            existing = db.query(UserAccount).filter(UserAccount.email == u["email"]).first()
            if existing:
                skipped += 1
                print(f"  SKIP  {u['email']} (already exists)")
                continue

            user = UserAccount(
                email=u["email"],
                password_hash=_hash_password(u["password"]),
                role="client",
                name=u["name"],
                phone=u["phone"],
                status="actif",
            )
            db.add(user)
            created += 1
            print(f"  CREATE {u['email']} (role=client)")

        db.commit()
        print(f"\nDone: {created} created, {skipped} skipped.")
    finally:
        db.close()


def ensure_admin():
    """Create an admin user from SEED_ADMIN or ADMIN_EMAIL / ADMIN_PASSWORD env vars if set."""
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()

    admins_to_seed = []
    if admin_email and admin_password:
        admins_to_seed.append({
            "email": admin_email,
            "name": "Admin",
            "phone": "",
            "password": admin_password,
            "role": "admin",
        })
    # Always seed the default admin
    admins_to_seed.append(SEED_ADMIN)

    db = SessionLocal()
    try:
        for admin_data in admins_to_seed:
            email = admin_data["email"].strip().lower()
            existing = db.query(UserAccount).filter(UserAccount.email == email).first()
            if existing:
                if existing.role != "admin":
                    existing.role = "admin"
                    print(f"  UPDATED {email} -> role=admin")
                else:
                    print(f"  SKIP  {email} (already admin)")
                db.commit()
            else:
                admin = UserAccount(
                    email=email,
                    password_hash=_hash_password(admin_data["password"]),
                    role="admin",
                    name=admin_data["name"],
                    phone=admin_data["phone"],
                    status="actif",
                )
                db.add(admin)
                db.commit()
                print(f"  CREATE {email} (role=admin)")
    finally:
        db.close()


if __name__ == "__main__":
    # Load .env so ADMIN_EMAIL / ADMIN_PASSWORD are available
    _load_local_env_file(BASE_DIR / ".env")

    print("Seeding client users...")
    seed_users()

    print("\nEnsuring admin user...")
    ensure_admin()

    print("\nSeed complete.")
