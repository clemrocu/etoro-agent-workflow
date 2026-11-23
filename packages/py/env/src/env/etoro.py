"""Settings for the eToro agent (agent portfolio token, notification topic).

Values live in the environment, the repo-root `.env` or the Mistral workspace
secrets (fields marked `WorkspaceSecret`). Never commit a real token.
"""

from typing import Annotated

from env._base import BaseEnv
from env._mistral_secrets import WorkspaceSecret


class Env(BaseEnv):
    # "real": the agent portfolio user token trades the REAL execution routes
    # (scopes etoro-public:trade.real:read/write). "demo": the /demo routes.
    etoro_account: str = "real"
    etoro_base_url: str = "https://public-api.etoro.com/api/v1"
    # Bearer token = the eToro agent portfolio user token (visible once at creation).
    etoro_token: Annotated[str | None, WorkspaceSecret()] = None
    # Alternative auth: key pair x-user-key / x-api-key (own account).
    etoro_user_key: Annotated[str | None, WorkspaceSecret()] = None
    etoro_api_key: Annotated[str | None, WorkspaceSecret()] = None
    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: Annotated[str | None, WorkspaceSecret()] = None

    def require(self, name: str) -> str:
        """The value of setting ``name``, or an error naming the variable to set."""
        value = getattr(self, name)
        if not value:
            raise RuntimeError(f"{name.upper()} is not set")
        return value

    def has_auth(self) -> bool:
        return bool(self.etoro_token or (self.etoro_user_key and self.etoro_api_key))


env = Env()
