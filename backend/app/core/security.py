from __future__ import annotations

from enum import StrEnum
from typing import Any

import jwt
from fastapi import Depends, Header, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import ForbiddenError, UnauthorizedError


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_ROLE_RANK = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMIN: 3}


class CurrentUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1, max_length=128)
    roles: frozenset[Role] = Field(min_length=1)

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    def has_role(self, required: Role) -> bool:
        return any(_ROLE_RANK[role] >= _ROLE_RANK[required] for role in self.roles)


class OIDCJWTVerifier:
    def __init__(self, settings: Settings, jwks_client: PyJWKClient | None = None) -> None:
        self.settings = settings
        self._jwks_client = jwks_client

    @property
    def jwks_client(self) -> PyJWKClient:
        if self._jwks_client is None:
            if not self.settings.oidc_jwks_url:
                raise UnauthorizedError("OIDC JWKS 未配置")
            self._jwks_client = PyJWKClient(self.settings.oidc_jwks_url)
        return self._jwks_client

    def verify(self, token: str) -> CurrentUser:
        if not all(
            (self.settings.oidc_issuer, self.settings.oidc_audience, self.settings.oidc_jwks_url)
        ):
            raise UnauthorizedError("OIDC 认证配置不完整")
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.settings.oidc_algorithm_list,
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "sub"]},
                leeway=30,
            )
        except PyJWTError as exc:
            raise UnauthorizedError() from exc
        return user_from_claims(claims)


def parse_roles(value: Any) -> frozenset[Role]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        raise UnauthorizedError("Token 缺少有效角色")
    roles = set()
    for item in items:
        try:
            roles.add(Role(str(item).strip()))
        except ValueError:
            continue
    if not roles:
        raise UnauthorizedError("Token 缺少有效角色")
    return frozenset(roles)


def user_from_claims(claims: dict[str, Any]) -> CurrentUser:
    try:
        return CurrentUser(subject=claims["sub"], roles=parse_roles(claims.get("roles")))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, UnauthorizedError):
            raise
        raise UnauthorizedError("Token Subject 无效") from exc


def authenticate(
    authorization: str | None,
    dev_roles: str | None,
    settings: Settings,
    verifier: OIDCJWTVerifier,
) -> CurrentUser:
    if not authorization:
        raise UnauthorizedError("缺少 Authorization 请求头")
    scheme, separator, credential = authorization.partition(" ")
    if not separator or not credential:
        raise UnauthorizedError("Authorization 格式无效")
    if scheme.lower() == "dev":
        if not settings.dev_auth_enabled or settings.is_production:
            raise UnauthorizedError("Dev Auth 未启用")
        return CurrentUser(subject=credential, roles=parse_roles(dev_roles or "viewer"))
    if scheme.lower() != "bearer":
        raise UnauthorizedError("不支持的认证方式")
    return verifier.verify(credential)


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    dev_roles: str | None = Header(default=None, alias="X-Dev-Roles"),
) -> CurrentUser:
    settings: Settings = request.app.state.settings
    verifier = getattr(request.app.state, "oidc_verifier", None)
    if verifier is None:
        verifier = OIDCJWTVerifier(settings)
        request.app.state.oidc_verifier = verifier
    return authenticate(authorization, dev_roles, settings, verifier)


def require_roles(*roles: Role):
    if not roles:
        raise ValueError("at least one role is required")

    def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not any(user.has_role(required) for required in roles):
            raise ForbiddenError()
        return user

    return dependency


require_viewer = require_roles(Role.VIEWER)
require_operator = require_roles(Role.OPERATOR)
require_admin = require_roles(Role.ADMIN)
