"""Vorflux protocol client — reverse-engineered from us1.vorflux.com SPA bundle.

Auth   : POST /api/auth/passwordless/start|verify|refresh   (email OTP -> Auth0 tokens)
         POST https://<auth0>/oauth/token  grant_type=refresh_token  (social OAuth accounts)
GraphQL: POST /query  +  Authorization: Bearer <access_token>, X-Account-ID: <accountId>
SSE    : GET  /api/sessions/updates/stream  (cookie via POST /api/sessions/auth)
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, AsyncIterator

import httpx

import config

GQL = "/query"

AUTH0_DOMAIN = "prod-vorflux-001.us.auth0.com"
AUTH0_CLIENT_ID = "UYsjHQprNR3pSrXUvclEEv4Dtt0bUSSs"
AUTH0_AUDIENCE = "https://prod-vorflux-001.us.auth0.com/api/v2/"

# ---------------------------------------------------------------- operations
Q_MY_ACCOUNTS = """
query MyAccounts { myAccounts { accountId accountType membershipRole isDefault
  accountName accountSpaceAccess onboardingCompleted userOnboardingCompleted adminEmail } }
"""

Q_REGISTER_USER = """
mutation RegisterUser($input: RegisterUserInput!) {
  registerUser(input: $input) {
    user { id uuid email name profilePicture timezone }
    isNewUser autoCreatedAccountId autoJoinedAccountIds explicitAccountSetupEnabled
  }
}
"""

Q_CREATE_INDIVIDUAL = "mutation CreateIndividualAccount { createIndividualAccount }"
Q_SELECT_ACCOUNT = "mutation SelectAccount($accountId: String!) { selectAccount(accountId: $accountId) }"

Q_LIST_MODELS = """
query ListAvailableModelsForAccount {
  listAvailableModelsForAccount {
    models { modelKey displayName description supportsThinking supportsVision
             family isAvailable unavailableReason disabledByAccountPolicy
             accountPolicyDisabledModelKey }
    defaultModelKey
  }
}
"""

Q_CREATE_SESSION = """
mutation CreateAgentSession(
  $sourceType: AgentSessionSourceType!, $sourceId: String, $initialMessage: String,
  $sessionType: AgentSessionType, $imageIds: [ID!],
  $modelConfiguration: ModelConfigurationInput,
  $initialRepositoryUrls: [String!], $internalNote: String, $harnessId: ID,
  $createAsTodo: Boolean, $autopilotEnabled: Boolean, $isIncognito: Boolean,
  $machineTypeTier: MachineTypeTier
) {
  createAgentSession(
    sourceType: $sourceType, sourceId: $sourceId, initialMessage: $initialMessage,
    sessionType: $sessionType, imageIds: $imageIds,
    modelConfiguration: $modelConfiguration,
    initialRepositoryUrls: $initialRepositoryUrls, internalNote: $internalNote,
    harnessId: $harnessId, createAsTodo: $createAsTodo,
    autopilotEnabled: $autopilotEnabled, isIncognito: $isIncognito,
    machineTypeTier: $machineTypeTier
  ) {
    sessionId sessionStatus sessionType title createdAt
    currentLLMModelName currentModelKey
    tokenUsage { model inputTokens outputTokens cachedTokens totalCostUsd }
    totalLlmCostUsd llmCostMicrousd
  }
}
"""

Q_ADD_MESSAGE = """
mutation AddMessageToSession($input: AddMessageToSessionInput!) {
  addMessageToSession(input: $input) { id sessionId sentAt sentBy processingState }
}
"""

Q_GET_SESSION = """
query GetSession($sessionId: ID!) {
  session(sessionId: $sessionId) {
    sessionId sessionStatus sessionType title createdAt lastMessageAt
    currentLLMModelName currentModelKey sessionProgress
    tokenUsage { model inputTokens outputTokens cachedTokens totalCostUsd }
    totalLlmCostUsd llmCostMicrousd
  }
}
"""

_MSG_FIELDS = """
  id sessionId sentAt sentBy agentType processingState
  messageContent {
    __typename
    ... on SimpleTextMessage { content summary }
    ... on InterimUpdateMessage { content }
    ... on AgentThoughtMessage { content }
    ... on ErrorOutputMessage { message statusPageUrl }
    ... on WarningOutputMessage { message }
    ... on TurnCostMessage { turnCostMicrousd sessionTotalCostMicrousd }
  }
"""

Q_GET_MESSAGES = f"""
query GetSessionMessages($sessionId: ID!, $limit: Int) {{
  sessionMessages(sessionId: $sessionId, limit: $limit) {{
    messages {{{_MSG_FIELDS}}}
    hasMore
  }}
}}
"""

Q_CANCEL_SESSION = "mutation CancelSession($sessionId: ID!) { cancelSession(sessionId: $sessionId) }"

Q_CREDIT_BALANCE = """
query GetCreditBalance { creditBalance { balanceMicrousd balanceCredits balanceUsd } }
"""

TERMINAL_STATUSES = {"AWAITING_INPUT", "COMPLETED", "IDLE", "CANCELLED", "ERROR"}
ACTIVE_STATUSES = {"QUEUED", "RUNNING", "PENDING", "STARTING", "IN_PROGRESS"}


class VorfluxError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _client(proxy: str | None) -> httpx.AsyncClient:
    kw: dict[str, Any] = {
        "base_url": config.VORFLUX_BASE,
        "timeout": httpx.Timeout(config.HTTP_TIMEOUT, connect=15.0),
        "headers": {"User-Agent": "vorflux-gateway/1.0", "Accept": "application/json"},
        "follow_redirects": True,
        # ignore env/system proxy: proxying is opt-in via per-account or global config
        "trust_env": False,
    }
    if proxy:
        kw["proxy"] = proxy
    return httpx.AsyncClient(**kw)


# ------------------------------------------------------------------ auth API
async def otp_start(email: str, proxy: str | None = None) -> None:
    async with _client(proxy) as c:
        r = await c.post("/api/auth/passwordless/start", json={"email": email})
        if not r.is_success:
            raise VorfluxError(_err_text(r, "Unable to send code"), r.status_code, "otp_start_failed")


async def otp_verify(email: str, code: str, proxy: str | None = None) -> dict:
    """-> {access_token, id_token, refresh_token, expires_in, identity{auth0UserId,email}}"""
    async with _client(proxy) as c:
        r = await c.post("/api/auth/passwordless/verify",
                         json={"email": email, "code": code})
        data = _json(r)
        if not r.is_success or not data.get("access_token"):
            raise VorfluxError(_err_text(r, "Verification failed"), r.status_code, "otp_verify_failed")
        return data


async def token_refresh(refresh_token: str, proxy: str | None = None) -> dict:
    async with _client(proxy) as c:
        r = await c.post("/api/auth/passwordless/refresh",
                         json={"refresh_token": refresh_token})
        if r.status_code in (401, 403):
            raise VorfluxError("refresh_token rejected", r.status_code, "refresh_rejected")
        data = _json(r)
        if not r.is_success or not data.get("access_token"):
            raise VorfluxError(_err_text(r, "Token refresh failed"), r.status_code, "refresh_failed")
        return data


async def oauth_refresh(refresh_token: str, proxy: str | None = None) -> dict:
    """Auth0 SPA refresh-token grant for social-logged-in accounts (Google/GitHub).
    Rotating: response carries a NEW refresh_token which must be persisted."""
    async with _client(proxy) as c:
        try:
            r = await c.post(f"https://{AUTH0_DOMAIN}/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": AUTH0_CLIENT_ID,
                "refresh_token": refresh_token,
                "audience": AUTH0_AUDIENCE,
                "scope": "openid profile email offline_access",
            })
        except httpx.HTTPError as e:
            raise VorfluxError(f"network: {e}", 0, "network") from e
        data = _json(r)
        if not r.is_success or not data.get("access_token"):
            msg = data.get("error_description") or data.get("error") or "oauth refresh rejected"
            code = "refresh_rejected" if r.status_code in (400, 401, 403) else "refresh_failed"
            raise VorfluxError(msg, r.status_code, code)
        data.setdefault("identity", _identity_from_id_token(data.get("id_token", "")))
        return data


async def refresh_tokens(refresh_token: str, kind: str = "otp",
                         proxy: str | None = None) -> dict:
    """Dispatch refresh by account auth kind: 'otp' (passwordless) | 'oauth' (Auth0)."""
    if kind == "oauth":
        return await oauth_refresh(refresh_token, proxy)
    return await token_refresh(refresh_token, proxy)


def detect_token_kind(refresh_token: str) -> str:
    """Auth0 rotating refresh tokens are 'v1.<base64>'; passwordless tokens are opaque/JWT."""
    return "oauth" if refresh_token.startswith("v1.") else "otp"


def _identity_from_id_token(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {"auth0UserId": claims.get("sub", ""),
                "email": claims.get("email", ""),
                "name": claims.get("name") or claims.get("nickname", "")}
    except Exception:
        return {}


# ------------------------------------------------------------------ sessions
class AccountSession:
    """Bound to one account row; handles token + account-id headers."""

    def __init__(self, access_token: str, account_id: str,
                 proxy: str | None = None, email: str = ""):
        self.access_token = access_token
        self.account_id = account_id
        self.email = email
        self.proxy = proxy

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if self.account_id:
            h["X-Account-ID"] = self.account_id
        return h

    async def graphql(self, query: str, variables: dict | None = None,
                      op: str = "") -> dict:
        async with _client(self.proxy) as c:
            try:
                r = await c.post(GQL, json={"query": query, "variables": variables or {}},
                                 headers=self._headers())
            except httpx.HTTPError as e:
                raise VorfluxError(f"network: {e}", 0, "network") from e
            if r.status_code in (401, 403):
                raise VorfluxError("unauthorized", r.status_code, "unauthorized")
            data = _json(r)
            if not r.is_success:
                raise VorfluxError(_err_text(r, "GraphQL HTTP error"), r.status_code)
            errs = data.get("errors") or []
            if errs:
                msg = errs[0].get("message", "graphql error")
                code = (errs[0].get("extensions") or {}).get("code", "")
                if code in ("UNAUTHENTICATED", "FORBIDDEN") or "auth" in msg.lower():
                    raise VorfluxError(msg, 401, "unauthorized")
                raise VorfluxError(f"{op or 'graphql'}: {msg}", r.status_code, "graphql_error")
            return data.get("data") or {}

    async def my_accounts(self) -> list[dict]:
        d = await self.graphql(Q_MY_ACCOUNTS, op="MyAccounts")
        return d.get("myAccounts") or []

    async def register_user(self, auth0_user_id: str, email: str,
                            name: str = "") -> dict:
        d = await self.graphql(Q_REGISTER_USER, {
            "input": {
                "auth0UserId": auth0_user_id,
                "email": email,
                "name": name or email.split("@")[0],
                "timezone": "UTC",
            }}, op="RegisterUser")
        return d.get("registerUser") or {}

    async def create_individual_account(self) -> Any:
        d = await self.graphql(Q_CREATE_INDIVIDUAL, op="CreateIndividualAccount")
        return d.get("createIndividualAccount")

    async def select_account(self, account_id: str) -> Any:
        d = await self.graphql(Q_SELECT_ACCOUNT, {"accountId": account_id},
                               op="SelectAccount")
        return d.get("selectAccount")

    async def list_models(self) -> dict:
        d = await self.graphql(Q_LIST_MODELS, op="ListAvailableModelsForAccount")
        return d.get("listAvailableModelsForAccount") or {}

    async def create_session(self, initial_message: str,
                             model_key: str | None = None,
                             model_selection: dict | None = None) -> dict:
        variables: dict[str, Any] = {
            "sourceType": "MANUAL",
            "sessionType": "GENERAL",
            "initialMessage": initial_message,
        }
        if model_key:
            variables["modelConfiguration"] = {
                "modelKey": model_key,
                "modelSelection": {"modelKey": model_key,
                                   **(model_selection or {})},
            }
        d = await self.graphql(Q_CREATE_SESSION, variables, op="CreateAgentSession")
        return d.get("createAgentSession") or {}

    async def add_message(self, session_id: str, contents: list[str],
                          model_key: str | None = None,
                          model_selection: dict | None = None) -> dict:
        inp: dict[str, Any] = {
            "sessionId": session_id,
            "genericInput": {"contents": contents},
        }
        if model_key:
            inp["modelConfiguration"] = {
                "modelKey": model_key,
                "modelSelection": {"modelKey": model_key,
                                   **(model_selection or {})},
            }
        d = await self.graphql(Q_ADD_MESSAGE, {"input": inp}, op="AddMessageToSession")
        return d.get("addMessageToSession") or {}

    async def get_session(self, session_id: str) -> dict | None:
        d = await self.graphql(Q_GET_SESSION, {"sessionId": session_id}, op="GetSession")
        return d.get("session")

    async def get_messages(self, session_id: str, limit: int = 100) -> list[dict]:
        d = await self.graphql(Q_GET_MESSAGES,
                               {"sessionId": session_id, "limit": limit},
                               op="GetSessionMessages")
        return (d.get("sessionMessages") or {}).get("messages") or []

    async def cancel_session(self, session_id: str) -> None:
        try:
            await self.graphql(Q_CANCEL_SESSION, {"sessionId": session_id},
                               op="CancelSession")
        except VorfluxError:
            pass

    async def credit_balance(self) -> dict:
        d = await self.graphql(Q_CREDIT_BALANCE, op="GetCreditBalance")
        return d.get("creditBalance") or {}

    async def session_cookie(self, client: httpx.AsyncClient) -> None:
        """POST /api/sessions/auth -> sets cookie jar on provided client."""
        r = await client.post("/api/sessions/auth", headers=self._headers())
        if not r.is_success:
            raise VorfluxError(f"session cookie failed: {r.status_code}",
                               r.status_code, "cookie_failed")

    async def sse_stream(self) -> AsyncIterator[dict]:
        """Yield session_update events (requires cookie via /api/sessions/auth)."""
        async with _client(self.proxy) as c:
            await self.session_cookie(c)
            async with c.stream("GET", "/api/sessions/updates/stream",
                                headers={"Accept": "text/event-stream"}) as r:
                if r.status_code != 200:
                    raise VorfluxError(f"sse {r.status_code}", r.status_code, "sse_failed")
                event = ""
                async for line in r.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "session_update":
                        try:
                            yield json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                    elif line == "":
                        event = ""


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except Exception:
        return {}


def _err_text(r: httpx.Response, fallback: str) -> str:
    try:
        d = r.json()
        return d.get("error") or d.get("message") or fallback
    except Exception:
        return f"{fallback} (HTTP {r.status_code})"
