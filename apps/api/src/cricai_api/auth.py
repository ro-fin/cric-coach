"""Role-based access (US-L3): Parent (admin), Coach (review), Player (read-only views).

LAN token auth: each role has one bearer token from settings. Constant-time
comparison; empty configured tokens can never authenticate.
"""

import secrets
from collections.abc import Callable
from typing import Annotated

from cricai_data.enums import Role
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cricai_api.settings import Settings

_bearer = HTTPBearer(auto_error=False)


def resolve_role(settings: Settings, token: str) -> Role | None:
    for role, expected in (
        (Role.PARENT, settings.parent_token),
        (Role.COACH, settings.coach_token),
        (Role.PLAYER, settings.player_token),
    ):
        if expected and secrets.compare_digest(token, expected):
            return role
    return None


def get_current_role(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Role:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    settings: Settings = request.app.state.settings
    role = resolve_role(settings, credentials.credentials)
    if role is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return role


def require_roles(*allowed: Role) -> Callable[[Role], Role]:
    def dependency(role: Annotated[Role, Depends(get_current_role)]) -> Role:
        if role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires one of: {sorted(allowed)}")
        return role

    return dependency
