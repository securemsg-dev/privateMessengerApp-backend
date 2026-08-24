from __future__ import annotations

"""
app/api/v1/endpoints/legal.py
──────────────────────────────
Public, unauthenticated legal pages served at the domain root:

  • /privacy         — privacy policy (required by the Play Store listing)
  • /delete-account  — account deletion instructions (required by Data safety)
  • /report-abuse    — abuse reporting + blocking (required by the UGC policy)

Both must be reachable in a plain browser WITHOUT installing the app; Google
reviewers open them directly, so they cannot live behind auth or a deep link.

Shared page shell lives here so the two pages cannot drift apart visually.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import settings

router = APIRouter()

# Deliberately NOT settings.APP_NAME — that is an operational/environment label
# ("Private Messenger (Local)" in dev) and would leak the wrong brand onto a
# public page that Google reviewers read. The store-facing name is fixed.
APP_DISPLAY_NAME = "Cricchat"

# Bump when the policy text materially changes; shown in each page footer.
LAST_UPDATED = "24 August 2026"

_CSS = """
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --surface: #f6f8fa; --text: #1a1d21; --muted: #5c6570;
    --border: #e1e5ea; --accent: #0b62d0; --danger: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171a; --surface: #1d2126; --text: #e8eaed; --muted: #9aa4af;
      --border: #2c3238; --accent: #6ba7f5; --danger: #f2b8b5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1.25rem 4rem;
    background: var(--bg); color: var(--text);
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
  }
  main { max-width: 44rem; margin: 0 auto; }
  h1 { font-size: 1.75rem; line-height: 1.25; margin: 0 0 .5rem; }
  h2 { font-size: 1.15rem; margin: 2.25rem 0 .75rem; }
  .lede { color: var(--muted); margin: 0 0 2rem; }
  ol, ul { padding-left: 1.25rem; }
  li { margin: .4rem 0; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem 1.25rem; margin: 1rem 0;
  }
  .danger { color: var(--danger); font-weight: 600; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
  th, td {
    text-align: left; padding: .55rem .5rem;
    border-bottom: 1px solid var(--border); vertical-align: top;
  }
  th { color: var(--muted); font-weight: 600; font-size: .875rem; }
  a { color: var(--accent); }
  footer {
    margin-top: 3rem; padding-top: 1.25rem;
    border-top: 1px solid var(--border);
    color: var(--muted); font-size: .875rem;
  }
  .scroll { overflow-x: auto; }
"""


def _shell(title: str, body: str) -> str:
    """Wrap page body in the shared HTML/CSS shell."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<main>\n"
        f"{body}\n"
        "</main>\n</body>\n</html>"
    )


# ── Privacy policy ────────────────────────────────────────────────────────────

def _privacy_body() -> str:
    app = APP_DISPLAY_NAME
    email = settings.SUPPORT_EMAIL

    # Omit rather than guess: an unset entity renders no sentence at all, so we
    # never publish a placeholder like "Your Company Ltd" to a live policy.
    operator = (
        f" {app} is operated by {settings.LEGAL_ENTITY_NAME}, the data "
        "controller responsible for the information described here."
        if settings.LEGAL_ENTITY_NAME
        else ""
    )
    turn_vendor = settings.TURN_PROVIDER_NAME or "Our TURN relay provider"

    return f"""
  <h1>{app} Privacy Policy</h1>
  <p class="lede">
    {app} is a private, end-to-end encrypted messenger. This policy explains
    what we collect, what we cannot see, and the choices you have.{operator}
    Last updated {LAST_UPDATED}.
  </p>

  <div class="card">
    <p style="margin:.25rem 0;">
      <strong>The short version.</strong> We do not ask for your phone number,
      email address, or real name. We cannot read your messages, calls, or
      media — they are encrypted on your device with keys we never hold. We do
      not use analytics, advertising, or tracking of any kind, and we do not
      sell or share your data.
    </p>
  </div>

  <h2>Information we collect</h2>
  <div class="scroll">
  <table>
    <thead><tr><th>Data</th><th>Why</th></tr></thead>
    <tbody>
      <tr>
        <td><strong>Private number</strong> — a 10-digit identifier issued by
        the app when you register</td>
        <td>Your account identity and how other users reach you. It is not a
        phone number and is not linked to one.</td>
      </tr>
      <tr>
        <td><strong>Password hashes</strong> — for your sign-in password and
        your delete password</td>
        <td>Authentication. Your device never sends us your actual password —
        it sends a value derived from it, which we store only as a one-way
        hash. Your password can never be recovered from what we hold.</td>
      </tr>
      <tr>
        <td><strong>Profile details</strong> — optional display name, bio, and
        profile picture</td>
        <td>Shown to people you talk to. All optional; leave them blank and
        nothing is stored.</td>
      </tr>
      <tr>
        <td><strong>Public encryption key</strong></td>
        <td>Distributed to people you message so their device can encrypt to
        you. The matching private key stays on your device; only an encrypted
        backup you alone can unlock is stored — see the next row.</td>
      </tr>
      <tr>
        <td><strong>Encrypted key backup</strong></td>
        <td>A copy of your private key, encrypted on your device with a key
        derived from your password that we never receive. Stored so you can
        restore your messages when you sign in on a new device. We cannot
        decrypt it.</td>
      </tr>
      <tr>
        <td><strong>Encrypted messages and media</strong> — ciphertext only,
        plus size and media type</td>
        <td>Held so a message can be delivered to your other devices or while
        a recipient is offline. We cannot decrypt any of it.</td>
      </tr>
      <tr>
        <td><strong>Message metadata</strong> — sender, conversation, and
        sent/delivered/read timestamps</td>
        <td>Required to route messages and show delivery and read status.</td>
      </tr>
      <tr>
        <td><strong>Contacts you add</strong> — the private numbers you save
        and any nickname you give them</td>
        <td>Your in-app contact list. We never read your device's address book
        or phone contacts.</td>
      </tr>
      <tr>
        <td><strong>Device records</strong> — device name, platform, and push
        notification token</td>
        <td>To deliver your messages to the right device and send
        notifications.</td>
      </tr>
      <tr>
        <td><strong>Call records</strong> — who called whom, start/end time,
        and how the call ended</td>
        <td>Your in-app call history. <strong>Call audio and video are never
        recorded or stored.</strong></td>
      </tr>
    </tbody>
  </table>
  </div>

  <h2>What we do not collect</h2>
  <ul>
    <li>No phone number, email address, or real name is required to register.</li>
    <li>No access to your device's contacts, address book, or location.</li>
    <li>No analytics, advertising, tracking, or profiling SDKs of any kind.</li>
    <li>No readable message content, media, or call audio — with one narrow
        exception you control, described under <em>Abuse reports</em> below.</li>
  </ul>

  <h2>End-to-end encryption</h2>
  <p>
    Messages and media are encrypted on your device before they are sent, and
    can only be decrypted by the intended recipient. Your private key is
    generated on your device. So that you can restore your conversations when
    you get a new phone, an encrypted backup of that key is stored on our
    servers — but it is encrypted on your device with a key derived from your
    password, and your password is never sent to us in a form we could use to
    unlock it. We cannot decrypt your backup, your messages, or your media, and
    cannot produce readable content in response to any request, including a
    lawful one. Because your backup is protected by your password, choose a
    strong, unique one. Calls are peer-to-peer and encrypted; we handle only
    the signalling needed to connect them.
  </p>
  <p>
    Push notifications never contain message content. They tell your device
    that something arrived; the app decrypts it locally.
  </p>

  <h2>Abuse reports</h2>
  <p>
    There is exactly one way message content can become readable to us, and
    only you can trigger it. When you report someone, you may choose to attach
    the messages you are reporting. If you tick <strong>Include recent
    messages</strong>, your device decrypts those specific messages and sends
    copies to our moderation team, where they are stored in readable form so a
    human can assess the report.
  </p>
  <ul>
    <li>This is off by default and always requires an explicit confirmation.</li>
    <li>Only the messages you select are attached — never your wider history,
        and never anything from your other conversations.</li>
    <li>If you decline, the report is still filed and reviewed using the reason
        and description you provide.</li>
    <li>Attached content is used solely to assess that report and enforce our
        rules. It is retained with the report record and is not used for any
        other purpose.</li>
    <li>We also store who reported whom, and when, so we can act on repeat
        offenders. The reported person is never told who reported them.</li>
  </ul>
  <p>
    Blocking someone involves no content at all — we record only that you
    blocked them. See <a href="/report-abuse">reporting abuse</a> for how both
    features work.
  </p>

  <h2>Permissions the app requests</h2>
  <ul>
    <li><strong>Camera</strong> — only when you take a photo/video or join a video call.</li>
    <li><strong>Microphone</strong> — only when you record a voice message or are on a call.</li>
    <li><strong>Photos and media</strong> — only to attach files you explicitly choose.</li>
    <li><strong>Bluetooth</strong> — to route call audio to headsets and speakers.</li>
    <li><strong>Notifications</strong> — to alert you to new messages and calls.</li>
  </ul>
  <p>Each permission is requested when the feature is first used, and you can decline or revoke it in your device settings.</p>

  <h2>Service providers</h2>
  <p>We keep third parties to the minimum needed to run the service:</p>
  <ul>
    <li><strong>Railway</strong> — hosts our servers and database.</li>
    <li><strong>Expo push notification service</strong> — relays notifications to your device. Content is never included.</li>
    <li><strong>{turn_vendor}</strong> — relays encrypted call traffic when a direct peer-to-peer connection is not possible. The relay sees only encrypted media and cannot decrypt it.</li>
    <li><strong>STUN servers</strong> — used to discover how your device can be reached for a call. A STUN server sees your device's network address but carries no call content.</li>
  </ul>
  <p>We do not sell, rent, or share your personal data with anyone for advertising or marketing.</p>

  <h2>How long we keep data</h2>
  <ul>
    <li><strong>Account data</strong> — kept until you delete your account, then removed immediately.</li>
    <li><strong>Messages and media</strong> — held as ciphertext until delivered and until you or the recipient delete them.</li>
    <li><strong>Unreferenced media</strong> — automatically purged after 30 days.</li>
    <li><strong>Expired sessions</strong> — removed automatically by routine housekeeping.</li>
  </ul>

  <h2>Your rights and choices</h2>
  <ul>
    <li><strong>Delete your account</strong> at any time — see
        <a href="/delete-account">our account deletion page</a>. Deletion is
        immediate and permanent.</li>
    <li><strong>Edit or remove</strong> your display name, bio, and profile picture in the app at any time.</li>
    <li><strong>Change your passwords</strong> in the app at any time.</li>
    <li><strong>Block or report</strong> anyone who contacts you — see
        <a href="/report-abuse">reporting abuse</a>. Blocking is immediate and
        the other person is not notified.</li>
    <li>Depending on where you live, you may have additional rights to access,
        correct, or export your data. Contact us and we will help — though note
        that we cannot produce message content, because we cannot decrypt it.</li>
  </ul>

  <h2>Children</h2>
  <p>
    {app} is not directed at children under 13, and we do not knowingly collect
    data from them. If you believe a child has created an account, contact us
    and we will remove it.
  </p>

  <h2>Changes to this policy</h2>
  <p>
    If this policy changes materially we will update the date above and, where
    the change is significant, notify you in the app.
  </p>

  <h2>Contact</h2>
  <p>
    Questions about this policy or your data:
    <a href="mailto:{email}?subject=Cricchat%20privacy%20enquiry">{email}</a>
  </p>

  <footer>{app} Privacy Policy — last updated {LAST_UPDATED}.</footer>
"""


# ── Account deletion ──────────────────────────────────────────────────────────
#
# Deliberately informational — this page does NOT delete anything. A public form
# taking private_number + delete_password would be an unauthenticated
# destructive endpoint, and its responses would leak whether a given private
# number exists. auth_service.authenticate_or_delete_intent works hard to keep
# the login/delete/unknown branches indistinguishable; a web form answering
# "deleted" vs "not found" would hand that straight back. Google accepts an
# instructions-plus-contact page, so this is the safe option.

def _delete_body() -> str:
    app = APP_DISPLAY_NAME
    email = settings.SUPPORT_EMAIL
    return f"""
  <h1>Delete your {app} account</h1>
  <p class="lede">
    You can permanently delete your {app} account and all associated data at
    any time, directly from the app. This page explains how, and exactly what
    gets removed.
  </p>

  <h2>Delete your account from the app</h2>
  <ol>
    <li>Open {app} and go to the sign-in screen.</li>
    <li>Enter your 10-digit private number.</li>
    <li>Enter your <strong>delete password</strong> — the second password you
        chose when you registered — instead of your normal sign-in password.</li>
    <li>Tap <strong>Sign in</strong>.</li>
    <li>A confirmation prompt appears: <em>“Permanently delete account?”</em>
        Tap <strong>Yes, delete</strong>.</li>
  </ol>

  <div class="card">
    <p class="danger" style="margin:.25rem 0;">This cannot be undone.</p>
    <p style="margin:.25rem 0;">
      Deletion is immediate and permanent. There is no recovery period and no
      backup we can restore from — by design, we hold no key that could recover
      your messages.
    </p>
  </div>

  <h2>What is deleted</h2>
  <p>Confirming deletion removes the following from our servers immediately:</p>
  <div class="scroll">
  <table>
    <thead><tr><th>Data</th><th>What happens</th></tr></thead>
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
    <li><strong>No retention period.</strong> Your data is deleted at the moment
        you confirm — it is not held in a grace period or soft-delete state.</li>
    <li><strong>Message content was never readable by us.</strong> {app} is
        end-to-end encrypted, so messages and media are stored only as
        ciphertext we cannot decrypt. Deletion removes that ciphertext.</li>
    <li><strong>Copies held by other people.</strong> Messages you already sent
        may remain on the recipient's device, in the same way a sent SMS does.
        We cannot remove those.</li>
  </ul>

  <h2>Can't access the app?</h2>
  <p>
    If you have lost your device or can no longer sign in, email
    <a href="mailto:{email}?subject=Cricchat%20account%20deletion%20request">{email}</a>
    from a contact address you can verify, and include your 10-digit private
    number. We will verify ownership before deleting the account and will
    confirm by email once it is done.
  </p>

  <p>See also our <a href="/privacy">Privacy Policy</a>.</p>

  <footer>{app} — account deletion policy. Last updated {LAST_UPDATED}.</footer>
"""


# ── Abuse reporting ───────────────────────────────────────────────────────────
# Google Play's User Generated Content policy asks for a reporting channel that
# works WITHOUT the app installed — a reviewer, or someone who has already
# uninstalled, must still be able to reach us. The in-app flow (Report on any
# chat or message) is the primary path; this page is the public fallback and
# the place where the response-time commitment is published.

def _report_abuse_body() -> str:
    app = APP_DISPLAY_NAME
    email = settings.ABUSE_EMAIL or settings.SUPPORT_EMAIL
    hours = settings.ABUSE_RESPONSE_HOURS
    return f"""
  <h1>Report abuse on {app}</h1>
  <p class="lede">
    {app} does not tolerate harassment, spam, hate speech, sexual content
    involving minors, threats of violence, or impersonation. Reports are
    reviewed by a human and acted on within {hours} hours.
  </p>

  <h2>Reporting from inside the app</h2>
  <p>This is the fastest route, and the only one that can attach evidence.</p>
  <ol>
    <li>Open the conversation with the person you want to report.</li>
    <li>Tap the <strong>⋮</strong> menu in the top-right corner.</li>
    <li>Tap <strong>Report</strong>, choose a reason, and add any context.</li>
    <li>Optionally tick <strong>Include recent messages</strong> so our
        moderators can see what was actually sent.</li>
  </ol>
  <p>
    You can also long-press any single message and tap <strong>Report</strong>
    to report just that message.
  </p>

  <div class="card">
    <p style="margin:.25rem 0;">
      <strong>{app} is end-to-end encrypted.</strong> We cannot read your
      messages, so we cannot see reported content unless you choose to attach
      it. Ticking <em>Include recent messages</em> sends copies of those
      specific messages to our moderation team unencrypted — nothing else in
      your account becomes readable, and reports without attachments are still
      reviewed on the reason and description you give.
    </p>
  </div>

  <h2>Blocking someone</h2>
  <p>
    Blocking is separate from reporting and takes effect immediately. Open the
    conversation, tap <strong>⋮</strong>, then <strong>Block</strong>. Their
    messages and calls stop reaching you, and they are not told they have been
    blocked. Manage your list under
    <strong>Settings → Blocked users</strong>.
  </p>

  <h2>Reporting without the app</h2>
  <p>
    If you have uninstalled {app}, cannot sign in, or are reporting on behalf
    of someone else, email
    <a href="mailto:{email}?subject=Cricchat%20abuse%20report">{email}</a>.
    Please include:
  </p>
  <ul>
    <li>The 10-digit private number of the account you are reporting.</li>
    <li>What happened, and roughly when.</li>
    <li>Screenshots, if you have them.</li>
  </ul>

  <h2>What we do with a report</h2>
  <ul>
    <li>Every report is reviewed by a person within {hours} hours.</li>
    <li>Depending on severity we warn, suspend, or permanently remove the
        reported account.</li>
    <li>Reports involving child safety or credible threats of violence are
        prioritised and may be referred to law enforcement.</li>
    <li>The reported account is never told who reported it.</li>
  </ul>

  <p>See also our <a href="/privacy">Privacy Policy</a>.</p>

  <footer>{app} — abuse reporting policy. Last updated {LAST_UPDATED}.</footer>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/privacy",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Public privacy policy (Play Store listing requirement)",
    tags=["Legal"],
)
async def privacy_page() -> HTMLResponse:
    """Public privacy policy. No auth, no side effects."""
    return HTMLResponse(
        content=_shell(f"{APP_DISPLAY_NAME} Privacy Policy", _privacy_body())
    )


@router.get(
    "/delete-account",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Public account-deletion instructions (Data safety requirement)",
    tags=["Legal"],
)
async def delete_account_page() -> HTMLResponse:
    """Public deletion-instructions page. No auth, no side effects."""
    return HTMLResponse(
        content=_shell(f"Delete your {APP_DISPLAY_NAME} account", _delete_body())
    )


@router.get(
    "/report-abuse",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Public abuse-reporting instructions (Play UGC policy requirement)",
    tags=["Legal"],
)
async def report_abuse_page() -> HTMLResponse:
    """Public abuse-reporting page. No auth, no side effects."""
    return HTMLResponse(
        content=_shell(f"Report abuse on {APP_DISPLAY_NAME}", _report_abuse_body())
    )
