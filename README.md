# Odoo MCP server

A Model Context Protocol server that talks to a self-hosted Odoo instance
over its standard XML-RPC external API. Tools are built on live
introspection (`ir.model`, `fields_get`, `read_group`) rather than one
hardcoded tool per module, so newly installed apps — including custom ones —
show up automatically.

## Tools and permission tiers

Every tool carries standard MCP annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`) so an MCP client can offer real per-tool consent —
"always allow", "ask every time", or "never" — rather than one blanket
switch for the whole server. In Claude, this is what populates the
per-tool permission controls in the connector's settings.

| Tool | Tier | `readOnlyHint` | `destructiveHint` | Server-side failsafe |
|---|---|---|---|---|
| `context` | Read | true | false | always on |
| `list_models` | Read | true | false | always on |
| `describe_model` | Read | true | false | always on |
| `search_read` | Read | true | false | always on |
| `read_records` | Read | true | false | always on |
| `aggregate` | Read | true | false | always on |
| `create_record` | Write | false | false | `MCP_ENABLE_WRITE=true` |
| `update_record` | Write | false | true | `MCP_ENABLE_WRITE=true` |
| `delete_record` | Delete | false | true | `MCP_ENABLE_DELETE=true` |

**Two layers of consent, both have to agree.** The server-side env vars
(`MCP_ENABLE_WRITE`, `MCP_ENABLE_DELETE`) are an infrastructure failsafe —
they exist so a leaked API key or a careless client can't silently mutate
or delete data on a server nobody meant to expose that way. The *primary*
permission surface is meant to be your MCP client's own per-tool controls,
driven by the annotations above. Set the client side the way you actually
want to work day to day (e.g. reads always allowed, writes ask every time,
delete never) — the server flags are the backstop underneath that, not a
replacement for it.

`update_record` is flagged `destructiveHint: true` even though it's
technically an update, not a delete — it overwrites existing field values
in place with no undo, which is the same risk profile MCP's spec treats as
destructive.

## Required environment variables

| Variable | Example | Notes |
|---|---|---|
| `ODOO_URL` | `https://odoo-production-2cb7.up.railway.app` | No trailing slash needed |
| `ODOO_DB` | `chaldea_mcp` | The database name you created |
| `ODOO_USERNAME` | `admin` | The login, not the display name |
| `ODOO_API_KEY` | (generated in Odoo) | Preferred over a raw password — see below |
| `ODOO_PASSWORD` | — | Only used if `ODOO_API_KEY` isn't set |
| `MCP_ENABLE_WRITE` | `false` | Gates `create_record`/`update_record`. Set to `true` only for a least-privilege Odoo user |
| `MCP_ENABLE_DELETE` | `false` | Gates `delete_record` independently of `MCP_ENABLE_WRITE` — you can allow writes while still blocking deletion outright |
| `PORT` | (set automatically by Railway) | Don't set manually on Railway |

### Generating an API key instead of using the admin password

In Odoo: click your avatar (top right) → **My Profile** → **Account
Security** tab → **New API Key**. Use that value for `ODOO_API_KEY`. This
avoids putting the actual login password in an environment variable, and
lets you revoke MCP access later without changing the admin password.

## Running locally

```bash
pip install -r requirements.txt
export ODOO_URL=https://odoo-production-2cb7.up.railway.app
export ODOO_DB=chaldea_mcp
export ODOO_USERNAME=admin
export ODOO_API_KEY=your-key-here
python server.py
```

Server listens on `http://localhost:8000` (streamable HTTP transport).

## Deploying to Railway (same project as Odoo)

This isn't a Docker-image deploy like the Odoo/Postgres services — it needs
to build from this source, so it goes in via a GitHub repo:

1. Push this folder to a new GitHub repo (or a subfolder of an existing one).
2. In the `odoo-mcp-hackathon` Railway project, add a new service from that
   GitHub repo.
3. Set the environment variables above on that service. For `ODOO_URL`, use
   the Odoo service's Railway-generated public domain — or, since both
   services live in the same project, you can reference it privately once
   the MCP server also needs to resolve container-to-container (ask before
   assuming that's wired correctly; XML-RPC over the private network works
   the same as the public URL, just faster and without leaving Railway).
4. Generate a public domain for the MCP service once it deploys
   successfully, so an MCP client (Claude, or anything else) can reach it.

## Connecting a client

Point an MCP-compatible client at the deployed service's
`/mcp` endpoint (streamable HTTP transport). Exact connection syntax
depends on the client — for Claude specifically, this is added as a custom
connector using the service's URL.

## A note on the `mcp` package version

This code targets `mcp>=2.0`, which renamed `FastMCP` to `MCPServer`
(`mcp.server.mcpserver.MCPServer`) and changed how the HTTP transport is
started. Verified against `mcp==2.1.1` — all 8 tools import and register
correctly with proper JSON schemas generated from the type hints.

If you're following an older tutorial that references `FastMCP` or
`mcp.server.fastmcp`, that's the pre-2.0 API and won't match this code.
The Python MCP SDK moves fast; if a future version changes this again, run
`pip show mcp` and check
<https://github.com/modelcontextprotocol/python-sdk> for the current
`run_streamable_http_async` signature.
