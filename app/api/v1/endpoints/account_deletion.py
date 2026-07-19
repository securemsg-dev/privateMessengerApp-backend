from __future__ import annotations

"""
app/api/v1/endpoints/account_deletion.py
─────────────────────────────────────────
Public, unauthenticated web page documenting how a user deletes their Cricchat
account and exactly what data is removed.

Why this exists: Google Play's account-deletion policy requires apps that let
users create an account to publish a deletion URL reachable from a browser
WITHOUT installing the app. The in-app path alone does not satisfy it.

Deliberately informational — this page does NOT delete anything. A public form
taking private_number + delete_password would create an unauthenticated
destructive endpoint (credential stuffing), and worse, its responses would leak
whether a given private number exists. auth_service.authenticate_or_delete_intent
goes out of its way to keep the login/delete/unknown branches indistinguishable;
a web form that says "deleted" vs "not found" would hand that back. Google
accepts an instructions-plus-contact page, so this is the boring, safe option.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import settings

router = APIRouter()

# Deliberately NOT settings.APP_NAME — that is an operational/environment label
# ("Private Messenger (Local)" in dev) and would leak the wrong brand onto a
# public page that Google reviewers read. The store-facing name is fixed.
APP_DISPLAY_NAME = "Cricchat"


def _render_page() -> str:
    app_name = APP_DISPLAY_NAME
    support_email = settings.SUPPORT_EMAIL

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delete your {app_name} account</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #ffffff;
    --surface: #f6f8fa;
    --text: #1a1d21;
    --muted: #5c6570;
    --border: #e1e5ea;
    --accent: #0b62d0;
    --danger: #b3261e;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14171a;
      --surface: #1d2126;
      --text: #e8eaed;
      --muted: #9aa4af;
      --border: #2c3238;
      --accent: #6ba7f5;
      --danger: #f2b8b5;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2rem 1.25rem 4rem;
    background: var(--bg);
    color: var(--text);
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
  }}
  main {{ max-width: 44rem; margin: 0 auto; }}
  h1 {{ font-size: 1.75rem; line-height: 1.25; margin: 0 0 .5rem; }}
  h2 {{ font-size: 1.15rem; margin: 2.25rem 0 .75rem; }}
  .lede {{ color: var(--muted); margin: 0 0 2rem; }}
  ol, ul {{ padding-left: 1.25rem; }}
  li {{ margin: .4rem 0; }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin: 1rem 0;
  }}
  .danger {{ color: var(--danger); font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin: .5rem 0; }}
  th, td {{
    text-align: left; padding: .55rem .5rem;
    border-bottom: 1px solid var(--border); vertical-align: top;
  }}
  th {{ color: var(--muted); font-weight: 600; font-size: .875rem; }}
  code {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 4px; padding: .1rem .35rem; font-size: .9em;
  }}
  a {{ color: var(--accent); }}
  footer {{
    margin-top: 3rem; padding-top: 1.25rem;
    border-top: 1px solid var(--border);
    color: var(--muted); font-size: .875rem;
  }}
  .scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<main>
  <h1>Delete your {app_name} account</h1>
  <p class="lede">
    You can permanently delete your {app_name} account and all associated data
    at any time, directly from the app. This page explains how, and exactly
    what gets removed.
  </p>

  <h2>Delete your account from the app</h2>
  <ol>
    <li>Open {app_name} and go to the sign-in screen.</li>
    <li>Enter your 10-digit private number.</li>
    <li>
      Enter your <strong>delete password</strong> — the second password you
      chose when you registered — instead of your normal sign-in password.
    </li>
    <li>Tap <strong>Sign in</strong>.</li>
    <li>
      A confirmation prompt appears: <em>“Permanently delete account?”</em>
      Tap <strong>Yes, delete</strong>.
    </li>
  </ol>

  <div class="card">
    <p class="danger" style="margin:.25rem 0;">This cannot be undone.</p>
    <p style="margin:.25rem 0;">
      Deletion is immediate and permanent. There is no recovery period and no
      backup we can restore from — by design, we hold no key that could
      recover your messages.
    </p>
  </div>

  <h2>What is deleted</h2>
  <p>Confirming deletion removes the following from our servers immediately:</p>
  <div class="scroll">
  <table>
    <thead>
      <tr><th>Data</th><th>What happens</th></tr>
    </thead>
    <tbody>
      <tr><td>Account record</td><td>Deleted — private number, password hashes, and profile</td></tr>
      <tr><td>Messages</td><td>Deleted — all encrypted messages you sent or received</td></tr>
      <tr><td>Media</td><td>Deleted — encrypted photos, videos, voice notes, and avatar files are removed from storage</td></tr>
      <tr><td>Contacts</td><td>Deleted — your contact list and conversation memberships</td></tr>
      <tr><td>Devices &amp; sessions</td><td>Deleted — every registered device, sign-in session, and push token</td></tr>
      <tr><td>Encryption keys</td><td>Deleted — your public key record on the server, and all local keys on your device</td></tr>
      <tr><td>Call history</td><td>Deleted — call records associated with your account</td></tr>
    </tbody>
  </table>
  </div>

  <h2>Data retention</h2>
  <ul>
    <li>
      <strong>No retention period.</strong> Your data is deleted at the moment
      you confirm — it is not held in a grace period or soft-delete state.
    </li>
    <li>
      <strong>Message content was never readable by us.</strong> {app_name} is
      end-to-end encrypted, so messages and media are stored only as ciphertext
      we cannot decrypt. Deletion removes that ciphertext.
    </li>
    <li>
      <strong>Copies held by other people.</strong> Messages you already sent
      may remain on the recipient's device, in the same way a sent SMS does.
      We cannot remove those.
    </li>
    <li>
      <strong>Server logs.</strong> Operational logs may retain a truncated,
      non-identifying reference (the last four digits of a private number) for
      up to 30 days for abuse and reliability purposes, then roll off
      automatically.
    </li>
  </ul>

  <h2>Can't access the app?</h2>
  <p>
    If you have lost your device or can no longer sign in, email
    <a href="mailto:{support_email}?subject=Cricchat%20account%20deletion%20request">{support_email}</a>
    from a contact address you can verify, and include your 10-digit private
    number. We will verify ownership before deleting the account and will
    confirm by email once it is done.
  </p>

  <footer>
    {app_name} — account deletion policy. Last updated July 2026.
  </footer>
</main>
</body>
</html>"""


@router.get(
    "/delete-account",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Public account-deletion instructions (Google Play requirement)",
    tags=["Legal"],
)
async def delete_account_page() -> HTMLResponse:
    """Public deletion-instructions page. No auth, no side effects."""
    return HTMLResponse(content=_render_page())
