import asyncio
import logging
import jwt
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


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
                logger.warning("OAuth rejected: invalid scope claim type")
                return None
            return AccessToken(
                token=token, client_id=claims.get("client_id", claims["sub"]),
                subject=claims["sub"], scopes=scope.split(), expires_at=claims["exp"],
            )
        except jwt.ExpiredSignatureError:
            logger.warning("OAuth rejected: token expired")
        except jwt.InvalidAudienceError:
            logger.warning("OAuth rejected: audience does not match OAUTH_AUDIENCE")
        except jwt.InvalidIssuerError:
            logger.warning("OAuth rejected: issuer does not match OAUTH_ISSUER")
        except jwt.PyJWKClientError:
            logger.warning("OAuth rejected: signing key lookup failed; check JWKS URL and outbound connectivity")
        except (jwt.PyJWTError, ValueError, TypeError):
            logger.warning("OAuth rejected: invalid token signature, format, or required claims")
        return None

    async def verify_token(self, token: str):
        return await asyncio.to_thread(self._verify, token)


class OwnerVerifier:
    """Authorize a single verified owner; provider roles/scopes are unnecessary."""

    def __init__(self, verifier, owner_subject, registry):
        self.verifier = verifier
        self.owner_subject = owner_subject
        self.registry = registry

    async def verify_token(self, token: str):
        access = await self.verifier.verify_token(token)
        if access is None:
            return None
        if access.subject != self.owner_subject:
            logger.warning("OAuth rejected: signed-in user does not match OWNER_SUBJECT")
            return None
        await asyncio.to_thread(self.registry.initialize)
        return access
