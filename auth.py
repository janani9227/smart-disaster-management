import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime

AUTH_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
CODE_EXPIRATION_SECONDS = 15 * 60  # 15 minutes
SESSION_EXPIRATION_SECONDS = 7 * 24 * 3600  # 7 days


def get_db_connection():
    conn = sqlite3.connect(AUTH_DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    name TEXT,
                    is_verified INTEGER DEFAULT 0,
                    verification_code TEXT,
                    code_expires_at REAL,
                    created_at TEXT,
                    last_login TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
    finally:
        conn.close()


def hash_password(password: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,
    )
    return key.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    new_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(stored_hash, new_hash)


def is_valid_email(email: str) -> bool:
    email = (email or "").strip().lower()
    return bool(re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email))


def generate_verification_code() -> str:
    # 6-digit numeric verification code
    return f"{secrets.randbelow(900000) + 100000}"


def create_session(user_id: int, conn=None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + SESSION_EXPIRATION_SECONDS
    should_close = False
    if conn is None:
        conn = get_db_connection()
        should_close = True
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, expires_at),
        )
        return token
    finally:
        if should_close:
            conn.commit()
            conn.close()


def register_user(email: str, password: str, name: str = None):
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return False, {"error": "Please enter a valid email address."}
    if not password or len(password) < 6:
        return False, {"error": "Password must be at least 6 characters long."}

    name = (name or "").strip() or email.split("@")[0].title()
    code = generate_verification_code()
    code_expires = time.time() + CODE_EXPIRATION_SECONDS
    pwd_hash, salt = hash_password(password)
    now_iso = datetime.now().isoformat()

    conn = get_db_connection()
    try:
        with conn:
            row = conn.execute("SELECT id, is_verified FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                if row["is_verified"] == 1:
                    return False, {"error": "An account with this email already exists. Please sign in."}
                else:
                    # Update password and generate fresh code
                    conn.execute(
                        """UPDATE users SET password_hash = ?, salt = ?, name = ?, 
                           verification_code = ?, code_expires_at = ? WHERE id = ?""",
                        (pwd_hash, salt, name, code, code_expires, row["id"]),
                    )
                    return True, {
                        "message": "Verification code resent to your email.",
                        "email": email,
                        "dev_code": code,
                        "requires_verification": True,
                    }

            conn.execute(
                """INSERT INTO users (email, password_hash, salt, name, is_verified, verification_code, code_expires_at, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (email, pwd_hash, salt, name, code, code_expires, now_iso),
            )
            return True, {
                "message": "Account created! Please verify your email with the 6-digit code.",
                "email": email,
                "dev_code": code,
                "requires_verification": True,
            }
    except Exception as error:
        return False, {"error": f"Registration failed: {str(error)}"}
    finally:
        conn.close()


def verify_user_code(email: str, code: str):
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not email or not code:
        return False, {"error": "Email and 6-digit verification code are required."}

    conn = get_db_connection()
    try:
        with conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                return False, {"error": "No account found with this email. Please register first."}

            if row["is_verified"] == 1:
                # Already verified, generate session and let in
                token = create_session(row["id"], conn=conn)
                return True, {
                    "message": "Email is already verified.",
                    "token": token,
                    "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": "User"},
                }

            if not row["verification_code"] or row["verification_code"] != code:
                return False, {"error": "Invalid verification code. Please check and try again."}

            if row["code_expires_at"] and time.time() > row["code_expires_at"]:
                return False, {"error": "Verification code has expired. Please request a new code."}

            # Code is valid, mark as verified
            conn.execute(
                "UPDATE users SET is_verified = 1, verification_code = NULL, code_expires_at = NULL, last_login = ? WHERE id = ?",
                (datetime.now().isoformat(), row["id"]),
            )
            token = create_session(row["id"], conn=conn)
            return True, {
                "message": "Email verified successfully! Welcome to Smart Disaster Management.",
                "token": token,
                "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": "User"},
            }
    except Exception as error:
        return False, {"error": f"Verification error: {str(error)}"}
    finally:
        conn.close()


def resend_verification_code(email: str):
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return False, {"error": "Please provide a valid email address."}

    conn = get_db_connection()
    try:
        with conn:
            row = conn.execute("SELECT id, is_verified FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                return False, {"error": "No account found with this email."}
            if row["is_verified"] == 1:
                return False, {"error": "Email is already verified. You can log in directly."}

            code = generate_verification_code()
            code_expires = time.time() + CODE_EXPIRATION_SECONDS
            conn.execute(
                "UPDATE users SET verification_code = ?, code_expires_at = ? WHERE id = ?",
                (code, code_expires, row["id"]),
            )
            return True, {
                "message": "A new verification code has been generated.",
                "email": email,
                "dev_code": code,
            }
    except Exception as error:
        return False, {"error": f"Could not resend code: {str(error)}"}
    finally:
        conn.close()


def login_user(email: str, password: str):
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return False, {"error": "Please enter both email and password."}

    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            return False, {"error": "No account found with this email. Please create an account."}

        if not verify_password(password, row["password_hash"], row["salt"]):
            return False, {"error": "Incorrect password. Please try again."}

        if row["is_verified"] != 1:
            # Generate new code for them to verify
            code = generate_verification_code()
            code_expires = time.time() + CODE_EXPIRATION_SECONDS
            with conn:
                conn.execute(
                    "UPDATE users SET verification_code = ?, code_expires_at = ? WHERE id = ?",
                    (code, code_expires, row["id"]),
                )
            return False, {
                "error": "Your email is not verified yet. We have sent a verification code to your email.",
                "requires_verification": True,
                "email": email,
                "dev_code": code,
            }

        # Update last login
        now_iso = datetime.now().isoformat()
        with conn:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso, row["id"]))

        token = create_session(row["id"])
        return True, {
            "message": "Signed in successfully.",
            "token": token,
            "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": "User"},
        }
    except Exception as error:
        return False, {"error": f"Login failed: {str(error)}"}
    finally:
        conn.close()


def get_user_from_token(token: str):
    if not token:
        return None
    now = time.time()
    conn = get_db_connection()
    try:
        row = conn.execute(
            """SELECT u.id, u.email, u.name, u.is_verified, s.expires_at 
               FROM sessions s 
               JOIN users u ON s.user_id = u.id 
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, now),
        ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "email": row["email"], "name": row["name"], "role": "User"}
    finally:
        conn.close()


def logout_user(token: str):
    if not token:
        return True
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return True
    finally:
        conn.close()


# Initialize database tables on import
init_auth_db()
