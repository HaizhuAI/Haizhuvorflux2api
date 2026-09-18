"""Re-login each Outlook account via OTP, check signupBonusEligibility,
auto-claim when verificationRequired clears. Run periodically (e.g. every few hours).
Usage: python bonus_watch.py [account_index ...]"""
import sys
import re
import time
import json
import imaplib
import email as em
import httpx

sys.path.insert(0, r"D:\Devin2\vorflux-gateway\tools")
from outlook_mail import parse_accounts, refresh

BASE = "https://us1.vorflux.com"
IMAP_HOST = "outlook.office365.com"
RESULTS = r"D:\Devin2\bonus_watch_results.json"


def get_otp(acc, msa_tok, since_ts, timeout=150):
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    auth = f"user={acc['email']}\x01auth=Bearer {msa_tok}\x01\x01"
    typ, _ = imap.authenticate("XOAUTH2", lambda _: auth)
    if typ != "OK":
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        for folder in ("INBOX", "Junk"):
            try:
                imap.select(folder, readonly=True)
                typ, data = imap.search(None, "ALL")
                for mid in data[0].split()[-6:][::-1]:
                    typ, md = imap.fetch(mid, "(RFC822)")
                    msg = em.message_from_bytes(md[0][1])
                    try:
                        d = em.utils.parsedate_to_datetime(msg.get("Date"))
                        if d.timestamp() < since_ts - 90:
                            continue
                    except Exception:
                        pass
                    blob = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/plain", "text/html"):
                                try:
                                    blob += part.get_payload(decode=True).decode(
                                        "utf-8", "replace")
                                except Exception:
                                    pass
                    else:
                        try:
                            blob = msg.get_payload(decode=True).decode(
                                "utf-8", "replace")
                        except Exception:
                            pass
                    if "vorflux" in (str(msg.get("Subject", "")) + blob).lower():
                        m = re.search(r"\b(\d{6})\b", blob)
                        if m:
                            imap.logout()
                            return m.group(1)
            except Exception:
                pass
        time.sleep(6)
    imap.logout()
    return None


def run(acc):
    c = httpx.Client(trust_env=False, timeout=30)
    out = {"email": acc["email"], "ts": time.strftime("%H:%M:%S")}
    try:
        msa = refresh(acc)
        r = c.post(f"{BASE}/api/auth/passwordless/start",
                   json={"email": acc["email"]})
        if not r.is_success:
            out["error"] = f"start {r.status_code}"
            return out
        ts = time.time()
        code = get_otp(acc, msa, ts)
        if not code:
            out["error"] = "no OTP"
            return out
        r = c.post(f"{BASE}/api/auth/passwordless/verify",
                   json={"email": acc["email"], "code": code})
        d = r.json()
        if not d.get("access_token"):
            out["error"] = f"verify {r.status_code} {r.text[:120]}"
            return out
        out["refresh_token"] = d["refresh_token"]
        H = {"Authorization": f"Bearer {d['access_token']}",
             "Content-Type": "application/json"}
        ma = c.post(f"{BASE}/query", headers=H, json={
            "query": "query{myAccounts{accountId}}"}).json()
        acct = ma["data"]["myAccounts"][0]["accountId"]
        out["account_id"] = acct
        H["X-Account-ID"] = acct
        elig = c.post(f"{BASE}/query", headers=H, json={"query":
            "query{signupBonusEligibility{eligible alreadyGranted "
            "verificationRequired providerIdentityAlreadyCredited}}"}).json()
        e = elig["data"]["signupBonusEligibility"]
        out["elig"] = e
        if not e["verificationRequired"] and not e["alreadyGranted"]:
            cl = c.post(f"{BASE}/query", headers=H, json={"query":
                "mutation{claimSignupBonus{eligible alreadyGranted "
                "verificationRequired}}"}).json()
            out["claim"] = cl.get("data", cl)
            bal = c.post(f"{BASE}/query", headers=H, json={
                "query": "query{creditBalance{balanceCredits}}"}).json()
            out["balance"] = bal["data"]["creditBalance"]["balanceCredits"]
    except Exception as ex:
        out["error"] = str(ex)[:200]
    return out


def main():
    accs = parse_accounts(r"D:\Outlook.txt")
    idxs = [int(x) for x in sys.argv[1:]] or range(len(accs))
    results = []
    for i in idxs:
        acc = accs[i]
        print(f"[{i+1}/{len(accs)}] {acc['email']}")
        r = run(acc)
        print("   ", json.dumps({k: v for k, v in r.items()
                                  if k != "refresh_token"}))
        results.append(r)
    try:
        old = json.load(open(RESULTS))
    except Exception:
        old = []
    json.dump(old + results, open(RESULTS, "w"), indent=1)
    print("saved ->", RESULTS)


if __name__ == "__main__":
    main()
