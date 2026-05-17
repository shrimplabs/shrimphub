"""Unit tests for swarm/login.py"""
import threading
import pytest
from datetime import datetime, timedelta

# Reset modules before each test
@pytest.fixture(autouse=True)
def reset_login_module():
    """Reset the login module state before each test."""
    import swarm.login as login_mod
    login_mod._sessions = {}
    login_mod._initialized = False
    yield
    login_mod._sessions = {}


class TestPasswordHashing:
    def test_hash_password_produces_consistent_output(self):
        """Same password and salt should produce same hash."""
        from swarm.login import _hash_password, generate_salt
        
        salt = "test_salt_123"
        password = "my_secure_password"
        
        hash1 = _hash_password(password, salt)
        hash2 = _hash_password(password, salt)
        
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex is 64 chars

    def test_different_salts_produce_different_hashes(self):
        """Different salts should produce different hashes."""
        from swarm.login import _hash_password, generate_salt
        
        password = "same_password"
        salt1 = generate_salt()
        salt2 = generate_salt()
        
        hash1 = _hash_password(password, salt1)
        hash2 = _hash_password(password, salt2)
        
        assert hash1 != hash2

    def test_generate_salt_produces_unique_values(self):
        """Each salt generation should be unique."""
        from swarm.login import generate_salt
        
        salts = [generate_salt() for _ in range(10)]
        assert len(set(salts)) == 10


class TestCredentialVerification:
    def test_verify_credentials_default_works(self):
        """Default credentials should work with default username/password."""
        from swarm.login import verify_credentials
        
        config = {}
        
        # Default: admin/admin
        assert verify_credentials("admin", "admin", config) is True
        assert verify_credentials("admin", "wrong", config) is False
        assert verify_credentials("wrong", "admin", config) is False

    def test_verify_credentials_custom_config(self):
        """Custom credentials should work when configured."""
        from swarm.login import verify_credentials, hash_password_for_storage
        
        password_hash, salt = hash_password_for_storage("secret123")
        
        config = {
            "login_username": "user1",
            "login_password_hash": password_hash,
            "login_salt": salt
        }
        
        assert verify_credentials("user1", "secret123", config) is True
        assert verify_credentials("user1", "wrong", config) is False
        assert verify_credentials("wrong", "secret123", config) is False

    def test_verify_credentials_missing_config_returns_false(self):
        """Missing password hash should return False."""
        from swarm.login import verify_credentials
        
        config = {
            "login_username": "user1",
            # No login_password_hash
        }
        
        assert verify_credentials("user1", "anything", config) is False


class TestSessionManagement:
    def test_create_session_returns_token(self):
        """Creating a session should return a valid token."""
        from swarm.login import create_session
        
        token = create_session("testuser")
        
        assert token is not None
        assert len(token) > 20  # URL-safe token

    def test_verify_session_returns_username_for_valid_session(self):
        """Valid session should return username."""
        from swarm.login import create_session, verify_session
        
        token = create_session("testuser")
        username = verify_session(token)
        
        assert username == "testuser"

    def test_verify_session_returns_none_for_invalid_token(self):
        """Invalid token should return None."""
        from swarm.login import verify_session
        
        result = verify_session("invalid_token_12345")
        
        assert result is None

    def test_verify_session_returns_none_for_expired_session(self):
        """Expired session should return None."""
        from swarm.login import create_session, verify_session, _sessions, _sessions_lock
        
        token = create_session("testuser")
        
        # Manually expire the session
        with _sessions_lock:
            _sessions[token]["expires_at"] = datetime.now() - timedelta(hours=1)
        
        result = verify_session(token)
        
        assert result is None

    def test_destroy_session_removes_session(self):
        """Destroying a session should remove it."""
        from swarm.login import create_session, verify_session, destroy_session
        
        token = create_session("testuser")
        
        assert verify_session(token) == "testuser"
        
        destroy_session(token)
        
        assert verify_session(token) is None

    def test_destroy_session_returns_false_for_nonexistent(self):
        """Destroying non-existent session should return False."""
        from swarm.login import destroy_session
        
        result = destroy_session("nonexistent_token")
        
        assert result is False


class TestHashPasswordForStorage:
    def test_returns_tuple_of_hash_and_salt(self):
        """Function should return (hash, salt) tuple."""
        from swarm.login import hash_password_for_storage
        
        result = hash_password_for_storage("testpassword")
        
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert len(result[0]) == 64  # SHA256 hex
        assert len(result[1]) == 32  # 16 bytes hex

    def test_different_calls_produce_different_results(self):
        """Each call should produce unique hash/salt."""
        from swarm.login import hash_password_for_storage
        
        results = [hash_password_for_storage("samepassword") for _ in range(5)]
        hashes = [r[0] for r in results]
        salts = [r[1] for r in results]
        
        assert len(set(hashes)) == 5
        assert len(set(salts)) == 5


class TestCleanupExpiredSessions:
    def test_removes_expired_sessions(self):
        """Should remove only expired sessions."""
        from swarm.login import (
            create_session, verify_session, 
            cleanup_expired_sessions, _sessions, _sessions_lock
        )
        
        # Create a valid session
        token1 = create_session("user1")
        
        # Create and manually expire a session
        token2 = create_session("user2")
        with _sessions_lock:
            _sessions[token2]["expires_at"] = datetime.now() - timedelta(hours=1)
        
        # Cleanup
        cleanup_expired_sessions()
        
        # First should still work
        assert verify_session(token1) == "user1"
        # Second should be expired
        assert verify_session(token2) is None
