"""
Minimal Odoo XML-RPC client.

Talks to the two endpoints every Odoo install exposes, regardless of which
modules are installed:

    /xmlrpc/2/common   -> authentication, server version
    /xmlrpc/2/object   -> execute_kw, the single method that reaches every
                          model, every field, and every business action in
                          the database

Nothing here is Odoo-version-specific and nothing hardcodes a model name.

This server talks to up to two separate Odoo databases at once — "initial"
(the original self-hosted instance) and "transfer" (wherever data is being
migrated to). Each is just a set of environment variables sharing a prefix;
OdooClient itself has no idea which instance it is.
"""

import os
import xmlrpc.client
from typing import Any


class OdooClient:
    def __init__(self, env_prefix: str = "ODOO") -> None:
        """
        env_prefix: the environment variable prefix for this instance.
        "ODOO" reads ODOO_URL/ODOO_DB/... (the original, unprefixed
        variables — kept exactly as-is so the existing self-hosted
        connection needs no Railway config change). "ODOO_TRANSFER" reads
        ODOO_TRANSFER_URL/ODOO_TRANSFER_DB/... for a second instance.
        """
        self.env_prefix = env_prefix
        self.url = os.environ[f"{env_prefix}_URL"].rstrip("/")
        self.db = os.environ[f"{env_prefix}_DB"]
        self.username = os.environ[f"{env_prefix}_USERNAME"]
        # Prefer an API key (Settings > Users > Administrator > API Keys) over
        # a raw password — same auth call, just a different credential.
        self.password = (
            os.environ.get(f"{env_prefix}_API_KEY") or os.environ[f"{env_prefix}_PASSWORD"]
        )

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._uid: int | None = None

    def authenticate(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(self.db, self.username, self.password, {})
            if not self._uid:
                raise RuntimeError(
                    f"Odoo authentication failed for {self.env_prefix} — check "
                    f"{self.env_prefix}_URL, {self.env_prefix}_DB, "
                    f"{self.env_prefix}_USERNAME and "
                    f"{self.env_prefix}_API_KEY/{self.env_prefix}_PASSWORD."
                )
        return self._uid

    def version(self) -> dict[str, Any]:
        return self._common.version()

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        uid = self.authenticate()
        return self._models.execute_kw(
            self.db, uid, self.password, model, method, args, kwargs or {}
        )


_clients: dict[str, OdooClient] = {}


def get_client() -> OdooClient:
    """The original, always-present instance — reads ODOO_URL etc. unchanged."""
    return _get_cached("ODOO")


def get_transfer_client() -> OdooClient:
    """The second instance, if configured — reads ODOO_TRANSFER_URL etc."""
    return _get_cached("ODOO_TRANSFER")


def _get_cached(env_prefix: str) -> OdooClient:
    if env_prefix not in _clients:
        _clients[env_prefix] = OdooClient(env_prefix)
    return _clients[env_prefix]


def transfer_configured() -> bool:
    return "ODOO_TRANSFER_URL" in os.environ
