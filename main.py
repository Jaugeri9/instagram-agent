import os
import hmac
import hashlib
import json
import datetime
import requests as http_requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from database import init_db, get_setting, set_setting, add_event, update_event, get_events, get_settings_all, get_stats
from instagram import reply_to_comment, send_dm, get_media_caption
from ai import generate_comment_reply, generate_follower_dm

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
APP_SECRET   = os.getenv("META_APP_SECRET", "")
OWN_USER_ID  = os.getenv("OWN_IG_USER_ID", "17841445556387920")
PAGE_ID      = os.getenv("PAGE_ID", "113420497129209")
IG_USER_ID   = os.getenv("OWN_IG_USER_ID", "17841445556387920")
APP_ID       = "4306924772888428"
CALLBACK_URL = "https://instagram-agent-production-4998.up.railway.app/callback"
BASE_GQL     = "https://graph.facebook.com/v19.0"


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_token() -> str:
    db_token = get_setting("access_token")
    if db_token:
        return db_token
    return os.getenv("INSTAGRAM_ACCESS_TOKEN", "")


def _try_ig_subscribe(ig_user_id: str, token: str, label: str) -> dict:
    """Attempt to subscribe IG user to app webhooks using a given token."""
    try:
        r = http_requests.post(
            f"{BASE_GQL}/{ig_user_id}/subscribed_apps",
            params={"subscribed_fields": "comments,mentions", "access_token": token},
            timeout=10,
        )
        d = r.json()
    except Exception as e:
        d = {"exception": str(e)}
    return {"ok": d.get("success", False), "label": label, "response": d}


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()


# ── Webhook signature check ────────────────────────────────────────────────────

def valid_signature(body: bytes, signature_header: str) -> bool:
    if not APP_SECRET:
        return True
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


# ── Webhook verification ───────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Webhook event receiver ─────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not valid_signature(body, signature):
        raise HTTPException(status_code=403, detail="Bad signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "invalid json"}

    # Log raw webhook for debugging — visible in /debug
    try:
        set_setting("last_webhook_raw", body.decode("utf-8", errors="replace")[:2000])
        set_setting("last_webhook_time", datetime.datetime.utcnow().isoformat() + "Z")
    except Exception:
        pass

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})

            if field == "comments":
                background_tasks.add_task(handle_comment, value)
            elif field == "mentions":
                background_tasks.add_task(handle_mention, value)
            elif field == "follows":
                background_tasks.add_task(handle_follow, value)

    return {"status": "ok"}


# ── Event handlers ─────────────────────────────────────────────────────────────

async def handle_comment(value: dict):
    if get_setting("active") != "true":
        return
    if get_setting("auto_reply_comments") != "true":
        return

    user_id      = value.get("from", {}).get("id", "")
    username     = value.get("from", {}).get("username", "there")
    comment_text = value.get("text", "")
    comment_id   = value.get("id", "")
    media_id     = value.get("media", {}).get("id", "")

    if user_id == OWN_USER_ID:
        return

    event_id = add_event("comment", user_id, username, comment_text, media_id, comment_id)

    try:
        post_caption = get_media_caption(media_id) if media_id else ""
        persona = get_setting("persona")
        reply   = generate_comment_reply(comment_text, username, persona, post_caption)
        success = reply_to_comment(comment_id, reply)
        update_event(event_id, reply, "replied" if success else "error")
    except Exception as e:
        update_event(event_id, str(e), "error")


async def handle_mention(value: dict):
    if get_setting("active") != "true":
        return
    if get_setting("auto_reply_comments") != "true":
        return

    user_id      = value.get("from", {}).get("id", "")
    username     = value.get("from", {}).get("username", "there")
    comment_text = value.get("text", "")
    comment_id   = value.get("id", "")
    media_id     = value.get("media_id", "")

    if user_id == OWN_USER_ID:
        return

    event_id = add_event("mention", user_id, username, comment_text, media_id, comment_id)

    try:
        post_caption = get_media_caption(media_id) if media_id else ""
        persona = get_setting("persona")
        reply   = generate_comment_reply(comment_text, username, persona, post_caption)
        if comment_id:
            success = reply_to_comment(comment_id, reply)
            update_event(event_id, reply, "replied" if success else "error")
        else:
            update_event(event_id, reply, "tracked")
    except Exception as e:
        update_event(event_id, str(e), "error")


async def handle_follow(value: dict):
    if get_setting("active") != "true":
        return

    user_id  = value.get("id", "")
    username = value.get("username", "")

    event_id = add_event("follow", user_id, username)

    if get_setting("auto_dm_followers") == "true":
        try:
            persona = get_setting("persona")
            message = generate_follower_dm(username, persona)
            success = send_dm(user_id, message)
            update_event(event_id, message, "replied" if success else "error")
        except Exception as e:
            update_event(event_id, str(e), "error")
    else:
        update_event(event_id, None, "tracked")


# ── OAuth flow ─────────────────────────────────────────────────────────────────

@app.get("/auth")
async def auth_start():
    scope = (
        "instagram_basic,"
        "instagram_manage_comments,"
        "instagram_manage_messages,"
        "pages_show_list,"
        "pages_read_engagement,"
        "pages_manage_metadata"
    )
    url = (
        f"https://www.facebook.com/dialog/oauth"
        f"?client_id={APP_ID}"
        f"&redirect_uri={CALLBACK_URL}"
        f"&scope={scope}"
        f"&response_type=code"
    )
    return RedirectResponse(url)


@app.get("/callback")
async def auth_callback(request: Request):
    code  = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(f"<h2>❌ Auth error: {error}</h2><p><a href='/auth'>Try again</a></p>")
    if not code:
        return HTMLResponse("<h2>❌ No code received</h2><p><a href='/auth'>Try again</a></p>")

    app_secret = os.getenv("META_APP_SECRET", "")

    # Step 1: code → short-lived user token
    r1 = http_requests.get(f"{BASE_GQL}/oauth/access_token", params={
        "client_id":     APP_ID,
        "client_secret": app_secret,
        "redirect_uri":  CALLBACK_URL,
        "code":          code,
    })
    d1 = r1.json()
    if "error" in d1:
        return HTMLResponse(f"<h2>❌ Code exchange failed:</h2><pre>{json.dumps(d1, indent=2)}</pre>")
    short_token = d1.get("access_token", "")

    # Step 2: short-lived → long-lived user token (60 days)
    r2 = http_requests.get(f"{BASE_GQL}/oauth/access_token", params={
        "grant_type":        "fb_exchange_token",
        "client_id":         APP_ID,
        "client_secret":     app_secret,
        "fb_exchange_token": short_token,
    })
    d2 = r2.json()
    if "error" in d2:
        return HTMLResponse(f"<h2>❌ Long token exchange failed:</h2><pre>{json.dumps(d2, indent=2)}</pre>")
    long_user_token = d2.get("access_token", "")
    set_setting("long_token", long_user_token)

    # Step 3: get permanent page access token
    r3 = http_requests.get(f"{BASE_GQL}/{PAGE_ID}", params={
        "fields":       "access_token,name",
        "access_token": long_user_token,
    })
    d3 = r3.json()
    page_token = d3.get("access_token", "")
    page_name  = d3.get("name", "Unknown")

    if not page_token:
        r3b      = http_requests.get(f"{BASE_GQL}/me/accounts", params={"access_token": long_user_token})
        accounts = r3b.json().get("data", [])
        for acct in accounts:
            if acct.get("id") == PAGE_ID:
                page_token = acct.get("access_token", "")
                page_name  = acct.get("name", "Unknown")
                break
        if not page_token and accounts:
            page_token = accounts[0].get("access_token", "")
            page_name  = accounts[0].get("name", "Unknown")

    if not page_token:
        return HTMLResponse(
            f"<h2>⚠️ Could not get page token</h2>"
            f"<pre>{json.dumps(d3, indent=2)}</pre>"
            f"<p><a href='/auth'>Try again</a></p>"
        )

    set_setting("access_token", page_token)

    # Step 4: Subscribe IG USER — try 3 token types
    app_token   = f"{APP_ID}|{app_secret}"
    ig_attempts = [
        _try_ig_subscribe(IG_USER_ID, app_token,       "App Token (APP_ID|SECRET)"),
        _try_ig_subscribe(IG_USER_ID, long_user_token, "Long User Token"),
        _try_ig_subscribe(IG_USER_ID, page_token,      "Page Token"),
    ]
    ig_sub_ok = any(a["ok"] for a in ig_attempts)
    ig_winner = next((a["label"] for a in ig_attempts if a["ok"]), None)

    # Step 5: Page subscription (valid fields only: mention, feed)
    r5 = http_requests.post(
        f"{BASE_GQL}/{PAGE_ID}/subscribed_apps",
        params={"subscribed_fields": "mention,feed", "access_token": page_token},
        timeout=10,
    )
    d5 = r5.json()
    page_sub_ok = d5.get("success", False)

    overall_ok = ig_sub_ok or page_sub_ok

    ig_rows = "".join(
        f"<tr><td>{a['label']}</td>"
        f"<td style='color:{'green' if a['ok'] else 'red'}'>{'✅ SUCCESS' if a['ok'] else '❌ FAILED'}</td>"
        f"<td><code style='font-size:11px'>{json.dumps(a['response'])}</code></td></tr>"
        for a in ig_attempts
    )

    return HTMLResponse(f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 850px; margin: 40px auto; padding: 20px;">
    <h2>Authentication Complete</h2>
    <p><b>Page:</b> {page_name}</p>
    <p><b>Token saved:</b> ...{page_token[-10:]}</p>
    <hr>
    <h3>IG User Subscription ({IG_USER_ID})</h3>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;word-break:break-all">
    <tr style="background:#eee"><th>Token Used</th><th>Result</th><th>Response</th></tr>
    {ig_rows}
    </table>
    <p>{("✅ <b>IG subscription succeeded using: " + ig_winner + "</b>") if ig_sub_ok else "❌ All IG subscription attempts failed (see errors above)"}</p>
    <hr>
    <p><b>Page subscription ({PAGE_ID}):</b> {"✅ mention + feed subscribed" if page_sub_ok else "❌ ERROR: " + json.dumps(d5)}</p>
    <hr>
    {"<p style='color:green;font-size:18px'><b>🎉 At least one subscription succeeded — comments should now trigger the agent.</b></p>" if overall_ok else "<p style='color:orange'><b>⚠️ Subscriptions failed. Check /debug → Token Scopes to see if instagram_manage_comments was granted.</b></p>"}
    <p><a href="/debug">→ Check /debug</a> &nbsp;|&nbsp; <a href="/">Dashboard</a></p>
    </body></html>
    """)


# ── Subscribe Instagram user to comment webhooks ───────────────────────────────

@app.get("/subscribe-ig")
async def subscribe_ig():
    token      = get_token()
    long_token = get_setting("long_token") or ""
    app_secret = os.getenv("META_APP_SECRET", "")
    app_token  = f"{APP_ID}|{app_secret}"

    if not token and not long_token:
        return HTMLResponse(
            "<h2>❌ No token found.</h2>"
            "<p>Please visit <a href='/auth'>/auth</a> first to authenticate.</p>"
        )

    ig_attempts = [
        _try_ig_subscribe(IG_USER_ID, app_token,              "App Token (APP_ID|SECRET)"),
        _try_ig_subscribe(IG_USER_ID, long_token or token,    "Long User Token"),
        _try_ig_subscribe(IG_USER_ID, token,                  "Page Token"),
    ]
    ig_sub_ok = any(a["ok"] for a in ig_attempts)

    r2 = http_requests.post(
        f"{BASE_GQL}/{PAGE_ID}/subscribed_apps",
        params={"subscribed_fields": "mention,feed", "access_token": token},
        timeout=10,
    )
    d2 = r2.json()
    page_ok = d2.get("success", False)

    ig_rows = "".join(
        f"<tr><td>{a['label']}</td>"
        f"<td style='color:{'green' if a['ok'] else 'red'}'>{'✅' if a['ok'] else '❌'}</td>"
        f"<td><code style='font-size:11px'>{json.dumps(a['response'])}</code></td></tr>"
        for a in ig_attempts
    )

    return HTMLResponse(f"""
    <html><body style="font-family:Arial,sans-serif;max-width:750px;margin:40px auto;padding:20px">
    <h2>Subscription Results</h2>
    <h3>IG User ({IG_USER_ID})</h3>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;word-break:break-all">
    <tr style="background:#eee"><th>Token Used</th><th>Result</th><th>Response</th></tr>
    {ig_rows}
    </table>
    <p><b>Page ({PAGE_ID}):</b> {"✅ mention + feed" if page_ok else "❌ ERROR: " + json.dumps(d2)}</p>
    <hr>
    {"<p style='color:green'><b>✅ At least one subscription succeeded.</b></p>" if (ig_sub_ok or page_ok) else "<p style='color:red'><b>❌ All subscriptions failed. Visit /debug to check token scopes.</b></p>"}
    <p><a href="/debug">Debug status</a> | <a href="/">Dashboard</a></p>
    </body></html>
    """)


# ── Debug endpoint ─────────────────────────────────────────────────────────────

@app.get("/debug")
async def debug():
    token      = get_token()
    db_token   = get_setting("access_token") or ""
    env_token  = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    long_token = get_setting("long_token") or ""
    app_secret = os.getenv("META_APP_SECRET", "")

    me = http_requests.get(f"{BASE_GQL}/me", params={
        "fields": "id,name", "access_token": token
    }).json() if token else {"error": "no token"}

    ig_sub = http_requests.get(
        f"{BASE_GQL}/{IG_USER_ID}/subscribed_apps",
        params={"access_token": token}
    ).json() if token else {}

    page_sub = http_requests.get(
        f"{BASE_GQL}/{PAGE_ID}/subscribed_apps",
        params={"access_token": token}
    ).json() if token else {}

    tok_debug = http_requests.get(
        f"{BASE_GQL}/debug_token",
        params={
            "input_token":  token,
            "access_token": f"{APP_ID}|{app_secret}",
        }
    ).json() if token else {}

    long_tok_debug = http_requests.get(
        f"{BASE_GQL}/debug_token",
        params={
            "input_token":  long_token,
            "access_token": f"{APP_ID}|{app_secret}",
        }
    ).json() if long_token else {"note": "no long token saved"}

    tok_preview = f"...{token[-15:]}" if token else "MISSING"

    ig_sub_data       = ig_sub.get("data", [])
    subscribed_fields = []
    for item in ig_sub_data:
        subscribed_fields.extend(item.get("subscribed_fields", []))
    comments_active = "comments" in subscribed_fields

    scopes          = tok_debug.get("data", {}).get("scopes", [])
    has_ig_comments = "instagram_manage_comments" in scopes

    long_scopes          = long_tok_debug.get("data", {}).get("scopes", [])
    long_has_ig_comments = "instagram_manage_comments" in long_scopes

    last_webhook      = get_setting("last_webhook_raw") or "None received yet"
    last_webhook_time = get_setting("last_webhook_time") or "Never"

    return HTMLResponse(f"""
    <html><body style="font-family: monospace; padding: 20px; max-width: 1000px; margin: auto;">
    <h2>🔍 Debug Status</h2>

    <h3>Tokens</h3>
    <p>Active token: <strong>{tok_preview}</strong></p>
    <p>DB token: {'✅ set (...' + db_token[-10:] + ')' if db_token else '❌ NOT SET'}</p>
    <p>ENV token: {'✅ set (...' + env_token[-10:] + ')' if env_token else '⚠️ not set (OK if DB set)'}</p>
    <p>Long user token: {'✅ saved (...' + long_token[-10:] + ')' if long_token else '❌ NOT SAVED — go to /auth to re-authenticate'}</p>

    <h3>🔑 Page Token Scopes</h3>
    <p style="font-size:16px">instagram_manage_comments: <strong style="color:{'green' if has_ig_comments else 'red'}">{'✅ YES — webhook permission granted' if has_ig_comments else '❌ NO — must re-authenticate at /auth'}</strong></p>
    <p>All scopes: {', '.join(scopes) if scopes else '(none found — check token)'}</p>
    <details><summary>Full page token debug_token response</summary><pre>{json.dumps(tok_debug, indent=2)}</pre></details>

    <h3>🔑 Long User Token Scopes</h3>
    <p>instagram_manage_comments: <strong style="color:{'green' if long_has_ig_comments else 'red'}">{'✅ YES' if long_has_ig_comments else '❌ NO'}</strong></p>
    <p>All scopes: {', '.join(long_scopes) if long_scopes else '(none)'}</p>
    <details><summary>Full long user token debug_token response</summary><pre>{json.dumps(long_tok_debug, indent=2)}</pre></details>

    <h3>/me Response</h3>
    <pre>{json.dumps(me, indent=2)}</pre>

    <h3>Instagram User Subscription (IG ID: {IG_USER_ID})</h3>
    <p>Comments field active: <strong style="color:{'green' if comments_active else 'red'}">{'✅ YES' if comments_active else '❌ NO — run /subscribe-ig'}</strong></p>
    <pre>{json.dumps(ig_sub, indent=2)}</pre>

    <h3>Page Subscription (Page ID: {PAGE_ID})</h3>
    <pre>{json.dumps(page_sub, indent=2)}</pre>

    <h3>📨 Last Webhook Received from Meta</h3>
    <p>Time: <strong>{last_webhook_time}</strong></p>
    <pre style="background:#f5f5f5;padding:12px;overflow-x:auto;border:1px solid #ccc">{last_webhook[:1500]}</pre>

    <hr>
    <p>
      <a href='/auth'>🔐 Re-authenticate</a> &nbsp;|&nbsp;
      <a href='/subscribe-ig'>📡 Subscribe IG</a> &nbsp;|&nbsp;
      <a href='/'>📊 Dashboard</a>
    </p>
    </body></html>
    """)


# ── Privacy Policy ─────────────────────────────────────────────────────────────

@app.get("/privacy")
async def privacy():
    return HTMLResponse("""
    <html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px;">
    <h1>Privacy Policy</h1>
    <p><strong>Last updated: 2024</strong></p>
    <p>This application ("Instagram Agent") automates responses to Instagram comments
    and messages on behalf of the account owner using AI-generated text.</p>

    <h2>Data We Collect</h2>
    <ul>
      <li>Instagram usernames and user IDs of people who comment on your posts</li>
      <li>Comment and mention text content</li>
      <li>Instagram post IDs and captions</li>
    </ul>

    <h2>How We Use Data</h2>
    <p>Data is used solely to generate and send automated replies on behalf of the
    account owner. Data is stored locally and never sold or shared with third parties,
    except as required to operate the service (Anthropic for AI generation, Meta for
    sending replies).</p>

    <h2>Data Retention</h2>
    <p>Event data is stored in a local database for operational and review purposes.
    You can delete data at any time by contacting the account owner.</p>

    <h2>User Rights</h2>
    <p>Users may request deletion of their data by contacting the account owner
    directly via Instagram.</p>

    <h2>Contact</h2>
    <p>For privacy concerns, please contact the Instagram account owner directly.</p>
    </body></html>
    """)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    events   = get_events(100)
    settings = get_settings_all()
    stats    = get_stats()
    return templates.TemplateResponse(request, "index.html", {
        "events":   events,
        "settings": settings,
        "stats":    stats,
    })


# ── API endpoints for dashboard ────────────────────────────────────────────────

@app.get("/api/events")
async def api_events():
    return get_events(100)


@app.get("/api/stats")
async def api_stats():
    return get_stats()


@app.get("/api/settings")
async def api_get_settings():
    return get_settings_all()


@app.post("/api/settings")
async def api_update_settings(request: Request):
    data    = await request.json()
    allowed = {"auto_reply_comments", "auto_dm_followers", "persona", "active"}
    for key, value in data.items():
        if key in allowed:
            set_setting(key, str(value))
    return {"status": "ok"}
