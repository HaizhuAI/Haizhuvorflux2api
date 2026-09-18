"""Outlook batch -> vorflux accounts. OTP -> IMAP code -> verify -> bonus check.

Usage: python tools/outlook_farm.py [--limit N] [--start-from IDX]
"""
import asyncio
import json
import re
import sys
import time

sys.path.insert(0, r"D:\Devin2\vorflux-gateway\app")
sys.path.insert(0, r"D:\Devin2\vorflux-gateway\tools")

import db
import vorflux
from pool import pool
from outlook_mail import parse_accounts, refresh, Inbox

OUTLOOK_FILE = r"D:\Outlook.txt"
RESULTS = r"D:\Devin2\vorflux-gateway\tools\farm_results.json"

ELIG_Q = ("query E { signupBonusEligibility { eligible alreadyGranted "
          "verificationRequired providerIdentityAlreadyCredited } }")
CLAIM_M = ("mutation C { claimSignupBonus { eligible alreadyGranted "
           "verificationRequired providerIdentityAlreadyCredited } }")
GATE_Q = ("query G { billingGate { allowed reason balanceCredits "
          "hasPaymentMethod accountType signupBonusCredits } }")
BAL_Q = "query B { creditBalance { balanceCredits balanceUsd } }"


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


async def gql_retry(sess, q, op="", tries=3):
    for i in range(tries):
        try:
            return await sess.graphql(q, op=op)
        except vorflux.VorfluxError as e:
            if i == tries - 1 or e.code == "unauthorized":
                raise
            await asyncio.sleep(2)


async def farm_one(acc: dict, idx: int) -> dict:
    email_addr = acc["email"]
    res = {"email": email_addr, "status": "fail"}
    try:
        await vorflux.otp_start(email_addr)
        log(f"[{idx}] OTP sent -> {email_addr}")
    except vorflux.VorfluxError as e:
        res["error"] = f"otp_start: {e}"
        return res

    # poll IMAP for the code (up to ~90s)
    code = None
    try:
        tok = await asyncio.to_thread(refresh, acc)
    except Exception as e:
        res["error"] = f"msa refresh: {e}"
        return res
    deadline = time.time() + 90
    while time.time() < deadline and not code:
        try:
            box = await asyncio.to_thread(Inbox, email_addr, tok)
            code = await asyncio.to_thread(box.find_code)
            await asyncio.to_thread(box.close)
        except Exception as e:
            log(f"[{idx}] imap retry: {e}")
        if not code:
            await asyncio.sleep(8)
    if not code:
        res["error"] = "no OTP mail within 90s"
        return res
    log(f"[{idx}] code {code}")

    # verify -> tokens
    try:
        data = await vorflux.otp_verify(email_addr, code)
    except vorflux.VorfluxError as e:
        res["error"] = f"verify: {e}"
        return res
    ident = data.get("identity") or {}
    acc_pk = await asyncio.to_thread(db.upsert_account, {
        "email": email_addr,
        "auth0_user_id": ident.get("auth0UserId", ""),
        "account_id": "",
        "refresh_token": data.get("refresh_token", ""),
        "access_token": data.get("access_token", ""),
        "id_token": data.get("id_token", ""),
        "token_expires_at": time.time() + (data.get("expires_in") or 86400),
    })
    await pool.reload()
    res["pk"] = acc_pk

    # bootstrap: resolve account_id
    ctx = pool.get_ctx(acc_pk)
    try:
        await pool._resolve_account_id(ctx, data.get("access_token", ""))
    except Exception as e:
        log(f"[{idx}] bootstrap defer: {e}")
    await pool.reload()
    sess = await pool.session(ctx)

    # bonus path
    try:
        elig = (await gql_retry(sess, ELIG_Q, "elig")).get("signupBonusEligibility", {})
        res["eligibility"] = elig
        log(f"[{idx}] eligibility: {elig}")
        if elig.get("eligible") and not elig.get("alreadyGranted"):
            claim = (await gql_retry(sess, CLAIM_M, "claim")).get("claimSignupBonus", {})
            res["claim"] = claim
            log(f"[{idx}] claim -> {claim}")
        else:
            res["claim"] = "skipped (not eligible / already granted)"
    except Exception as e:
        res["eligibility_error"] = str(e)

    # balance + gate
    try:
        res["balance"] = (await gql_retry(sess, BAL_Q, "bal")).get("creditBalance", {})
        res["billing_gate"] = (await gql_retry(sess, GATE_Q, "gate")).get("billingGate", {})
    except Exception as e:
        res["balance_error"] = str(e)

    res["status"] = "ok"
    return res


async def main():
    accs = parse_accounts(OUTLOOK_FILE)
    limit = next((int(sys.argv[i + 1]) for i, a in enumerate(sys.argv)
                  if a == "--limit"), len(accs))
    start = next((int(sys.argv[i + 1]) for i, a in enumerate(sys.argv)
                  if a == "--start-from"), 0)
    todo = accs[start:start + limit]
    log(f"{len(todo)} accounts to process (from #{start})")
    await pool.reload()
    results = []
    for i, acc in enumerate(todo, start=start):
        try:
            r = await farm_one(acc, i)
        except Exception as e:
            r = {"email": acc["email"], "status": "fail", "error": str(e)}
        results.append(r)
        json.dump(results, open(RESULTS, "w"), indent=1)
        log(f"[{i}] -> {r['status']} {r.get('error','')}")
        await asyncio.sleep(2)  # pace OTP sends
    ok = [r for r in results if r["status"] == "ok"]
    log(f"done: {len(ok)}/{len(results)} ok")
    for r in results:
        bal = (r.get("balance") or {}).get("balanceUsd", "?")
        elig = r.get("eligibility") or {}
        print(f"{r['email']:45s} | {r['status']:4s} | ${bal} | elig={elig.get('eligible')} "
              f"verif={elig.get('verificationRequired')} | {r.get('error','')}")


if __name__ == "__main__":
    asyncio.run(main())
