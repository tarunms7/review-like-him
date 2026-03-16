"""GitHub App JWT generation and installation token management."""

import logging
import time
from pathlib import Path

import httpx
import jwt

logger = logging.getLogger("review-bot")

GITHUB_API_BASE = "https://api.github.com"

_TOKEN_CACHE_MAX_ENTRIES = 1000


def mask_token(token: str) -> str:
    """Mask a token for safe logging, showing only the last 4 characters.

    Returns:
        Token masked as '****<last4chars>'.
    """
    if not token:
        return "****"
    return f"****{token[-4:]}"


class GitHubAppAuth:
    """GitHub App authentication: JWT generation and installation token caching.

    Auth flow: App private key → JWT (10 min expiry) → installation access token
    (1 hour, cached with early refresh).
    """

    def __init__(self, app_id: str, private_key_path: str) -> None:
        self._app_id = app_id
        self._private_key = Path(private_key_path).read_text()
        self._token_cache: dict[int, tuple[str, float]] = {}

    def _evict_oldest_cache_entry(self) -> None:
        """Remove the oldest cache entry (by expiry time) when cache is full."""
        if len(self._token_cache) >= _TOKEN_CACHE_MAX_ENTRIES:
            oldest_id = min(self._token_cache, key=lambda k: self._token_cache[k][1])
            del self._token_cache[oldest_id]
            logger.debug("Evicted oldest token cache entry for installation %d", oldest_id)

    def invalidate(self, installation_id: int) -> None:
        """Explicitly clear the cached token for an installation.

        Args:
            installation_id: The installation whose cached token to remove.
        """
        self._token_cache.pop(installation_id, None)
        logger.debug("Invalidated cached token for installation %d", installation_id)

    def get_jwt(self) -> str:
        """Generate a JWT for GitHub App authentication.

        The JWT is valid for 10 minutes per GitHub's requirements.
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued-at with 60s clock drift allowance
            "exp": now + (10 * 60),  # 10 minute expiry
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """Get an installation access token, using cache when possible.

        Tokens are cached and refreshed 5 minutes before expiry.
        On failure, any stale cache entry for this installation is deleted.
        """
        cached = self._token_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 300:  # refresh 5 min early
                return token

        token_jwt = self.get_jwt()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {token_jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            # Clear stale cache entry before re-raising
            self._token_cache.pop(installation_id, None)
            raise

        token = data["token"]
        # GitHub tokens expire in 1 hour; cache with that assumption
        expires_at = time.time() + 3600

        # Evict oldest entry if cache is full before adding new one
        if installation_id not in self._token_cache:
            self._evict_oldest_cache_entry()
        self._token_cache[installation_id] = (token, expires_at)

        logger.debug(
            "Refreshed installation token for installation %d (%s)",
            installation_id,
            mask_token(token),
        )
        return token

    async def create_token_client(self, installation_id: int) -> httpx.AsyncClient:
        """Create an authenticated httpx.AsyncClient for a given installation.

        The client includes Authorization and Accept headers for GitHub API v3.
        """
        token = await self.get_installation_token(installation_id)
        logger.debug(
            "Creating authenticated client for installation %d (%s)",
            installation_id,
            mask_token(token),
        )
        return httpx.AsyncClient(
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            base_url=GITHUB_API_BASE,
        )
