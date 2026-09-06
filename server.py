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
"""

import asyncio
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from odoo_client import get_client

mcp = MCPServer("odoo-mcp")

# Two independent infrastructure-level switches, one per risk tier. These
# are a server-side failsafe, NOT the primary permission mechanism — the
# primary mechanism is the read_only_hint / destructive_hint annotation on
# each tool below, which lets an MCP client (Claude's connector settings,
# for instance) offer per-tool "always allow / ask every time / never"
# controls. A client that respects those hints will ask before ever
# reaching a write or delete tool; these env vars exist so a compromised or
# careless client still can't silently mutate data on a server that was
# only ever meant to be read from.
WRITE_ENABLED = os.environ.get("MCP_ENABLE_WRITE", "false").lower() == "true"
DELETE_ENABLED = os.environ.get("MCP_ENABLE_DELETE", "false").lower() == "true"

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE_CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
WRITE_UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)
DESTRUCTIVE_DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)

# A handful of noisy/technical models nobody wants surfaced in a "what
# models exist" call. This is a display filter only — describe_model and
# search_read still work on anything if you ask by exact name.
_NOISY_MODEL_PREFIXES = ("ir.", "base.", "bus.", "report.")


@mcp.tool(annotations=READ_ONLY)
def context() -> dict:
    """
    Call this first. Returns who the MCP server is authenticated as, the
    Odoo version it's talking to, and basic server info — establishes
    context before any other tool call.
    """
    client = get_client()
    uid = client.authenticate()
    version = client.version()
    user = client.execute_kw(
        "res.users", "read", [[uid]], {"fields": ["name", "login", "lang", "tz", "company_id"]}
    )
    return {"uid": uid, "user": user[0] if user else None, "odoo_version": version}


@mcp.tool(annotations=READ_ONLY)
def list_models(search: str = "", include_technical: bool = False) -> list[dict]:
    """
    List Odoo models the authenticated user can access. This is read live
    from ir.model, so newly installed apps (or custom modules) show up
    automatically without any change here.

    Args:
        search: optional case-insensitive substring to filter by model
            name or technical name (e.g. "sale", "crm", "booking").
        include_technical: if False (default), hides internal/technical
            models (ir.*, base.*, bus.*, report.*) to keep the list focused
            on business models.
    """
    client = get_client()
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


@mcp.tool(annotations=READ_ONLY)
def describe_model(model: str) -> dict:
    """
    Describe every field on an Odoo model: technical name, label, type,
    whether it's required, and — for relational fields — which model it
    points to. This is fetched live via Odoo's own fields_get() call, the
    same introspection mechanism Odoo's native MCP module uses, so it's
    always accurate to whatever is actually installed.

    Args:
        model: technical model name, e.g. "res.partner", "sale.order",
            "crm.lead". Use list_models() first if you don't know it.
    """
    client = get_client()
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
    Search and read records from any Odoo model in one call.

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
    client = get_client()
    kwargs: dict = {"limit": limit, "offset": offset}
    if fields:
        kwargs["fields"] = fields
    if order:
        kwargs["order"] = order
    return client.execute_kw(model, "search_read", [domain or []], kwargs)


@mcp.tool(annotations=READ_ONLY)
def read_records(model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
    """
    Read specific records by ID. Use this when you already know the IDs
    (e.g. from a previous search_read) and just need fresh or additional
    field values.
    """
    client = get_client()
    kwargs = {"fields": fields} if fields else {}
    return client.execute_kw(model, "read", [ids], kwargs)


@mcp.tool(annotations=READ_ONLY)
def aggregate(
    model: str,
    domain: list | None = None,
    group_by: list[str] | None = None,
    fields: list[str] | None = None,
) -> list[dict]:
    """
    Grouped aggregation for simple analytics — totals, counts, and
    averages broken down by one or more fields. This is Odoo's read_group
    under the hood, the same mechanism its own reporting views use.

    Args:
        model: technical model name, e.g. "sale.order.line".
        domain: filter, same format as search_read.
        group_by: fields to group by, e.g. ["partner_id"] or
            ["state", "date_order:month"].
        fields: fields to aggregate, e.g. ["amount_total:sum",
            "id:count"]. Odoo infers the aggregate function per field's
            type if you just pass the bare field name.
    """
    client = get_client()
    return client.execute_kw(
        model,
        "read_group",
        [domain or [], fields or [], group_by or []],
    )


@mcp.tool(annotations=WRITE_CREATE)
def create_record(model: str, values: dict) -> dict:
    """
    Create a new record. This is a WRITE tool — not read-only, not
    destructive (nothing existing is overwritten). Gated server-side by
    MCP_ENABLE_WRITE as a failsafe; the primary control is your MCP
    client's per-tool permission setting for this tool.
    """
    if not WRITE_ENABLED:
        return {
            "error": "Write access is disabled on this MCP server's "
            "infrastructure switch (MCP_ENABLE_WRITE=false). This is separate "
            "from your client's own per-tool permission setting — both have "
            "to allow it."
        }
    client = get_client()
    new_id = client.execute_kw(model, "create", [values])
    return {"id": new_id}


@mcp.tool(annotations=WRITE_UPDATE)
def update_record(model: str, ids: list[int], values: dict) -> dict:
    """
    Update existing records. This is a WRITE tool, flagged destructive
    since it overwrites existing field values in place. Gated server-side
    by MCP_ENABLE_WRITE as a failsafe; the primary control is your MCP
    client's per-tool permission setting for this tool.
    """
    if not WRITE_ENABLED:
        return {
            "error": "Write access is disabled on this MCP server's "
            "infrastructure switch (MCP_ENABLE_WRITE=false). This is separate "
            "from your client's own per-tool permission setting — both have "
            "to allow it."
        }
    client = get_client()
    ok = client.execute_kw(model, "write", [ids, values])
    return {"success": ok}


@mcp.tool(annotations=DESTRUCTIVE_DELETE)
def delete_record(model: str, ids: list[int]) -> dict:
    """
    Permanently delete one or more records. This is the DELETE tier — the
    highest-risk tool this server exposes, and irreversible. Gated
    server-side by MCP_ENABLE_DELETE independently of MCP_ENABLE_WRITE, so
    an operator can allow create/update on this server while still
    blocking deletion outright. The primary control is still your MCP
    client's per-tool permission setting for this specific tool.
    """
    if not DELETE_ENABLED:
        return {
            "error": "Delete access is disabled on this MCP server's "
            "infrastructure switch (MCP_ENABLE_DELETE=false). This is "
            "separate from MCP_ENABLE_WRITE and from your client's own "
            "per-tool permission setting — all relevant layers have to allow it."
        }
    client = get_client()
    ok = client.execute_kw(model, "unlink", [ids])
    return {"success": ok}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # mcp>=2.0: MCPServer.run_streamable_http_async starts its own ASGI
    # server (host/port passed directly, no separate uvicorn.run call).
    asyncio.run(mcp.run_streamable_http_async(host="0.0.0.0", port=port))
