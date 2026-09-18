"""Boomlify temp gmail -> vorflux OTP -> eligibility check."""
import httpx
import json
import re
import uuid
import time
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

h = open(r"D:\Devin2\boomlify_main.js", encoding="utf-8").read()
m = re.search(r"getTransportKeyRing\(\)\{try\{const e=JSON\.parse\('([^']+)'\)", h)
keyring = json.loads(m.group(1))
DK = "7a9b3c8d2e1f4g5h6i9j0k8l2m4n6o8p"


def dec(hexstr, key):
    k = key.encode()
    b = bytes.fromhex(hexstr)
    return bytes(bytearray(b[i] ^ k[i % len(k)] for i in range(len(b)))).decode("utf-8", "replace")


def dec_resp(r):
    try:
        d = r.json()
    except Exception:
        return {"_raw": r.text[:200], "_s": r.status_code}
    if isinstance(d, dict) and "encrypted" in d:
        out = dec(d["encrypted"], keyring.get(r.headers.get("x-enc-key-id"), DK))
        try:
            return json.loads(out)
        except Exception:
            return {"_raw": out[:300]}
    return d


def main():
    c = httpx.Client(trust_env=False, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 Chrome/131",
        "Referer": "https://boomlify.com/zh/gmail-temp-mail/",
        "Origin": "https://boomlify.com"})
    vf = httpx.Client(trust_env=False, timeout=30)
    uid = "anon_" + uuid.uuid4().hex[:12]
    H = {"X-Boomlify-Device-Id": str(uuid.uuid4()),
         "X-User-Language": "en", "Content-Type": "application/json"}
    r = c.post(f"https://v1.boomlify.com/gmail/public/create?userId={uid}",
               headers=H, json={"strategy": "dot", "domain": "gmail.com",
                                "version": "1.0.0"})
    mb = dec_resp(r)["alias"]
    print("mailbox:", mb)
    r = vf.post("https://us1.vorflux.com/api/auth/passwordless/start",
                json={"email": mb})
    print("vf start:", r.status_code, r.text[:120])
    ep = ("https://v1.boomlify.com/gmail/public/emails/"
          + mb.replace("@", "%40") + f"?userId={uid}")
    otp = None
    for i in range(45):
        time.sleep(8)
        d = dec_resp(c.get(ep, headers=H))
        s = json.dumps(d)
        if "vorflux" in s.lower():
            print("mail:", s[:700])
            m2 = re.search(r"\b(\d{6})\b", s)
            if m2:
                otp = m2.group(1)
                break
        if i % 5 == 0:
            print(f"[{i*8}s]", s[:150])
    print("OTP:", otp)
    if not otp:
        return
    r = vf.post("https://us1.vorflux.com/api/auth/passwordless/verify",
                json={"email": mb, "code": otp})
    dd = r.json()
    print("verify:", r.status_code)
    if not dd.get("access_token"):
        print(r.text[:300])
        return
    HH = {"Authorization": f"Bearer {dd['access_token']}",
          "Content-Type": "application/json"}
    ma = vf.post("https://us1.vorflux.com/query", headers=HH,
                 json={"query": "query{myAccounts{accountId}}"}).json()
    acct = ma["data"]["myAccounts"][0]["accountId"]
    HH["X-Account-ID"] = acct
    print("account:", acct)
    e = vf.post("https://us1.vorflux.com/query", headers=HH, json={"query":
        "query{signupBonusEligibility{eligible alreadyGranted "
        "verificationRequired providerIdentityAlreadyCredited}}"}).json()
    print("ELIG:", json.dumps(e))
    cl = vf.post("https://us1.vorflux.com/query", headers=HH, json={"query":
        "mutation{claimSignupBonus{eligible alreadyGranted "
        "verificationRequired providerIdentityAlreadyCredited}}"}).json()
    print("CLAIM:", json.dumps(cl))
    bal = vf.post("https://us1.vorflux.com/query", headers=HH, json={
        "query": "query{creditBalance{balanceCredits balanceUsd}}"}).json()
    print("BAL:", json.dumps(bal))
    json.dump({"email": mb, "account": acct,
               "refresh_token": dd.get("refresh_token"),
               "elig": e, "claim": cl, "bal": bal},
              open(r"D:\Devin2\boomlify_result.json", "w"), indent=1)


if __name__ == "__main__":
    main()
