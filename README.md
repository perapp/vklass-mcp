# vklass-mcp

A multi-user [Model Context Protocol](https://modelcontextprotocol.io/) server for Vklass
guardians, with data access and explicitly confirmed absence reporting. Every user authenticates
their **own Vklass account** with BankID as part of the standard
MCP OAuth flow. Each OAuth subject maps directly to one Vklass user ID; there is no shared login,
global MCP token or administrator password.

The MCP protocol surface is designed like a first-party remote MCP server. The Vklass integration is
necessarily unofficial because Vklass does not publish a guardian API; its web endpoints can change.

## MCP and identity model

- One Streamable HTTP endpoint: `/mcp`.
- OAuth 2.1 authorization code flow with S256 PKCE.
- OAuth Authorization Server Metadata and RFC 9728 Protected Resource Metadata.
- Dynamic Client Registration for compatible MCP clients.
- Rotating access and refresh tokens, revocation, scopes and RFC 8707 resource indicators.
- The OAuth authorization page starts the Göteborg Vklass BankID QR flow.
- After login, Vklass `appData.userId` is transformed into a stable server-local pseudonymous OAuth
  subject using the state key; the raw Vklass user ID is not stored in OAuth grants.
- Each subject receives its own Vklass session, SQLite cache, synchronization tasks and encrypted
  state directory. Data queries can never select another subject's database.
- Raw OAuth access/refresh/code values are SHA-256 hashed in SQLite. Registered client metadata,
  including client secrets, is encrypted with the server state key.
- When the upstream Vklass session expires, all grants for that Vklass subject are revoked so MCP
  clients receive a standard 401 and restart the BankID authorization flow.

MCP clients connect only to:

```text
https://vklass.example.com/mcp
```

A compatible client discovers OAuth, opens the browser, asks the user to approve BankID, and stores
its own tokens. Different users and clients use the same URL but receive different OAuth subjects.

The server uses separate least-privilege OAuth scopes: `vklass.read` for cached and live queries,
and optional `vklass.write` for absence reporting. Existing read-only clients remain read-only;
they must re-register and authorize the write scope before they can submit reports.

## Security

- The only exposed Vklass mutation is guardian absence reporting. Leave, messages, schedule changes,
  deletion and other mutations are not exposed.
- Absence tools require `vklass.write` plus `confirm=true`, use a fresh Vklass anti-forgery token,
  restrict the child to the authenticated guardian's current wards, and honor school date limits.
  New clients receive read-only scope by default and must explicitly request write access.
- An interrupted submission returns `outcome_unknown`; clients must inspect Vklass rather than
  retrying automatically and risking a duplicate report.
- BankID approval is always performed by the account owner in a browser.
- Vklass cookies and OAuth secrets are never returned through MCP or logs.
- Göteborg SAML and BankID form/redirect hosts are strictly allow-listed.
- Vklass content is treated as untrusted data, never as instructions.
- The container runs without root or capabilities and uses a read-only root filesystem.
- Production OAuth requires a public **HTTPS** origin. The container port binds to loopback for a TLS
  reverse proxy and must not be published directly.

If this service is offered to other parents, the operator becomes responsible for personal data.
Provide clear retention/deletion terms, protected backups, incident handling and an operator contact.
Users should also understand that their MCP client may send tool results to its model provider.

## Implemented Vklass coverage

| Feature | Support |
|---|---|
| Göteborg guardian BankID QR | OAuth authorization UI |
| Per-user session restore, rotation and keepalive | Implemented |
| Children/wards | Normalized |
| Teacher news and **veckobrev** | Normalized/searchable |
| Calendar, lessons, homework, tests and assignments | Normalized per child |
| Omsorgsschema, including planned and actual attendance times | Normalized per child |
| Automatic weekly reports | Normalized separately from teacher veckobrev |
| Meals and notification count | Normalized |
| Study courses, judgements and grades | Normalized per child |
| Study and absence overview | Plain-text snapshots |
| Report absence today or for a date/time period | Implemented; explicit confirmation required |
| Class list | Disabled to avoid unrelated children |
| News attachments | Metadata only |
| Messages, documents, development talks | Endpoint mapping pending |
| Leave requests and all other write operations | Not exposed |

## MCP tools

- `vklass_capabilities`, `vklass_status`, `vklass_sync_now`
- `vklass_list_children`
- `vklass_report_absence_today`, `vklass_report_absence_period` (`vklass.write`)
- `vklass_list_weekly_letters`, `vklass_get_weekly_letter`
- `vklass_list_news`, `vklass_get_news_article`
- `vklass_list_calendar`, `vklass_list_assignments`, `vklass_list_care_schedule`
- `vklass_list_automatic_weekly_reports`
- `vklass_get_meals`, `vklass_get_notifications`
- `vklass_list_study_courses`, `vklass_get_feature_snapshot`, `vklass_search`

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# For localhost only:
sed -i 's#https://vklass.example.com#http://127.0.0.1:8000#' .env
sed -i 's#VKLASS_STATE_KEY_FILE=.*#VKLASS_STATE_KEY=development-state-key-change-me#' .env
uv sync --all-groups
uv run pytest
uv run vklass-mcp
```

Connect a development MCP client to `http://127.0.0.1:8000/mcp`. Do not use HTTP on a LAN or the
internet.

## Podman and systemd

```bash
make build
make install-quadlet
$EDITOR ~/.config/vklass-mcp/server.env
systemctl --user start vklass-mcp.service
journalctl --user -u vklass-mcp.service -f
```

The installer creates only one Podman secret: `vklass-mcp-state-key`. OAuth clients and users create
their own credentials through the protocol. Version 0.2 deliberately refuses to start when legacy
single-user `vklass.db*` or `session.json.fernet` files remain at the data root; migrate them or
securely remove the complete legacy set before deployment.

Runtime locations:

```text
~/.config/vklass-mcp/server.env
~/.local/share/vklass-mcp/oauth.db
~/.local/share/vklass-mcp/users/<sha256-of-vklass-user-id>/
~/.config/containers/systemd/vklass-mcp.container
```

The Quadlet binds `127.0.0.1:8787`. Put Caddy or another TLS reverse proxy in front of it:

```caddyfile
vklass.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Set both `VKLASS_PUBLIC_BASE_URL=https://vklass.example.com` and
`VKLASS_ALLOWED_HOSTS=vklass.example.com,localhost:*,127.0.0.1:*`. The public URL is the OAuth
issuer and cannot be changed without requiring clients to authorize again.

For the user service to survive logout:

```bash
loginctl enable-linger "$USER"
```

### Production deployment on `perd25`

`deploy/perd25/` builds locally, transfers the image through SSH to the dedicated `perapp` account,
installs a hardened Quadlet in that account's rootless Podman store, and attaches it only to the
private `perapp` network without publishing a host port. The independent
[`perapp-edge`](https://gitlab.com/perapp/perapp-edge) Caddy service owns public TLS and routes
`https://vklass.perapp.dev` to the container alias.

```bash
make build
./deploy/perd25/deploy.sh
```

Set `VKLASS_DEPLOY_ACTIVATE=0` to transfer the image and install the Quadlet without starting the
service during a coordinated migration. The public OAuth issuer is independent of the service
account and internal port.

Back up `~/.local/share/vklass-mcp/` together with `~/.config/vklass-mcp/state-key`; losing the key
disconnects every user and makes encrypted sessions and OAuth client registrations unreadable.

## Operations

- Health: `GET /healthz`
- OAuth metadata: `GET /.well-known/oauth-authorization-server`
- Protected resource metadata: `GET /.well-known/oauth-protected-resource/mcp`
- OAuth revocation: `POST /revoke`
- SQLite and encrypted sessions must be backed up together with the state key.
- OAuth grants can be revoked through `/revoke`; local data deletion is currently an operator-assisted
  action so an MCP read token cannot trigger destructive account management.
- BankID authorization transactions are intentionally process-local; run one application worker.
- Built-in per-peer rate limits, global authorization limits, concurrent BankID slots and a resident
  service cap provide backstops. Apply stricter distributed limits at the TLS edge for public use.
- Keep the state key stable and backed up. Rotation requires a planned migration of encrypted client
  metadata, user sessions and pseudonymous OAuth subjects; replacing it directly disconnects users.

## Attribution

The Göteborg BankID flow is adapted from the MIT-licensed
[Kaptensanders/vklass](https://github.com/Kaptensanders/vklass). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
