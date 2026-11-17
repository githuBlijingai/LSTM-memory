from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import (
    CurrentUser,
    OIDCJWTVerifier,
    Role,
    authenticate,
    require_roles,
    user_from_claims,
)
from app.models import EventMemory, Visibility
from app.repositories.permissions import can_view_memory, event_candidate_predicate


def test_dev_auth_and_role_hierarchy() -> None:
    settings = Settings(_env_file=None, dev_auth_enabled=True)
    user = authenticate("Dev alice", "viewer,operator", settings, OIDCJWTVerifier(settings))

    assert user.subject == "alice"
    assert user.has_role(Role.VIEWER)
    assert user.has_role(Role.OPERATOR)
    assert not user.has_role(Role.ADMIN)
    assert require_roles(Role.OPERATOR)(user) is user
    with pytest.raises(ForbiddenError):
        require_roles(Role.ADMIN)(user)


def test_dev_auth_is_rejected_in_production_even_with_explicit_object() -> None:
    settings = Settings(_env_file=None, environment="production")

    with pytest.raises(UnauthorizedError):
        authenticate("Dev alice", "admin", settings, OIDCJWTVerifier(settings))


def test_oidc_verifier_requires_complete_configuration_and_valid_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = Settings(_env_file=None)
    with pytest.raises(UnauthorizedError):
        OIDCJWTVerifier(incomplete).verify("token")

    settings = Settings(
        _env_file=None,
        oidc_jwks_url="https://identity.example.test/jwks",
        oidc_issuer="https://identity.example.test/",
        oidc_audience="memory-api",
    )
    fake_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key="public-key")
    )
    verifier = OIDCJWTVerifier(settings, jwks_client=fake_client)
    captured: dict[str, Any] = {}

    def fake_decode(token: str, key: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"sub": "alice", "roles": ["viewer"]}

    monkeypatch.setattr("app.core.security.jwt.decode", fake_decode)
    user = verifier.verify("signed-token")

    assert user == CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
    assert captured["audience"] == "memory-api"
    assert captured["options"] == {"require": ["exp", "sub"]}
    with pytest.raises(UnauthorizedError):
        user_from_claims({"sub": "alice", "roles": ["unknown"]})


def test_owner_visibility_rules_and_candidate_filter() -> None:
    alice = CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
    admin = CurrentUser(subject="admin", roles=frozenset({Role.ADMIN}))
    own = SimpleNamespace(owner_id="alice", visibility=Visibility.PRIVATE)
    other = SimpleNamespace(owner_id="bob", visibility=Visibility.PRIVATE)
    shared = SimpleNamespace(owner_id="bob", visibility="shared")

    assert can_view_memory(alice, own)
    assert not can_view_memory(alice, other)
    assert can_view_memory(alice, shared)
    assert can_view_memory(admin, other)

    predicate = str(
        event_candidate_predicate(alice).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "event_memory.status = 'active'" in predicate
    assert "event_memory.embedding IS NOT NULL" in predicate
    assert "event_memory.deleted_at IS NULL" in predicate
    assert "event_memory.owner_id = 'alice'" in predicate
    assert EventMemory.__tablename__ == "event_memory"
