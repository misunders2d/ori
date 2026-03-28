import pytest

def test_auth_import():
    try:
        from app.core.auth import auth_service
        assert auth_service is not None
    except ImportError as e:
        pytest.fail(f"Could not import auth_service: {e}")
