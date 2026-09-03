import logging
import hashlib
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class LocalAuthAdapter:
    def __init__(self) -> None:
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        from adapters.sqlite_db import sqlite_db
        await sqlite_db._ensure_initialized()
        self._initialized = True
        logger.info("LocalAuthAdapter initialized successfully")

    def _hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def _verify_password(self, password: str, stored_hash: str) -> bool:
        return self._hash_password(password) == stored_hash

    async def login(self, email: str, password: str) -> dict:
        await self._ensure_initialized()
        from adapters.sqlite_db import sqlite_db
        # Normalize email: strip spaces, case-insensitive (fixes frontend caps/space 401)
        email_norm = (email or "").strip().lower()
        # Use manual scan for case-insensitive match (sqlite query is case-sensitive)
        all_users = await sqlite_db.get_all("Users") or []
        users = [u for u in all_users if str(u.get("email","")).strip().lower() == email_norm]
        # fallback to direct query if scan empty (keeps old behavior for exact match)
        if not users:
            users = await sqlite_db.query("Users", {"email": email})
            if not users and email_norm != email:
                users = await sqlite_db.query("Users", {"email": email_norm})
        if not users:
            logger.info(f"Login failed: email not found {email} -> {email_norm}")
            raise ValueError("Invalid credentials")

        user = users[0]
        stored_hash = user.get("password_hash", "")
        if not stored_hash or not self._verify_password(password, stored_hash):
            logger.info(f"Login failed: password mismatch for {email_norm}")
            raise ValueError("Invalid credentials")

        return {
            "access_token": "",  # JWT will be created by auth_service
            "user_id": user.get("ROWID") or user.get("user_id"),
        }

    async def logout(self, token: str) -> None:
        # JWT is stateless, no server-side logout needed
        pass

    async def reset_password(self, email: str) -> dict:
        await self._ensure_initialized()
        from adapters.sqlite_db import sqlite_db
        users = await sqlite_db.query("Users", {"email": email})
        if users:
            # In a real implementation, send reset email
            # For testing, generate a temporary password and log it
            import string
            import random
            import hashlib
            temporary_password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
            password_hash = hashlib.sha256(temporary_password.encode()).hexdigest()
            user = users[0]
            user_id = user.get("ROWID") or user.get("user_id")
            await sqlite_db.update("Users", user_id, {
                "password_hash": password_hash,
                "updated_at": datetime.utcnow().isoformat()
            })
            logger.debug("Temporary password generated for %s: %s", email, temporary_password)
        else:
            logger.debug("Password reset requested for non-existent email: %s", email)

    async def verify_token(self, token: str) -> dict:
        # Token verification is handled by JWT middleware
        return {"valid": True}

    async def get_user_details(self, user_id: str) -> Optional[dict]:
        await self._ensure_initialized()
        from adapters.sqlite_db import sqlite_db
        user = await sqlite_db.get("Users", user_id)
        return user

    async def signup(self, email: str, password: str, display_name: str) -> dict:
        await self._ensure_initialized()
        from adapters.sqlite_db import sqlite_db
        email_norm = (email or "").strip().lower()
        all_users = await sqlite_db.get_all("Users") or []
        existing = [u for u in all_users if str(u.get("email","")).strip().lower() == email_norm]
        if existing:
            raise ValueError("Email already registered")

        from utils.helpers import generate_uuid

        user_id = generate_uuid()
        now = datetime.utcnow().isoformat()
        password_hash = self._hash_password(password)

        user_data = {
            "ROWID": user_id,
            "user_id": user_id,
            "display_name": display_name,
            "email": email,
            "password_hash": password_hash,
            "role": "officer",
            "badge_number": "",
            "phone": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }

        await sqlite_db.insert("Users", user_data)
        return {"user_id": user_id, "email": email, "display_name": display_name}


local_auth = LocalAuthAdapter()