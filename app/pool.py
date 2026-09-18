"""Account pool: rotation, per-account concurrency, circuit breaker, token lifecycle."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import config
import db
import vorflux


@dataclass
class AccountCtx:
    row: dict
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)   # token refresh mutex
    inflight: int = 0
    rr_ts: float = 0.0                                          # last picked time (LRU tiebreak)

    @property
    def id(self) -> int:
        return self.row["id"]

    @property
    def email(self) -> str:
        return self.row["email"]

    @property
    def proxy(self) -> str:
        return self.row.get("proxy") or self.global_proxy()

    @staticmethod
    def global_proxy() -> str:
        return db.get_setting("global_proxy", config.GLOBAL_PROXY)

    @property
    def max_concurrent(self) -> int:
        return self.row.get("max_concurrent") or config.DEFAULT_MAX_CONCURRENT

    def cooling(self) -> bool:
        return (self.row.get("cooldown_until") or 0) > time.time()

    def healthy(self) -> bool:
        return self.row.get("status") == "active" and not self.cooling()


class Pool:
    def __init__(self) -> None:
        self._ctx: dict[int, AccountCtx] = {}
        self._mu = asyncio.Lock()
        self._rr = 0
        self.stats = {"requests": 0, "ok": 0, "fail": 0, "started_at": time.time()}
        self._lat: list[float] = []

    async def reload(self) -> None:
        async with self._mu:
            rows = await asyncio.to_thread(db.list_accounts)
            seen = set()
            for row in rows:
                seen.add(row["id"])
                if row["id"] in self._ctx:
                    self._ctx[row["id"]].row = row
                else:
                    self._ctx[row["id"]] = AccountCtx(row=row)
            for stale in set(self._ctx) - seen:
                del self._ctx[stale]

    async def pick(self, prefer_id: int | None = None) -> AccountCtx | None:
        """Least-inflight healthy account, LRU tiebreak.
        prefer_id pins to a specific account when it can still serve
        (conversation affinity); falls back to normal rotation otherwise."""
        async with self._mu:
            cands = [c for c in self._ctx.values() if c.healthy()
                     and c.inflight < c.max_concurrent]
            if not cands:
                return None
            if prefer_id is not None:
                pin = next((c for c in cands if c.id == prefer_id), None)
                if pin is not None:
                    cands = [pin]
            cands.sort(key=lambda c: (c.inflight, c.rr_ts))
            ctx = cands[0]
            ctx.inflight += 1
            ctx.rr_ts = time.time()
            return ctx

    async def release(self, ctx: AccountCtx, ok: bool, latency: float = 0.0,
                      hard_fail: bool = False) -> None:
        async with self._mu:
            ctx.inflight = max(0, ctx.inflight - 1)
            row = ctx.row
            if ok:
                row["fail_count"] = 0
                row["cooldown_until"] = 0
            else:
                row["fail_count"] = (row.get("fail_count") or 0) + 1
                if hard_fail or row["fail_count"] >= config.CB_FAIL_THRESHOLD:
                    exp = min(config.CB_BASE_COOLDOWN *
                              2 ** (row["fail_count"] - config.CB_FAIL_THRESHOLD),
                              config.CB_MAX_COOLDOWN)
                    row["cooldown_until"] = time.time() + max(exp, 15)
        # persist (non-blocking)
        patch = {"fail_count": row["fail_count"],
                 "cooldown_until": row.get("cooldown_until") or 0}
        await asyncio.to_thread(db.update_account, ctx.id, patch)
        self.stats["requests"] += 1
        self.stats["ok" if ok else "fail"] += 1
        if latency:
            self._lat.append(latency)
            if len(self._lat) > 500:
                self._lat = self._lat[-250:]

    # ------------------------------------------------------------ token mgmt
    async def ensure_token(self, ctx: AccountCtx) -> str:
        """Return a valid access token; refresh when <120s to expiry."""
        row = ctx.row
        if row.get("access_token") and (row.get("token_expires_at") or 0) - 120 > time.time():
            return row["access_token"]
        async with ctx.lock:
            row = ctx.row = await asyncio.to_thread(db.get_account, ctx.id) or ctx.row
            if row.get("access_token") and (row.get("token_expires_at") or 0) - 120 > time.time():
                return row["access_token"]
            data = await self._do_refresh(ctx)
            await self._save_tokens(ctx, data)
            return ctx.row["access_token"]

    async def _do_refresh(self, ctx: AccountCtx) -> dict:
        """Refresh via the account's auth kind; on rejection retry the other
        channel and persist the corrected kind (OTP accounts can hold v1.* tokens)."""
        row = ctx.row
        kind = row.get("auth_kind") or "otp"
        try:
            return await vorflux.refresh_tokens(row["refresh_token"], kind, ctx.proxy)
        except vorflux.VorfluxError as e:
            if e.code != "refresh_rejected":
                raise
            alt = "oauth" if kind == "otp" else "otp"
            data = await vorflux.refresh_tokens(row["refresh_token"], alt, ctx.proxy)
            ctx.row["auth_kind"] = alt
            await asyncio.to_thread(db.update_account, ctx.id, {"auth_kind": alt})
            return data

    async def force_refresh(self, ctx: AccountCtx) -> str:
        async with ctx.lock:
            data = await self._do_refresh(ctx)
            await self._save_tokens(ctx, data)
            return ctx.row["access_token"]

    async def _save_tokens(self, ctx: AccountCtx, data: dict) -> None:
        exp = time.time() + (data.get("expires_in") or 86400)
        patch = {
            "access_token": data["access_token"],
            "id_token": data.get("id_token", ""),
            "token_expires_at": exp,
        }
        if data.get("refresh_token"):
            patch["refresh_token"] = data["refresh_token"]
        await asyncio.to_thread(db.update_account, ctx.id, patch)
        ctx.row.update(patch)

    async def session(self, ctx: AccountCtx,
                      retried: bool = False) -> vorflux.AccountSession:
        token = await self.ensure_token(ctx)
        account_id = ctx.row.get("account_id") or ""
        if not account_id:
            account_id = await self._resolve_account_id(ctx, token)
        return vorflux.AccountSession(token, account_id, ctx.proxy, ctx.email)

    async def _resolve_account_id(self, ctx: AccountCtx, token: str) -> str:
        sess = vorflux.AccountSession(token, "", ctx.proxy, ctx.email)
        accounts = await sess.my_accounts()
        if not accounts:
            reg = await sess.register_user(ctx.row.get("auth0_user_id") or "",
                                           ctx.email)
            acc_id = reg.get("autoCreatedAccountId") or ""
            if not acc_id:
                await sess.create_individual_account()
                accounts = await sess.my_accounts()
        if accounts:
            acc = next((a for a in accounts if a.get("isDefault")), accounts[0])
            acc_id = acc["accountId"]
            try:
                await sess.select_account(acc_id)
            except vorflux.VorfluxError:
                pass
            await asyncio.to_thread(db.update_account, ctx.id,
                                    {"account_id": acc_id,
                                     "account_name": acc.get("accountName") or ""})
            ctx.row["account_id"] = acc_id
            return acc_id
        raise vorflux.VorfluxError("no vorflux account available", 0, "no_account")

    async def invalidate_token(self, ctx: AccountCtx) -> None:
        await asyncio.to_thread(db.update_account, ctx.id,
                                {"access_token": "", "token_expires_at": 0})
        ctx.row["access_token"] = ""

    # ------------------------------------------------------------ views
    def snapshot(self) -> list[dict]:
        out = []
        for c in sorted(self._ctx.values(), key=lambda x: x.id):
            r = c.row
            out.append({
                "id": c.id,
                "email": c.email,
                "auth_kind": r.get("auth_kind") or "otp",
                "account_id": r.get("account_id") or "",
                "account_name": r.get("account_name") or "",
                "proxy": r.get("proxy") or "",
                "effective_proxy": c.proxy or "",
                "status": r.get("status"),
                "healthy": c.healthy(),
                "cooldown_until": r.get("cooldown_until") or 0,
                "fail_count": r.get("fail_count") or 0,
                "inflight": c.inflight,
                "max_concurrent": c.max_concurrent,
                "token_valid": bool(r.get("access_token"))
                and (r.get("token_expires_at") or 0) > time.time(),
                "token_expires_at": r.get("token_expires_at") or 0,
                "stats": json.loads(r.get("stats_json") or "{}"),
                "created_at": r.get("created_at"),
            })
        return out

    def overview(self) -> dict:
        accs = self.snapshot()
        avg = sum(self._lat) / len(self._lat) if self._lat else 0
        return {
            "accounts_total": len(accs),
            "accounts_healthy": sum(1 for a in accs if a["healthy"]),
            "inflight": sum(a["inflight"] for a in accs),
            "requests": self.stats["requests"],
            "ok": self.stats["ok"],
            "fail": self.stats["fail"],
            "avg_latency_ms": round(avg * 1000),
            "uptime_s": round(time.time() - self.stats["started_at"]),
        }

    def get_ctx(self, acc_id: int) -> AccountCtx | None:
        return self._ctx.get(acc_id)


pool = Pool()
