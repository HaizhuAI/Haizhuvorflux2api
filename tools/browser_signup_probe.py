"""Full browser signup: real Chrome -> email OTP -> eligibility check.
Captures all /query + /api/auth traffic to compare vs raw-API signup."""
import json
import re
import sys
import time
import imaplib
import email as em
from playwright.sync_api import sync_playwright

sys.path.insert(0, r"D:\Devin2\vorflux-gateway\tools")
from outlook_mail import parse_accounts, refresh

BASE = "https://us1.vorflux.com"
ACC_INDEX = 2
IMAP_HOST = "outlook.office365.com"
LOG = []


def log_req(req):
    if "vorflux.com" not in req.url and "auth0.com" not in req.url:
        return
    if req.method == "GET" and "/api/" not in req.url and "/query" not in req.url:
        return
    e = {"m": req.method, "url": req.url, "h": dict(req.headers)}
    try:
        if req.post_data and len(req.post_data) < 6000:
            e["body"] = req.post_data
    except Exception:
        pass
    LOG.append(e)
    print(f"  >> {req.method} {req.url[:90]}")


def poll_otp(acc, access_token, since_ts, timeout=240):
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    auth = f"user={acc['email']}\x01auth=Bearer {access_token}\x01\x01"
    typ, _ = imap.authenticate("XOAUTH2", lambda _: auth)
    if typ != "OK":
        raise RuntimeError("imap auth failed")
    t0 = time.time()
    while time.time() - t0 < timeout:
        for folder in ("INBOX", "Junk"):
            try:
                imap.select(folder, readonly=True)
                typ, data = imap.search(None, "ALL")
                ids = data[0].split()[-8:][::-1]
                for mid in ids:
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
                                        part.get_content_charset() or "utf-8", "replace")
                                except Exception:
                                    pass
                    else:
                        try:
                            blob = msg.get_payload(decode=True).decode(
                                msg.get_content_charset() or "utf-8", "replace")
                        except Exception:
                            pass
                    subj = str(msg.get("Subject", ""))
                    if "vorflux" in (subj + blob).lower():
                        mt = re.search(r"\b(\d{6})\b", blob)
                        if mt:
                            imap.logout()
                            return mt.group(1)
            except Exception as ex:
                print(f"  [{folder}] err {ex}")
        print(f"  ...polling {int(time.time()-t0)}s")
        time.sleep(6)
    imap.logout()
    return None


def main():
    accs = parse_accounts(r"D:\Outlook.txt")
    acc = accs[ACC_INDEX]
    print("account:", acc["email"])
    msa_tok = refresh(acc)
    print("msa token ok")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US", timezone_id="America/Los_Angeles")
        page = ctx.new_page()
        page.on("request", log_req)
        page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        # click "Continue with email instead"
        page.click("text=Continue with email")
        page.wait_for_timeout(1500)
        print("after email click, inputs:")
        for i, inp in enumerate(page.query_selector_all("input")):
            print(f"  input[{i}] type={inp.get_attribute('type')} "
                  f"ph={inp.get_attribute('placeholder')} "
                  f"name={inp.get_attribute('name')}")
        page.screenshot(path=r"D:\Devin2\signup_email.png")

        # fill email
        email_input = page.query_selector("input[type='email']") or \
            page.query_selector("input")
        email_input.fill(acc["email"])
        page.wait_for_timeout(400)
        # find the submit/continue button
        ts = time.time()
        page.click("button:has-text('Continue'), button:has-text('Send'), "
                   "button[type='submit']")
        print("submitted, polling OTP...")
        page.wait_for_timeout(3000)
        page.screenshot(path=r"D:\Devin2\signup_code.png")
        for i, inp in enumerate(page.query_selector_all("input")):
            print(f"  code-input[{i}] type={inp.get_attribute('type')} "
                  f"ph={inp.get_attribute('placeholder')} "
                  f"maxlen={inp.get_attribute('maxlength')}")

        code = poll_otp(acc, msa_tok, ts)
        print("OTP:", code)
        if not code:
            print("NO OTP - dumping traffic")
            with open(r"D:\Devin2\signup_traffic2.json", "w") as f:
                json.dump(LOG, f, indent=2)
            browser.close()
            return

        # fill the code - may be 6 separate inputs or one
        code_inputs = page.query_selector_all("input")
        visible = [i for i in code_inputs if i.is_visible()]
        if len(visible) >= 6:
            for idx, ch in enumerate(code[:6]):
                visible[idx].fill(ch)
        else:
            visible[0].fill(code)
        page.wait_for_timeout(800)
        page.screenshot(path=r"D:\Devin2\signup_filled.png")
        # submit if needed
        try:
            page.click("button:has-text('Verify'), button:has-text('Continue'), "
                       "button[type='submit']", timeout=3000)
        except Exception:
            pass
        print("waiting for app...")
        page.wait_for_timeout(10000)
        print("url now:", page.url)
        page.screenshot(path=r"D:\Devin2\signup_done.png")

        # check eligibility in-page
        result = page.evaluate("""(async()=>{
          const keys=Object.keys(localStorage).filter(k=>k.startsWith('@@auth0spajs@@'));
          let tok=null;
          for(const k of keys){try{const v=JSON.parse(localStorage.getItem(k));
            if(v?.body?.access_token){tok=v.body.access_token;break}}catch(e){}}
          if(!tok) return {err:'no token', keys};
          const q=(query,variables={},acct)=>fetch('/query',{method:'POST',
            headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok,
            ...(acct?{'X-Account-ID':acct}:{})},
            body:JSON.stringify({query,variables})}).then(r=>r.json());
          const accts=await q('query{myAccounts{accountId accountName accountType}}');
          const acct=accts?.data?.myAccounts?.[0]?.accountId;
          if(!acct) return {err:'no acct', accts};
          const elig=await q('query{signupBonusEligibility{eligible alreadyGranted verificationRequired providerIdentityAlreadyCredited}}',{},acct);
          const claim=await q('mutation{claimSignupBonus{eligible alreadyGranted verificationRequired providerIdentityAlreadyCredited}}',{},acct);
          const bal=await q('query{creditBalance{balanceCredits balanceUsd}}',{},acct);
          const rt=Object.keys(localStorage).filter(k=>k.startsWith('@@auth0spajs@@'))
            .map(k=>{try{return JSON.parse(localStorage.getItem(k)).body.refresh_token}catch(e){return null}}).filter(Boolean);
          return {acct, elig, claim, bal, rt};
        })()""")
        print("=== RESULT ===")
        print(json.dumps(result, indent=2))
        with open(r"D:\Devin2\signup_result.json", "w") as f:
            json.dump(result, f, indent=2)
        with open(r"D:\Devin2\signup_traffic2.json", "w") as f:
            json.dump(LOG, f, indent=2)
        page.wait_for_timeout(2000)
        browser.close()


if __name__ == "__main__":
    main()
