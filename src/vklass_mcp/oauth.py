"""MCP-native OAuth 2.1 authorization server with per-user subjects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from vklass_mcp.config import Settings

SCOPES = ["vklass.read"]


class StoredRefreshToken(RefreshToken):
    resource: str | None = None
    pair_id: str
    family_id: str
    replayed: bool = False


class StoredAccessToken(AccessToken):
    pair_id: str
    family_id: str


@dataclass
class PendingAuthorization:
    flow_id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float
    status: str = "pending"
    qr_seed: str | None = None
    qr_revision: int = 0
    error: str | None = None
    redirect_url: str | None = None
    authenticated_subject: str | None = None
    authenticated_name: str | None = None
    pending_cookie: str | None = field(default=None, repr=False)
    issued_code_hash: str | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    login_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def expired(self) -> bool:
        return self.created_at + 600 < time.time()


class OAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, StoredRefreshToken, StoredAccessToken]
):
    """Persist OAuth grants while keeping raw bearer and refresh tokens out of SQLite."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.data_dir / "oauth.db"
        self.public_base_url = settings.public_base_url.rstrip("/")
        self.issuer_url = f"{self.public_base_url}/"
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._flows: dict[str, PendingAuthorization] = {}
        state_key = settings.resolved_state_key or ""
        self._fernet = Fernet(_fernet_key(state_key))
        self._subject_key = hashlib.sha256(f"vklass-subject:{state_key}".encode()).digest()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                encrypted_json BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_codes (
                token_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                data_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                token_hash TEXT PRIMARY KEY,
                token_type TEXT NOT NULL CHECK(token_type IN ('access','refresh')),
                pair_id TEXT NOT NULL,
                family_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                resource TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS oauth_tokens_pair ON oauth_tokens(pair_id);
            CREATE INDEX IF NOT EXISTS oauth_tokens_subject ON oauth_tokens(subject);
            CREATE TABLE IF NOT EXISTS oauth_used_refresh (
                token_hash TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                resource TEXT,
                expires_at INTEGER NOT NULL
            );
            """
        )
        async with self._db.execute("PRAGMA table_info(oauth_codes)") as cursor:
            code_columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "subject" not in code_columns:
            await self._db.execute(
                "ALTER TABLE oauth_codes ADD COLUMN subject TEXT NOT NULL DEFAULT ''"
            )
        async with self._db.execute("PRAGMA table_info(oauth_tokens)") as cursor:
            token_columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "family_id" not in token_columns:
            await self._db.execute(
                "ALTER TABLE oauth_tokens ADD COLUMN family_id TEXT NOT NULL DEFAULT ''"
            )
            await self._db.execute("UPDATE oauth_tokens SET family_id=pair_id WHERE family_id='' ")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family_id)"
        )
        now = int(time.time())
        await self._db.execute("DELETE FROM oauth_codes WHERE expires_at<?", (now,))
        await self._db.execute("DELETE FROM oauth_tokens WHERE expires_at<?", (now,))
        await self._db.execute("DELETE FROM oauth_used_refresh WHERE expires_at<?", (now,))
        await self._db.commit()
        self.path.chmod(0o600)

    async def close(self) -> None:
        for flow in self._flows.values():
            if flow.login_task and not flow.login_task.done():
                flow.login_task.cancel()
        tasks = [flow.login_task for flow in self._flows.values() if flow.login_task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._flows.clear()
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("OAuth provider is not open")
        return self._db

    def subject_for_vklass_user(self, vklass_user_id: str) -> str:
        """Return a server-local pseudonymous OAuth subject for a Vklass user ID."""

        return hmac.new(
            self._subject_key, vklass_user_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self.db.execute(
            "SELECT encrypted_json FROM oauth_clients WHERE client_id=?", (client_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        try:
            payload = self._fernet.decrypt(bytes(row["encrypted_json"]))
            return OAuthClientInformationFull.model_validate_json(payload)
        except (InvalidToken, ValueError):
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        for redirect_uri in client_info.redirect_uris or []:
            _validate_redirect_uri(str(redirect_uri))
        requested = set((client_info.scope or "").split())
        if "vklass.read" not in requested or not requested.issubset(SCOPES):
            raise RegistrationError(
                "invalid_client_metadata", "client scope must be exactly vklass.read"
            )
        encrypted = self._fernet.encrypt(client_info.model_dump_json().encode("utf-8"))
        async with self._lock:
            try:
                async with self.db.execute("SELECT COUNT(*) AS count FROM oauth_clients") as cursor:
                    row = await cursor.fetchone()
                if row and int(row["count"]) >= self.settings.max_oauth_clients:
                    raise RegistrationError("unapproved_software_statement", "client limit reached")
                await self.db.execute(
                    """INSERT INTO oauth_clients(
                         client_id, encrypted_json, created_at
                       ) VALUES(?,?,?)""",
                    (client_info.client_id, encrypted, int(time.time())),
                )
                await self.db.commit()
            except aiosqlite.IntegrityError as error:
                await self.db.rollback()
                raise RegistrationError(
                    "invalid_client_metadata", "client_id already exists"
                ) from error
            except Exception:
                await self.db.rollback()
                raise

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.settings.resource_server_url:
            raise AuthorizeError(
                "invalid_request", "resource must exactly identify this MCP server"
            )
        registered_scopes = set((client.scope or "").split())
        requested_scopes = set(params.scopes or registered_scopes)
        if "vklass.read" not in requested_scopes or not requested_scopes.issubset(
            registered_scopes
        ):
            raise AuthorizeError("invalid_scope", "requested scope is not registered")
        params = params.model_copy(update={"scopes": sorted(requested_scopes)})
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", params.code_challenge):
            raise AuthorizeError("invalid_request", "invalid S256 PKCE challenge")
        active_statuses = {"pending", "queued", "authenticating", "consent"}
        active_flows = [
            flow
            for flow in self._flows.values()
            if not flow.expired and flow.status in active_statuses
        ]
        if len(active_flows) >= self.settings.max_pending_authorizations:
            raise AuthorizeError("temporarily_unavailable", "authorization capacity reached")
        active_for_client = sum(
            1 for flow in active_flows if flow.client.client_id == client.client_id
        )
        if active_for_client >= self.settings.max_pending_authorizations_per_client:
            raise AuthorizeError("temporarily_unavailable", "too many pending authorizations")
        flow_id = secrets.token_urlsafe(32)
        self._flows[flow_id] = PendingAuthorization(
            flow_id=flow_id,
            client=client,
            params=params,
            created_at=time.time(),
        )
        self._prune_flows()
        return f"{self.public_base_url}/auth/vklass/{flow_id}"

    def get_flow(self, flow_id: str) -> PendingAuthorization | None:
        flow = self._flows.get(flow_id)
        if flow and not flow.expired:
            return flow
        if flow:
            flow.status = "expired"
            flow.pending_cookie = None
            flow.error = "Authorization request expired"
            if flow.login_task and not flow.login_task.done():
                flow.login_task.cancel()
        return None

    async def complete_authorization(
        self,
        flow_id: str,
        subject: str,
        before_issue: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        flow = self.get_flow(flow_id)
        if flow is None:
            raise ValueError("authorization flow is no longer active")
        async with flow.lock:
            if flow.status != "consent" or flow.authenticated_subject != subject:
                raise ValueError("authorization flow is not awaiting consent")
            if before_issue:
                await before_issue()
            code = secrets.token_urlsafe(32)
            code_hash = _token_hash(code)
            scopes = flow.params.scopes or ["vklass.read"]
            authorization_code = AuthorizationCode(
                code=code,
                scopes=scopes,
                expires_at=time.time() + self.settings.authorization_code_ttl_seconds,
                client_id=str(flow.client.client_id),
                code_challenge=flow.params.code_challenge,
                redirect_uri=flow.params.redirect_uri,
                redirect_uri_provided_explicitly=flow.params.redirect_uri_provided_explicitly,
                resource=self.settings.resource_server_url,
                subject=subject,
            )
            async with self._lock:
                try:
                    await self.db.execute(
                        """INSERT INTO oauth_codes(
                             token_hash, client_id, subject, data_json, expires_at
                           ) VALUES(?,?,?,?,?)""",
                        (
                            code_hash,
                            authorization_code.client_id,
                            subject,
                            authorization_code.model_copy(update={"code": ""}).model_dump_json(),
                            int(authorization_code.expires_at),
                        ),
                    )
                    await self.db.commit()
                except Exception:
                    await self.db.rollback()
                    raise
            flow.issued_code_hash = code_hash
            flow.pending_cookie = None
            flow.status = "complete"
            flow.qr_seed = None
            flow.redirect_url = construct_redirect_uri(
                str(flow.params.redirect_uri), code=code, state=flow.params.state
            )
            return flow.redirect_url

    async def deny_authorization(self, flow_id: str) -> str:
        flow = self.get_flow(flow_id)
        if flow is None:
            raise ValueError("authorization flow is no longer active")
        async with flow.lock:
            if flow.status == "exchanged":
                raise ValueError("authorization code was already exchanged")
            if flow.issued_code_hash:
                async with self._lock:
                    try:
                        await self.db.execute(
                            "DELETE FROM oauth_codes WHERE token_hash=?",
                            (flow.issued_code_hash,),
                        )
                        await self.db.commit()
                    except Exception:
                        await self.db.rollback()
                        raise
                flow.issued_code_hash = None
            flow.status = "denied"
            flow.qr_seed = None
            flow.pending_cookie = None
            flow.redirect_url = construct_redirect_uri(
                str(flow.params.redirect_uri),
                error="access_denied",
                error_description="The user denied access",
                state=flow.params.state,
            )
            return flow.redirect_url

    def fail_authorization(self, flow_id: str, message: str) -> None:
        flow = self._flows.get(flow_id)
        if flow and flow.status in {"pending", "queued", "authenticating", "consent"}:
            flow.status = "error"
            flow.qr_seed = None
            flow.pending_cookie = None
            flow.error = message

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with self.db.execute(
            "SELECT data_json FROM oauth_codes WHERE token_hash=? AND client_id=?",
            (_token_hash(authorization_code), client.client_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        stored = AuthorizationCode.model_validate_json(row["data_json"])
        return stored.model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_hash = _token_hash(authorization_code.code)
        flow = next(
            (item for item in self._flows.values() if item.issued_code_hash == code_hash),
            None,
        )
        if flow:
            async with flow.lock:
                result = await self._exchange_authorization_code_locked(
                    client, authorization_code, code_hash
                )
                flow.status = "exchanged"
                flow.issued_code_hash = None
                return result
        return await self._exchange_authorization_code_locked(client, authorization_code, code_hash)

    async def _exchange_authorization_code_locked(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
        code_hash: str,
    ) -> OAuthToken:
        async with self._lock:
            try:
                cursor = await self.db.execute(
                    "DELETE FROM oauth_codes WHERE token_hash=? AND client_id=?",
                    (code_hash, client.client_id),
                )
                if cursor.rowcount != 1:
                    raise TokenError("invalid_grant", "authorization code was already used")
                result = await self._issue_token_pair(
                    client_id=str(client.client_id),
                    subject=authorization_code.subject or "",
                    scopes=authorization_code.scopes,
                    resource=authorization_code.resource,
                )
                await self.db.commit()
                return result
            except Exception:
                await self.db.rollback()
                raise

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        row = await self._load_token(refresh_token, "refresh", str(client.client_id))
        if row:
            return StoredRefreshToken(
                token=refresh_token,
                client_id=str(row["client_id"]),
                scopes=json.loads(row["scopes_json"]),
                expires_at=int(row["expires_at"]),
                subject=str(row["subject"]),
                resource=row["resource"],
                pair_id=str(row["pair_id"]),
                family_id=str(row["family_id"]),
            )
        async with self.db.execute(
            "SELECT * FROM oauth_used_refresh WHERE token_hash=? AND client_id=?",
            (_token_hash(refresh_token), client.client_id),
        ) as cursor:
            used = await cursor.fetchone()
        if not used or int(used["expires_at"]) < int(time.time()):
            return None
        return StoredRefreshToken(
            token=refresh_token,
            client_id=str(used["client_id"]),
            scopes=json.loads(used["scopes_json"]),
            expires_at=int(used["expires_at"]),
            subject=str(used["subject"]),
            resource=used["resource"],
            pair_id="replayed",
            family_id=str(used["family_id"]),
            replayed=True,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: StoredRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._lock:
            try:
                replayed = refresh_token.replayed
                if not replayed:
                    cursor = await self.db.execute(
                        "DELETE FROM oauth_tokens WHERE pair_id=? AND client_id=?",
                        (refresh_token.pair_id, client.client_id),
                    )
                    replayed = cursor.rowcount < 1
                if replayed:
                    await self.db.execute(
                        "DELETE FROM oauth_tokens WHERE family_id=?",
                        (refresh_token.family_id,),
                    )
                    await self.db.execute(
                        "DELETE FROM oauth_used_refresh WHERE family_id=?",
                        (refresh_token.family_id,),
                    )
                    await self.db.commit()
                    raise TokenError("invalid_grant", "refresh token replay detected")
                await self.db.execute(
                    """INSERT INTO oauth_used_refresh(
                         token_hash,family_id,client_id,subject,scopes_json,resource,expires_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        _token_hash(refresh_token.token),
                        refresh_token.family_id,
                        refresh_token.client_id,
                        refresh_token.subject,
                        json.dumps(refresh_token.scopes, separators=(",", ":")),
                        refresh_token.resource,
                        refresh_token.expires_at,
                    ),
                )
                result = await self._issue_token_pair(
                    client_id=str(client.client_id),
                    subject=refresh_token.subject or "",
                    scopes=scopes,
                    resource=refresh_token.resource,
                    family_id=refresh_token.family_id,
                )
                await self.db.commit()
                return result
            except TokenError:
                await self.db.rollback()
                raise
            except Exception:
                await self.db.rollback()
                raise

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        row = await self._load_token(token, "access")
        if not row or row["resource"] != self.settings.resource_server_url:
            return None
        return StoredAccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=str(row["subject"]),
            claims={"iss": self.issuer_url},
            pair_id=str(row["pair_id"]),
            family_id=str(row["family_id"]),
        )

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        async with self._lock:
            try:
                await self.db.execute(
                    "DELETE FROM oauth_tokens WHERE family_id=?", (token.family_id,)
                )
                await self.db.execute(
                    "DELETE FROM oauth_used_refresh WHERE family_id=?", (token.family_id,)
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def revoke_subject(self, subject: str) -> None:
        """Revoke every grant and pending code for one Vklass user."""

        async with self._lock:
            try:
                await self.db.execute("DELETE FROM oauth_codes WHERE subject=?", (subject,))
                await self.db.execute("DELETE FROM oauth_tokens WHERE subject=?", (subject,))
                await self.db.execute("DELETE FROM oauth_used_refresh WHERE subject=?", (subject,))
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def _issue_token_pair(
        self,
        *,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str | None,
        family_id: str | None = None,
    ) -> OAuthToken:
        if not subject:
            raise TokenError("invalid_grant", "authorization has no user subject")
        now = int(time.time())
        pair_id = secrets.token_hex(16)
        family_id = family_id or secrets.token_hex(16)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(48)
        access_expires = now + self.settings.access_token_ttl_seconds
        refresh_expires = now + self.settings.refresh_token_ttl_seconds
        values = [
            (
                _token_hash(access),
                "access",
                pair_id,
                family_id,
                client_id,
                subject,
                json.dumps(scopes, separators=(",", ":")),
                resource,
                access_expires,
                now,
            ),
            (
                _token_hash(refresh),
                "refresh",
                pair_id,
                family_id,
                client_id,
                subject,
                json.dumps(scopes, separators=(",", ":")),
                resource,
                refresh_expires,
                now,
            ),
        ]
        await self.db.executemany(
            """INSERT INTO oauth_tokens(
                 token_hash,token_type,pair_id,family_id,client_id,subject,scopes_json,
                 resource,expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        return OAuthToken(
            access_token=access,
            expires_in=self.settings.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def _load_token(
        self, raw_token: str, token_type: str, client_id: str | None = None
    ) -> aiosqlite.Row | None:
        sql = "SELECT * FROM oauth_tokens WHERE token_hash=? AND token_type=?"
        params: list[Any] = [_token_hash(raw_token), token_type]
        if client_id is not None:
            sql += " AND client_id=?"
            params.append(client_id)
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        if row and int(row["expires_at"]) >= int(time.time()):
            return row
        return None

    def _prune_flows(self) -> None:
        expired = [key for key, flow in self._flows.items() if flow.expired]
        for key in expired:
            flow = self._flows.pop(key)
            if flow.login_task and not flow.login_task.done():
                flow.login_task.cancel()


def _validate_redirect_uri(value: str) -> None:
    parsed = urlparse(value)
    if parsed.fragment or parsed.username or parsed.password:
        raise RegistrationError("invalid_redirect_uri", "redirect URI contains forbidden parts")
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if loopback:
            return
    raise RegistrationError("invalid_redirect_uri", "redirect URI must use HTTPS or loopback HTTP")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fernet_key(value: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())
