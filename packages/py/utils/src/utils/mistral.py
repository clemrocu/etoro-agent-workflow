"""The app's Mistral SDK clients: one acting as the app itself, one acting for the caller served.

Background work, a deployed worker's included, uses ``mistral_client()``. It authenticates with the
service-account token in ``MISTRAL_SA_TOKEN_PATH`` where the platform mounts one, and with
``MISTRAL_API_KEY`` otherwise. Every client keeps the SDK's own HTTP clients, so the standard
``HTTPS_PROXY``, ``NO_PROXY`` and ``SSL_CERT_FILE`` environment variables route and verify its
traffic.
"""

import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from env.mistral import env
from mistralai.client import Mistral
from mistralai.client.types import UNSET, OptionalNullable
from mistralai.client.utils.retries import RetryConfig

# The public API, where the SDK goes when ``MISTRAL_BASE_URL`` is unset.
_PUBLIC_API = "https://api.mistral.ai"


class MistralNotConfiguredError(RuntimeError):
    """Raised when a Mistral client is requested but nothing can authenticate its calls."""


class CallerCredentials(NamedTuple):
    """Where to reach the Mistral API as the caller being served, and the token to do it with."""

    server_url: str
    token: str


CallerCredentialProvider = Callable[[], CallerCredentials]

_provider: CallerCredentialProvider | None = None


def install_caller_credentials(provider: CallerCredentialProvider | None) -> None:
    """Make ``provider`` the source of the credential that work on a caller's behalf spends."""
    global _provider
    _provider = provider


def caller_credentials_installed() -> bool:
    """Whether anything can supply a caller credential, without asking it for one."""
    return _provider is not None


def _sa_token_path() -> str | None:
    # Only the platform sets this, on a deployed worker.
    return os.environ.get("MISTRAL_SA_TOKEN_PATH") or None


def _token_file(path: str) -> Callable[[], str]:
    # Read on every request: the platform rotates the token file under a running worker.
    return lambda: Path(path).read_text(encoding="utf-8").strip()


def mistral_api_url() -> str:
    """The Mistral API root the app calls as itself: ``MISTRAL_BASE_URL``, or the public API."""
    return (env.mistral_base_url or _PUBLIC_API).rstrip("/")


def _new_client(
    api_key: str | Callable[[], str],
    server_url: str | None,
    retry_config: OptionalNullable[RetryConfig] = UNSET,
) -> Mistral:
    # The one construction of the SDK client, so every client the app builds is built the same way.
    return Mistral(api_key=api_key, server_url=server_url, retry_config=retry_config)


def mistral_client(
    *,
    server_url: str | None = None,
    retry_config: OptionalNullable[RetryConfig] = UNSET,
) -> Mistral:
    """A client acting as the app itself, against ``MISTRAL_BASE_URL``.

    It authenticates with the token in ``MISTRAL_SA_TOKEN_PATH`` when that is set, and with
    ``MISTRAL_API_KEY`` otherwise. A missing key is not an error here: the API rejects the first
    call with a 401.
    """
    token_path = _sa_token_path()
    api_key = _token_file(token_path) if token_path else env.mistral_api_key or ""
    return _new_client(
        api_key,
        env.mistral_base_url if server_url is None else server_url,
        retry_config,
    )
