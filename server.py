"""
Odoo MCP server — dynamic introspection edition.

Instead of writing one tool per Odoo model (what most community MCP servers
do), this exposes a small, fixed set of tools that ask Odoo's own metadata
tables (`ir.model`, `fields_get`) what exists at runtime. Install a new
module in Odoo — CRM, Website, a custom booking module, anything — and it's
immediately reachable through the same tools, no server code change needed.

Read tools are always on. Write tools are OFF by default (matching the
posture of Odoo's own native Enterprise MCP module) and only activate when
MCP_ENABLE_WRITE=true is set in the environment. Treat that flag like a
loaded weapon — only point it at a least-privilege Odoo user.

Two instances, two independent tool sets
-----------------------------------------
This server can talk to up to two separate Odoo databases: the original
("initial") and a second one named "transfer" — e.g. a database being
migrated to. The nine tools below are unprefixed and always target
"initial", exactly as before this file grew a second instance: nothing
about the original tool names, behaviour, or environment variables changed,
so an existing deployment or client needs no reconfiguration to keep
working.

A second, transfer_-prefixed copy of the same nine tools targets the
"transfer" instance, reading ODOO_TRANSFER_* environment variables instead
of ODOO_*. Those tools only register at all if ODOO_TRANSFER_URL is set —
so deploying this file with no transfer instance configured yet is a no-op
for anyone already using the server today.

Write and delete are gated per instance, independently:
MCP_ENABLE_WRITE / MCP_ENABLE_DELETE for "initial",
MCP_ENABLE_TRANSFER_WRITE / MCP_ENABLE_TRANSFER_DELETE for "transfer" — so
enabling bulk write/delete against a transfer instance (e.g. to wipe and
re-populate it) never has to touch, or even imply anything about, the
write/delete posture of the original.
"""

import asyncio
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from odoo_client import OdooClient, get_client, get_transfer_client, transfer_configured

mcp = MCPServer("odoo-mcp")

# Two independent infrastructure-level switches per instance, one per risk
# tier. These are a server-side failsafe, NOT the primary permission
# mechanism — the primary mechanism is the read_only_hint / destructive_hint
# annotation on each tool below, which lets an MCP client (Claude's
# connector settings, for instance) offer per-tool "always allow / ask every
# time / never" controls. A client that respects those hints will ask
# before ever reaching a write or delete tool; these env vars exist so a
# compromised or careless client still can't silently mutate data on a
# server that was only ever meant to be read from.
WRITE_ENABLED = os.environ.get("MCP_ENABLE_WRITE", "false").lower() == "true"
DELETE_ENABLED = os.environ.get("MCP_ENABLE_DELETE", "false").lower() == "true"
TRANSFER_WRITE_ENABLED = os.environ.get("MCP_ENABLE_TRANSFER_WRITE", "false").lower() == "true"
TRANSFER_DELETE_ENABLED = os.environ.get("MCP_ENABLE_TRANSFER_DELETE", "false").lower() == "true"

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE_CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
WRITE_UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)
DESTRUCTIVE_DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)

# A handful of noisy/technical models nobody wants surfaced in a "what
# models exist" call. This is a display filter only — describe_model and
# search_read still work on anything if you ask by exact name.
_NOISY_MODEL_PREFIXES = ("ir.", "base.", "bus.", "report.")


# --------------------------------------------------------------------------
# Shared implementations. Each takes the already-resolved client, so the
# two tools per name (unprefixed / transfer_-prefixed) are thin wrappers
# that differ only in which client they pass and what MCP sees as the tool
# name and docstring.
# --------------------------------------------------------------------------


def _context(client: OdooClient) -> dict:
    uid = client.authenticate()
    version = client.version()
    user = client.execute_kw(
        "res.users", "read", [[uid]], {"fields": ["name", "login", "lang", "tz", "company_id"]}
    )
    return {"uid": uid, "user": user[0] if user else None, "odoo_version": version}


def _list_models(client: OdooClient, search: str, include_technical: bool) -> list[dict]:
    domain = []
    if search:
        domain = ["|", ("name", "ilike", search), ("model", "ilike", search)]
    models = client.execute_kw(
        "ir.model", "search_read", [domain], {"fields": ["model", "name", "transient"]}
    )
    if not include_technical:
        models = [
            m for m in models
            if not any(m["model"].startswith(p) for p in _NOISY_MODEL_PREFIXES)
            and not m["transient"]
        ]
    return [{"model": m["model"], "label": m["name"]} for m in models]


def _describe_model(client: OdooClient, model: str) -> dict:
    fields = client.execute_kw(
        model,
        "fields_get",
        [],
        {"attributes": ["string", "type", "required", "relation", "help"]},
    )
    return {
        name: {
            "label": info.get("string"),
            "type": info.get("type"),
            "required": info.get("required", False),
            "relation": info.get("relation"),
            "help": info.get("help") or None,
        }
        for name, info in fields.items()
    }


def _search_read(
    client: OdooClient,
    model: str,
    domain: list | None,
    fields: list[str] | None,
    limit: int,
    offset: int,
    order: str,
) -> list[dict]:
    kwargs: dict = {"limit": limit, "offset": offset}
    if fields:
        kwargs["fields"] = fields
    if order:
        kwargs["order"] = order
    return client.execute_kw(model, "search_read", [domain or []], kwargs)


def _read_records(client: OdooClient, model: str, ids: list[int], fields: list[str] | None) -> list[dict]:
    kwargs = {"fields": fields} if fields else {}
    return client.execute_kw(model, "read", [ids], kwargs)


def _aggregate(
    client: OdooClient,
    model: str,
    domain: list | None,
    group_by: list[str] | None,
    fields: list[str] | None,
) -> list[dict]:
    return client.execute_kw(model, "read_group", [domain or [], fields or [], group_by or []])


def _create_record(client: OdooClient, model: str, values: dict, enabled: bool, flag_name: str) -> dict:
    if not enabled:
        return {
            "error": f"Write access is disabled on this MCP server's infrastructure "
            f"switch ({flag_name}=false). This is separate from your client's own "
            f"per-tool permission setting — both have to allow it."
        }
    new_id = client.execute_kw(model, "create", [values])
    return {"id": new_id}


def _update_record(
    client: OdooClient, model: str, ids: list[int], values: dict, enabled: bool, flag_name: str
) -> dict:
    if not enabled:
        return {
            "error": f"Write access is disabled on this MCP server's infrastructure "
            f"switch ({flag_name}=false). This is separate from your client's own "
            f"per-tool permission setting — both have to allow it."
        }
    ok = client.execute_kw(model, "write", [ids, values])
    return {"success": ok}


def _delete_record(client: OdooClient, model: str, ids: list[int], enabled: bool, flag_name: str) -> dict:
    if not enabled:
        return {
            "error": f"Delete access is disabled on this MCP server's infrastructure "
            f"switch ({flag_name}=false). This is separate from the write flag and "
            f"from your client's own per-tool permission setting — all relevant "
            f"layers have to allow it."
        }
    ok = client.execute_kw(model, "unlink", [ids])
    return {"success": ok}


# --------------------------------------------------------------------------
# "initial" instance — unprefixed tool names, unchanged from before this
# file supported a second instance.
# --------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def context() -> dict:
    """
    Call this first. Returns who the MCP server is authenticated as, the
    Odoo version it's talking to, and basic server info — establishes
    context before any other tool call. Targets the "initial" instance.
    """
    return _context(get_client())


@mcp.tool(annotations=READ_ONLY)
def _debug_env_names() -> dict:
    """
    TEMPORARY diagnostic — remove once the ODOO_API_KEY-not-visible issue
    is understood. Returns only the *names* of environment variables this
    process can see (never values), plus, for the ones this bug is
    actually about, whether each is present and its length — a length of
    0 versus "key not found at all" are different bugs with different
    fixes, and neither reveals the value.
    """
    watched = ["ODOO_API_KEY", "ODOO_PASSWORD", "ODOO_URL", "ODOO_DB", "ODOO_USERNAME"]
    return {
        "env_var_names": sorted(os.environ.keys()),
        "watched": {
            name: {"present": name in os.environ, "length": len(os.environ.get(name, ""))}
            for name in watched
        },
    }


@mcp.tool(annotations=READ_ONLY)
def list_models(search: str = "", include_technical: bool = False) -> list[dict]:
    """
    List Odoo models the authenticated user can access, on the "initial"
    instance. Read live from ir.model, so newly installed apps (or custom
    modules) show up automatically without any change here.

    Args:
        search: optional case-insensitive substring to filter by model
            name or technical name (e.g. "sale", "crm", "booking").
        include_technical: if False (default), hides internal/technical
            models (ir.*, base.*, bus.*, report.*) to keep the list focused
            on business models.
    """
    return _list_models(get_client(), search, include_technical)


@mcp.tool(annotations=READ_ONLY)
def describe_model(model: str) -> dict:
    """
    Describe every field on an Odoo model on the "initial" instance:
    technical name, label, type, whether it's required, and — for
    relational fields — which model it points to. Fetched live via Odoo's
    own fields_get() call.

    Args:
        model: technical model name, e.g. "res.partner", "sale.order",
            "crm.lead". Use list_models() first if you don't know it.
    """
    return _describe_model(get_client(), model)


@mcp.tool(annotations=READ_ONLY)
def search_read(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    order: str = "",
) -> list[dict]:
    """
    Search and read records from any Odoo model on the "initial" instance,
    in one call.

    Args:
        model: technical model name, e.g. "sale.order", "product.product".
        domain: Odoo domain filter as a list of triples, e.g.
            [["state", "=", "sale"], ["amount_total", ">", 1000]].
            Omit or pass [] to match everything.
        fields: field names to return (use describe_model to see what's
            available). Omit to get Odoo's default display fields.
        limit: max records to return. Keep this small — this is a live
            business database, not a bulk export tool.
        offset: pagination offset.
        order: Odoo order string, e.g. "create_date desc".
    """
    return _search_read(get_client(), model, domain, fields, limit, offset, order)


@mcp.tool(annotations=READ_ONLY)
def read_records(model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
    """
    Read specific records by ID on the "initial" instance. Use this when
    you already know the IDs (e.g. from a previous search_read) and just
    need fresh or additional field values.
    """
    return _read_records(get_client(), model, ids, fields)


@mcp.tool(annotations=READ_ONLY)
def aggregate(
    model: str,
    domain: list | None = None,
    group_by: list[str] | None = None,
    fields: list[str] | None = None,
) -> list[dict]:
    """
    Grouped aggregation for simple analytics on the "initial" instance —
    totals, counts, and averages broken down by one or more fields. This
    is Odoo's read_group under the hood, the same mechanism its own
    reporting views use.

    Args:
        model: technical model name, e.g. "sale.order.line".
        domain: filter, same format as search_read.
        group_by: fields to group by, e.g. ["partner_id"] or
            ["state", "date_order:month"].
        fields: fields to aggregate, e.g. ["amount_total:sum",
            "id:count"]. Odoo infers the aggregate function per field's
            type if you just pass the bare field name.
    """
    return _aggregate(get_client(), model, domain, group_by, fields)


@mcp.tool(annotations=WRITE_CREATE)
def create_record(model: str, values: dict) -> dict:
    """
    Create a new record on the "initial" instance. This is a WRITE tool —
    not read-only, not destructive (nothing existing is overwritten).
    Gated server-side by MCP_ENABLE_WRITE as a failsafe; the primary
    control is your MCP client's per-tool permission setting for this
    tool.
    """
    return _create_record(get_client(), model, values, WRITE_ENABLED, "MCP_ENABLE_WRITE")


@mcp.tool(annotations=WRITE_UPDATE)
def update_record(model: str, ids: list[int], values: dict) -> dict:
    """
    Update existing records on the "initial" instance. This is a WRITE
    tool, flagged destructive since it overwrites existing field values in
    place. Gated server-side by MCP_ENABLE_WRITE as a failsafe; the
    primary control is your MCP client's per-tool permission setting for
    this tool.
    """
    return _update_record(get_client(), model, ids, values, WRITE_ENABLED, "MCP_ENABLE_WRITE")


@mcp.tool(annotations=DESTRUCTIVE_DELETE)
def delete_record(model: str, ids: list[int]) -> dict:
    """
    Permanently delete one or more records on the "initial" instance. This
    is the DELETE tier — the highest-risk tool this server exposes, and
    irreversible. Gated server-side by MCP_ENABLE_DELETE independently of
    MCP_ENABLE_WRITE. The primary control is still your MCP client's
    per-tool permission setting for this specific tool.
    """
    return _delete_record(get_client(), model, ids, DELETE_ENABLED, "MCP_ENABLE_DELETE")


# --------------------------------------------------------------------------
# "transfer" instance — same nine tools, transfer_-prefixed, only
# registered at all if ODOO_TRANSFER_URL is set. A deployment with no
# transfer instance configured yet simply doesn't offer these.
# --------------------------------------------------------------------------

if transfer_configured():

    @mcp.tool(annotations=READ_ONLY)
    def transfer_context() -> dict:
        """Same as context(), against the "transfer" instance."""
        return _context(get_transfer_client())

    @mcp.tool(annotations=READ_ONLY)
    def transfer_list_models(search: str = "", include_technical: bool = False) -> list[dict]:
        """Same as list_models(), against the "transfer" instance."""
        return _list_models(get_transfer_client(), search, include_technical)

    @mcp.tool(annotations=READ_ONLY)
    def transfer_describe_model(model: str) -> dict:
        """Same as describe_model(), against the "transfer" instance."""
        return _describe_model(get_transfer_client(), model)

    @mcp.tool(annotations=READ_ONLY)
    def transfer_search_read(
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str = "",
    ) -> list[dict]:
        """Same as search_read(), against the "transfer" instance."""
        return _search_read(get_transfer_client(), model, domain, fields, limit, offset, order)

    @mcp.tool(annotations=READ_ONLY)
    def transfer_read_records(model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
        """Same as read_records(), against the "transfer" instance."""
        return _read_records(get_transfer_client(), model, ids, fields)

    @mcp.tool(annotations=READ_ONLY)
    def transfer_aggregate(
        model: str,
        domain: list | None = None,
        group_by: list[str] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Same as aggregate(), against the "transfer" instance."""
        return _aggregate(get_transfer_client(), model, domain, group_by, fields)

    @mcp.tool(annotations=WRITE_CREATE)
    def transfer_create_record(model: str, values: dict) -> dict:
        """
        Same as create_record(), against the "transfer" instance. Gated by
        MCP_ENABLE_TRANSFER_WRITE — independent of MCP_ENABLE_WRITE, which
        only ever governs the "initial" instance.
        """
        return _create_record(
            get_transfer_client(), model, values, TRANSFER_WRITE_ENABLED, "MCP_ENABLE_TRANSFER_WRITE"
        )

    @mcp.tool(annotations=WRITE_UPDATE)
    def transfer_update_record(model: str, ids: list[int], values: dict) -> dict:
        """
        Same as update_record(), against the "transfer" instance. Gated by
        MCP_ENABLE_TRANSFER_WRITE — independent of MCP_ENABLE_WRITE, which
        only ever governs the "initial" instance.
        """
        return _update_record(
            get_transfer_client(), model, ids, values, TRANSFER_WRITE_ENABLED, "MCP_ENABLE_TRANSFER_WRITE"
        )

    @mcp.tool(annotations=DESTRUCTIVE_DELETE)
    def transfer_delete_record(model: str, ids: list[int]) -> dict:
        """
        Same as delete_record(), against the "transfer" instance. Gated by
        MCP_ENABLE_TRANSFER_DELETE — independent of MCP_ENABLE_DELETE,
        which only ever governs the "initial" instance. Enabling this can
        never delete anything on the "initial" instance; there is no
        shared switch between the two.
        """
        return _delete_record(
            get_transfer_client(), model, ids, TRANSFER_DELETE_ENABLED, "MCP_ENABLE_TRANSFER_DELETE"
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # mcp>=2.0: MCPServer.run_streamable_http_async starts its own ASGI
    # server (host/port passed directly, no separate uvicorn.run call).
    asyncio.run(mcp.run_streamable_http_async(host="0.0.0.0", port=port))
