import asyncio
import jwt
from mcp.server.auth.provider import AccessToken


class JWTVerifier:
    """Validate access tokens from a configured external OAuth authorization server."""

    def __init__(self, issuer: str, audience: str, jwks_url: str):
        self.issuer = issuer
        self.audience = audience
        self.keys = jwt.PyJWKClient(jwks_url, timeout=5)

    def _verify(self, token: str):
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, options={"require": ["exp", "iss", "aud", "sub"]},
            )
            scope = claims.get("scope", "")
            if not isinstance(scope, str):
                return None
            return AccessToken(
                token=token, client_id=claims.get("client_id", claims["sub"]),
                subject=claims["sub"], scopes=scope.split(), expires_at=claims["exp"],
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            return None

    async def verify_token(self, token: str):
        return await asyncio.to_thread(self._verify, token)


class ProjectVerifier:
    """Apply project grants and provision only after complete authorization."""

    def __init__(self, verifier, project, registry):
        self.verifier = verifier
        self.project = project
        self.registry = registry

    async def verify_token(self, token: str):
        access = await self.verifier.verify_token(token)
        if access is None:
            return None
        scope = f"project:{self.project}"
        if "projects:manage" in access.scopes and scope not in access.scopes:
            access = access.model_copy(update={"scopes": [*access.scopes, scope]})
        if "skills:read" in access.scopes and scope in access.scopes:
            await asyncio.to_thread(self.registry.initialize)
        return access
