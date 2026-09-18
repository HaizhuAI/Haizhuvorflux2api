"""Admin API for WebUI: account OTP login, pool management, settings, logs."""
from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import config
import db
import vorflux
from pool import pool

router = APIRouter(prefix="/admin/api")

_admin_sessions: set[str] = set()


# --------------------------------------------------------------------- auth
def _admin_token() -> str:
    tok = config.ADMIN_TOKEN or db.get_setting("admin_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        db.set_setting("admin_token", tok)
        print(f"[vorflux-gateway] generated ADMIN_TOKEN: {tok}")
    return tok


async def admin_auth(request: Request,
                     authorization: str = Header(default=""),
                     x_admin_token: str = Header(default="")) -> None:
    token = (authorization.removeprefix("Bearer ").strip()
             or x_admin_token.strip())
    if token == _admin_token() or token in _admin_sessions:
        return
    raise HTTPException(401, "invalid admin token")


class LoginBody(BaseModel):
    token: str


@router.post("/login")
async def login(body: LoginBody):
    if body.token != _admin_token():
        raise HTTPException(401, "bad token")
    sess = secrets.token_urlsafe(32)
    _admin_sessions.add(sess)
    return {"session": sess}


@router.post("/logout", dependencies=[Depends(admin_auth)])
async def logout(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    _admin_sessions.discard(token)
    return {"ok": True}


# ------------------------------------------------------------------ overview
@router.get("/overview", dependencies=[Depends(admin_auth)])
async def overview():
    ov = pool.overview()
    ov["upstream"] = config.VORFLUX_BASE
    ov["global_proxy"] = db.get_setting("global_proxy", config.GLOBAL_PROXY)
    return ov


@router.get("/stream", dependencies=[Depends(admin_auth)])
async def stats_stream():
    from fastapi.responses import StreamingResponse

    async def gen():
        while True:
            yield f"data: {json.dumps({'overview': pool.overview(), 'accounts': pool.snapshot()})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------ accounts
@router.get("/accounts", dependencies=[Depends(admin_auth)])
async def accounts():
    return {"accounts": pool.snapshot()}


class OtpStart(BaseModel):
    email: str
    proxy: str = ""


@router.post("/accounts/otp/start", dependencies=[Depends(admin_auth)])
async def otp_start(body: OtpStart):
    proxy = body.proxy or db.get_setting("global_proxy", config.GLOBAL_PROXY)
    await vorflux.otp_start(body.email, proxy or None)
    return {"ok": True, "message": f"verification code sent to {body.email}"}


class OtpVerify(BaseModel):
    email: str
    code: str
    proxy: str = ""
    name: str = ""


@router.post("/accounts/otp/verify", dependencies=[Depends(admin_auth)])
async def otp_verify(body: OtpVerify):
    proxy = body.proxy or db.get_setting("global_proxy", config.GLOBAL_PROXY)
    data = await vorflux.otp_verify(body.email, body.code, proxy or None)
    identity = data.get("identity") or {}
    acc_id = await _bootstrap_account(
        email=body.email,
        auth0_user_id=identity.get("auth0UserId", ""),
        tokens=data, proxy=proxy, name=body.name)
    return {"ok": True, "account_id": acc_id}


class TokenImport(BaseModel):
    email: str = ""
    refresh_token: str
    proxy: str = ""
    account_id: str = ""
    kind: str = ""           # "" auto | "otp" | "oauth"


@router.post("/accounts/import", dependencies=[Depends(admin_auth)])
async def token_import(body: TokenImport):
    proxy = body.proxy or db.get_setting("global_proxy", config.GLOBAL_PROXY)
    kind = body.kind or vorflux.detect_token_kind(body.refresh_token)
    if kind not in ("otp", "oauth"):
        raise HTTPException(400, "kind must be otp|oauth")
    data = await vorflux.refresh_tokens(body.refresh_token, kind, proxy or None)
    identity = data.get("identity") or {}
    email = body.email or identity.get("email") or f"imported-{secrets.token_hex(3)}@local"
    acc_id = await _bootstrap_account(
        email=email,
        auth0_user_id=identity.get("auth0UserId", ""),
        tokens={**data, "refresh_token": data.get("refresh_token") or body.refresh_token},
        proxy=proxy, fixed_account_id=body.account_id, auth_kind=kind)
    return {"ok": True, "account_id": acc_id, "kind": kind}


async def _bootstrap_account(email: str, auth0_user_id: str, tokens: dict,
                             proxy: str = "", name: str = "",
                             fixed_account_id: str = "",
                             auth_kind: str = "otp") -> int:
    """Persist tokens, resolve account_id via MyAccounts/bootstrap chain."""
    expires = time.time() + (tokens.get("expires_in") or 86400)
    acc_pk = await asyncio.to_thread(db.upsert_account, {
        "email": email,
        "auth0_user_id": auth0_user_id,
        "account_id": fixed_account_id,
        "auth_kind": auth_kind,
        "refresh_token": tokens.get("refresh_token", ""),
        "access_token": tokens.get("access_token", ""),
        "id_token": tokens.get("id_token", ""),
        "token_expires_at": expires,
        "proxy": proxy,
    })
    await pool.reload()
    ctx = pool.get_ctx(acc_pk)
    if ctx and not fixed_account_id:
        try:
            await pool._resolve_account_id(ctx, tokens.get("access_token", ""))
        except vorflux.VorfluxError as e:
            # account row exists; account_id resolution can retry lazily
            print(f"[pool] account-id resolve deferred for {email}: {e}")
    await pool.reload()
    return acc_pk


class AccountPatch(BaseModel):
    proxy: str | None = None
    status: str | None = None          # active | disabled
    max_concurrent: int | None = None
    account_id: str | None = None


@router.patch("/accounts/{acc_id}", dependencies=[Depends(admin_auth)])
async def patch_account(acc_id: int, body: AccountPatch):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "status" in patch and patch["status"] not in ("active", "disabled"):
        raise HTTPException(400, "status must be active|disabled")
    await asyncio.to_thread(db.update_account, acc_id, patch)
    await pool.reload()
    return {"ok": True}


@router.delete("/accounts/{acc_id}", dependencies=[Depends(admin_auth)])
async def remove_account(acc_id: int):
    await asyncio.to_thread(db.delete_account, acc_id)
    await pool.reload()
    return {"ok": True}


@router.post("/accounts/{acc_id}/refresh", dependencies=[Depends(admin_auth)])
async def refresh_account(acc_id: int):
    ctx = pool.get_ctx(acc_id)
    if not ctx:
        raise HTTPException(404, "account not found")
    try:
        await pool.force_refresh(ctx)
    except vorflux.VorfluxError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "token_expires_at": ctx.row.get("token_expires_at")}


@router.post("/accounts/{acc_id}/test", dependencies=[Depends(admin_auth)])
async def test_account(acc_id: int):
    ctx = pool.get_ctx(acc_id)
    if not ctx:
        raise HTTPException(404, "account not found")
    try:
        sess = await pool.session(ctx)
        t0 = time.time()
        accounts = await sess.my_accounts()
        models = await sess.list_models()
        balance = {}
        try:
            balance = await sess.credit_balance()
        except vorflux.VorfluxError:
            pass
        # auto-revive an account that was sidelined for zero credits
        if (ctx.row.get("status") == "no_credits"
                and (balance.get("balanceCredits") or 0) > 0):
            await asyncio.to_thread(db.update_account, acc_id,
                                    {"status": "active", "fail_count": 0,
                                     "cooldown_until": 0})
            await pool.reload()
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "vorflux_accounts": len(accounts),
                "models_available": sum(1 for m in models.get("models", [])
                                        if m.get("isAvailable")),
                "default_model": models.get("defaultModelKey"),
                "credit_balance": balance}
    except vorflux.VorfluxError as e:
        return {"ok": False, "error": str(e), "code": e.code}


@router.post("/accounts/{acc_id}/reset-cb", dependencies=[Depends(admin_auth)])
async def reset_breaker(acc_id: int):
    await asyncio.to_thread(db.update_account, acc_id,
                            {"fail_count": 0, "cooldown_until": 0})
    await pool.reload()
    return {"ok": True}


# ------------------------------------------------------------------ settings
@router.get("/settings", dependencies=[Depends(admin_auth)])
async def get_settings():
    return {
        "global_proxy": db.get_setting("global_proxy", config.GLOBAL_PROXY),
        "model_map": db.get_json_setting("model_map", {}),
        "max_turn_wait": db.get_setting("max_turn_wait",
                                       str(config.MAX_TURN_WAIT)),
        "upstream": config.VORFLUX_BASE,
    }


class SettingsPatch(BaseModel):
    global_proxy: str | None = None
    model_map: dict | None = None
    max_turn_wait: float | None = None


@router.put("/settings", dependencies=[Depends(admin_auth)])
async def put_settings(body: SettingsPatch):
    if body.global_proxy is not None:
        db.set_setting("global_proxy", body.global_proxy)
    if body.model_map is not None:
        db.set_json_setting("model_map", body.model_map)
    if body.max_turn_wait is not None:
        db.set_setting("max_turn_wait", str(body.max_turn_wait))
        config.MAX_TURN_WAIT = body.max_turn_wait
    return {"ok": True}


# ------------------------------------------------------------------ api keys
@router.get("/keys", dependencies=[Depends(admin_auth)])
async def keys():
    return {"keys": db.list_api_keys(),
            "master_key_set": bool(config.API_KEY)}


class KeyBody(BaseModel):
    name: str = ""
    key: str = ""


@router.post("/keys", dependencies=[Depends(admin_auth)])
async def add_key(body: KeyBody):
    key = body.key or f"sk-vgw-{secrets.token_urlsafe(24)}"
    db.add_api_key(key, body.name)
    return {"ok": True, "key": key}


@router.delete("/keys/{kid}", dependencies=[Depends(admin_auth)])
async def del_key(kid: int):
    db.delete_api_key(kid)
    return {"ok": True}


# ------------------------------------------------------------------ logs
@router.get("/logs", dependencies=[Depends(admin_auth)])
async def logs(limit: int = 200):
    return {"logs": db.recent_logs(min(limit, 1000))}
