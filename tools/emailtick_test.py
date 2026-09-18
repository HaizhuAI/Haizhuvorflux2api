"""EmailTick v4: use site's own flow to get mailbox, poll rendered inbox."""
import json
import re
import time
import httpx
from playwright.sync_api import sync_playwright

BASE = "https://us1.vorflux.com"


def main():
    vf = httpx.Client(trust_env=False, timeout=30)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.emailtick.com/zh",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        cur = page.evaluate("document.querySelector('#mailbox')?.value || ''")
        print("page mailbox:", cur)
        if "@" not in cur:
            for sel in ("#modalChange", "button:has-text('随机')",
                        "button:has-text('Random')"):
                try:
                    page.click(sel, timeout=2000)
                    print("clicked", sel)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(3000)
            cur = page.evaluate(
                "document.querySelector('#mailbox')?.value || ''")
            print("after click:", cur)
        mb = cur
        if "@" not in mb:
            print("no mailbox")
            page.screenshot(path=r"D:\Devin2\emailtick_ui.png")
            browser.close()
            return
        code = page.evaluate("document.querySelector('#code')?.value || ''")
        print("USING:", mb, "| code:", code[:24])

        r = vf.post(f"{BASE}/api/auth/passwordless/start",
                    json={"email": mb})
        print("vf start:", r.status_code, r.text[:120])
        if not r.is_success:
            browser.close()
            return

        otp = None
        for i in range(45):
            time.sleep(8)
            try:
                page.evaluate("""(async(em,cd)=>{
                    try{await fetch('/get-emails',{method:'POST',
                      headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({email:em,code:cd})});}catch(e){}
                })""", [mb, code])
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                html = page.content()
                txt = re.sub(r"<[^>]+>", " ",
                             re.sub(r"<script.*?</script>", "", html,
                                    flags=re.S))
                if "vorflux" in txt.lower():
                    m = re.search(r"\b(\d{6})\b", txt)
                    if m:
                        otp = m.group(1)
                        print(f"FOUND: {otp}")
                        break
                    idx = txt.lower().find("vorflux")
                    print("mail seen:", txt[max(0, idx - 150):idx + 300])
                elif i % 5 == 0:
                    print(f"[{i*8}s] no mail")
            except Exception as e:
                print("poll err", e)
        print("OTP:", otp)
        page.screenshot(path=r"D:\Devin2\emailtick_inbox.png")
        open(r"D:\Devin2\emailtick_page.html", "w",
             encoding="utf-8").write(page.content())
        if not otp:
            browser.close()
            return

        r = vf.post(f"{BASE}/api/auth/passwordless/verify",
                    json={"email": mb, "code": otp})
        dd = r.json()
        print("verify:", r.status_code)
        if not dd.get("access_token"):
            print(r.text[:300])
            browser.close()
            return
        H = {"Authorization": f"Bearer {dd['access_token']}",
             "Content-Type": "application/json"}
        ma = vf.post(f"{BASE}/query", headers=H, json={
            "query": "query{myAccounts{accountId}}"}).json()
        acct = ma["data"]["myAccounts"][0]["accountId"]
        print("account:", acct)
        H["X-Account-ID"] = acct
        e = vf.post(f"{BASE}/query", headers=H, json={"query":
            "query{signupBonusEligibility{eligible alreadyGranted "
            "verificationRequired providerIdentityAlreadyCredited}}"}).json()
        print("ELIG:", json.dumps(e))
        cl = vf.post(f"{BASE}/query", headers=H, json={"query":
            "mutation{claimSignupBonus{eligible alreadyGranted "
            "verificationRequired providerIdentityAlreadyCredited}}"}).json()
        print("CLAIM:", json.dumps(cl))
        bal = vf.post(f"{BASE}/query", headers=H, json={
            "query": "query{creditBalance{balanceCredits balanceUsd}}"}).json()
        print("BAL:", json.dumps(bal))
        json.dump({"email": mb, "account": acct, "tokens": dd,
                   "elig": e, "claim": cl, "bal": bal},
                  open(r"D:\Devin2\emailtick_result.json", "w"), indent=1)
        browser.close()


if __name__ == "__main__":
    main()
