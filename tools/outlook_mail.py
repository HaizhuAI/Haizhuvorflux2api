"""Outlook MSA refresh-token -> IMAP access token -> read mail."""
import email
import email.header
import imaplib
import re
import time
import httpx

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_HOST = "outlook.office365.com"
SCOPE = ("https://outlook.office.com/IMAP.AccessAsUser.All "
         "https://outlook.office.com/SMTP.Send offline_access")


def parse_accounts(path: str) -> list[dict]:
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or "----x----" not in line:
            continue
        parts = line.split("----")
        email_addr = parts[0]
        token = parts[2] if len(parts) > 2 else ""
        client = parts[-1] if re.match(r"^[0-9a-f-]{36}$", parts[-1]) else ""
        out.append({"email": email_addr, "refresh_token": token,
                    "client_id": client})
    return out


def refresh(acc: dict) -> str:
    r = httpx.post(TOKEN_URL, data={
        "client_id": acc["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": acc["refresh_token"],
        "scope": SCOPE,
    }, timeout=30)
    d = r.json()
    if not r.is_success or not d.get("access_token"):
        raise RuntimeError(d.get("error_description") or d.get("error")
                           or f"HTTP {r.status_code}")
    return d["access_token"]


class Inbox:
    def __init__(self, email_addr: str, access_token: str):
        self.email = email_addr
        self.imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
        auth = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
        typ, _ = self.imap.authenticate("XOAUTH2", lambda _: auth)
        if typ != "OK":
            raise RuntimeError("IMAP XOAUTH2 failed")
        self.imap.select("INBOX")

    def recent_messages(self, limit: int = 15) -> list[dict]:
        """Return [{subject, from, text}] newest-first."""
        typ, data = self.imap.search(None, "ALL")
        ids = data[0].split()[-limit:][::-1]
        out = []
        for mid in ids:
            typ, msg_data = self.imap.fetch(mid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subj = _decode_hdr(msg.get("Subject", ""))
            frm = _decode_hdr(msg.get("From", ""))
            text = _body_text(msg)
            out.append({"subject": subj, "from": frm, "text": text})
        return out

    def find_code(self, sender_hint: str = "vorflux") -> str | None:
        for m in self.recent_messages():
            blob = m["subject"] + " " + m["from"] + " " + m["text"]
            if sender_hint in blob.lower():
                mt = re.search(r"\b(\d{6})\b", m["text"])
                if mt:
                    return mt.group(1)
        return None

    def close(self):
        try:
            self.imap.logout()
        except Exception:
            pass


def _decode_hdr(v: str) -> str:
    parts = []
    for txt, enc in email.header.decode_header(v or ""):
        parts.append(txt.decode(enc or "utf-8", "replace")
                     if isinstance(txt, bytes) else txt)
    return "".join(parts)


def _body_text(msg) -> str:
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    texts.append(part.get_payload(decode=True)
                                 .decode(part.get_content_charset() or "utf-8",
                                         "replace"))
                except Exception:
                    pass
    else:
        try:
            texts.append(msg.get_payload(decode=True)
                         .decode(msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            pass
    return "\n".join(texts)


if __name__ == "__main__":
    accs = parse_accounts(r"D:\Outlook.txt")
    print(f"{len(accs)} accounts")
    acc = accs[0]
    print("testing", acc["email"])
    tok = refresh(acc)
    print("imap token ok")
    box = Inbox(acc["email"], tok)
    msgs = box.recent_messages(5)
    print(f"{len(msgs)} messages:")
    for m in msgs:
        print(" -", m["from"][:40], "|", m["subject"][:60])
    box.close()
