"""
Minimal Odoo XML-RPC client.

Talks to the two endpoints every Odoo install exposes, regardless of which
modules are installed:

    /xmlrpc/2/common   -> authentication, server version
    /xmlrpc/2/object   -> execute_kw, the single method that reaches every
                          model, every field, and every business action in
                          the database

Nothing here is Odoo-version-specific and nothing hardcodes a model name.
"""

import os
import xmlrpc.client
from typing import Any


class OdooClient:
    def __init__(self) -> None:
        self.url = os.environ["ODOO_URL"].rstrip("/")
        self.db = os.environ["ODOO_DB"]
        self.username = os.environ["ODOO_USERNAME"]
        # Prefer an API key (Settings > Users > Administrator > API Keys) over
        # a raw password — same auth call, just a different credential.
        self.password = os.environ.get("ODOO_API_KEY") or os.environ["ODOO_PASSWORD"]

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._uid: int | None = None

    def authenticate(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(self.db, self.username, self.password, {})
            if not self._uid:
                raise RuntimeError(
                    "Odoo authentication failed — check ODOO_URL, ODOO_DB, "
                    "ODOO_USERNAME and ODOO_API_KEY/ODOO_PASSWORD."
                )
        return self._uid

    def version(self) -> dict[str, Any]:
        return self._common.version()

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        uid = self.authenticate()
        return self._models.execute_kw(
            self.db, uid, self.password, model, method, args, kwargs or {}
        )


_client: OdooClient | None = None


def get_client() -> OdooClient:
    global _client
    if _client is None:
        _client = OdooClient()
    return _client
