"""
Swarm Login Module

Simple session-based authentication for the Swarm Controller.
"""

import hashlib
import secrets
import threading
from typing import Optional, Dict
from datetime import datetime, timedelta


# Simple in-memory session store (thread-safe)
_sessions: Dict[str, Dict] = {}
_sessions_lock = threading.Lock()

# Default credentials (can be overridden in config)
_default_username = "admin"
_default_password = "admin"  # Change in production!


def _hash_password(password: str, salt: str) -> str:
    """Hash a password with salt."""
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def generate_salt() -> str:
    """Generate a random salt for password hashing."""
    return secrets.token_hex(16)


def verify_credentials(username: str, password: str, config: dict) -> bool:
    """Verify username and password against config."""
    stored_username = config.get("login_username", _default_username)
    stored_password_hash = config.get("login_password_hash")
    stored_salt = config.get("login_salt")
    
    # If no hash configured, use default password
    if not stored_password_hash:
        if username == stored_username and password == _default_password:
            return True
        return False
    
    # Verify against stored hash
    if not stored_salt:
        return False
    
    return (username == stored_username and 
            _hash_password(password, stored_salt) == stored_password_hash)


def create_session(username: str) -> str:
    """Create a new session for a user."""
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=24)
    
    with _sessions_lock:
        _sessions[session_token] = {
            "username": username,
            "expires_at": expires_at,
            "created_at": datetime.now(),
        }
    
    return session_token


def verify_session(session_token: str) -> Optional[str]:
    """Verify a session token and return username if valid."""
    with _sessions_lock:
        session = _sessions.get(session_token)
        if not session:
            return None
        
        if datetime.now() > session["expires_at"]:
            del _sessions[session_token]
            return None
        
        return session["username"]


def destroy_session(session_token: str) -> bool:
    """Destroy a session."""
    with _sessions_lock:
        if session_token in _sessions:
            del _sessions[session_token]
            return True
    return False


def hash_password_for_storage(password: str) -> tuple:
    """Hash a password for storage. Returns (hash, salt)."""
    salt = generate_salt()
    hashed = _hash_password(password, salt)
    return hashed, salt


def cleanup_expired_sessions():
    """Remove expired sessions (call periodically)."""
    now = datetime.now()
    with _sessions_lock:
        expired = [token for token, sess in _sessions.items() 
                   if now > sess["expires_at"]]
        for token in expired:
            del _sessions[token]
