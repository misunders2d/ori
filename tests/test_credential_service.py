"""OriCredentialService — round-trip via vault."""
from unittest.mock import MagicMock

import pytest

from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_tool import AuthConfig

from app.runtime.credential_service import OriCredentialService, _scheme_name, _vault_key


def _make_auth_config(scheme_name: str = "google"):
    """Build a minimal AuthConfig with an attribute-friendly scheme."""
    cfg = MagicMock(spec=AuthConfig)
    cfg.auth_scheme = MagicMock()
    cfg.auth_scheme.name = scheme_name
    cfg.exchanged_auth_credential = None
    cfg.raw_auth_credential = None
    return cfg


def _make_callback_context(user_id: str | None = "tg_42"):
    cb = MagicMock()
    cb.state = MagicMock()
    cb.state.to_dict.return_value = {"user_id": user_id} if user_id else {}
    return cb


def test_scheme_name_uses_scheme_name_attr():
    cfg = _make_auth_config(scheme_name="github")
    assert _scheme_name(cfg) == "github"


def test_scheme_name_falls_back_to_type_name():
    cfg = MagicMock(spec=AuthConfig)
    # No name attributes available
    cfg.auth_scheme = type("MyCustomScheme", (), {})()
    out = _scheme_name(cfg)
    # Falls back to class name
    assert out == "MyCustomScheme"


def test_vault_key_namespace():
    cfg = _make_auth_config(scheme_name="google")
    cb = _make_callback_context(user_id="tg_42")
    from app.runtime.credential_service import _user_id_from
    key = _vault_key(cfg, _user_id_from(cb))
    assert key == "OAUTH:google:tg_42"


def test_user_id_falls_back_to_global():
    from app.runtime.credential_service import _user_id_from
    cb = _make_callback_context(user_id=None)
    assert _user_id_from(cb) == "_global"


@pytest.mark.asyncio
async def test_save_then_load_round_trip(monkeypatch):
    """save_credential writes to vault; load_credential reads it back as AuthCredential."""
    fake_vault: dict[str, str] = {}
    monkeypatch.setattr(
        "app.runtime.credential_service.vault.get",
        lambda key, default="": fake_vault.get(key, default),
    )
    monkeypatch.setattr(
        "app.runtime.credential_service.vault.set",
        lambda key, value: fake_vault.__setitem__(key, value),
    )

    svc = OriCredentialService()
    cfg = _make_auth_config(scheme_name="google")
    cred = AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(access_token="tok-abc-123", refresh_token="ref-xyz"),
    )
    cfg.exchanged_auth_credential = cred
    cb = _make_callback_context(user_id="tg_42")

    await svc.save_credential(cfg, cb)
    assert "OAUTH:google:tg_42" in fake_vault

    loaded = await svc.load_credential(cfg, cb)
    assert loaded is not None
    assert loaded.auth_type == AuthCredentialTypes.OAUTH2
    assert loaded.oauth2.access_token == "tok-abc-123"
    assert loaded.oauth2.refresh_token == "ref-xyz"


@pytest.mark.asyncio
async def test_load_missing_returns_none(monkeypatch):
    monkeypatch.setattr(
        "app.runtime.credential_service.vault.get",
        lambda key, default="": "",
    )
    svc = OriCredentialService()
    cfg = _make_auth_config(scheme_name="github")
    cb = _make_callback_context(user_id="tg_42")
    out = await svc.load_credential(cfg, cb)
    assert out is None


@pytest.mark.asyncio
async def test_load_corrupted_returns_none(monkeypatch):
    monkeypatch.setattr(
        "app.runtime.credential_service.vault.get",
        lambda key, default="": "{not-json",
    )
    svc = OriCredentialService()
    cfg = _make_auth_config(scheme_name="google")
    cb = _make_callback_context()
    out = await svc.load_credential(cfg, cb)
    assert out is None


@pytest.mark.asyncio
async def test_save_no_credential_is_noop(monkeypatch):
    """save_credential with neither raw nor exchanged credential just logs and returns."""
    saves: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.runtime.credential_service.vault.set",
        lambda key, value: saves.append((key, value)),
    )
    svc = OriCredentialService()
    cfg = _make_auth_config(scheme_name="google")
    cfg.exchanged_auth_credential = None
    cfg.raw_auth_credential = None
    cb = _make_callback_context()
    await svc.save_credential(cfg, cb)
    assert saves == []
