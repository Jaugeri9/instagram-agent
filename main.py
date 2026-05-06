import os
import hmac
import hashlib
import json
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
APP_SECRET = os.getenv("META_APP_SECRET", "")
OWN_USER_ID = os.getenv("OWN_IG_USER_ID", "")
APP_ID = "4306924772888428"
CALLBACK_URL = "https://instagram-agent-production-4998.up.railway.app/callback"


@app.on_event("startup")
async def startup():
    init_db()


def valid_signature(body: bytes, signature_header: str) -> bool:
    if not APP_SECRET:
        return True
    expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not valid_signature(body, signature):
        raise HTTPException(status_code=403, detail="Bad signature")

    data = json.loads(body)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})

            if field == "comments":
                background_tasks.add_task(handle_comment, value)
            elif field == "follows":
                background_tasks.add_task(handle_follow, value)

    return {"status": "ok"}


async def handle_comment(value: dict):
    if get_setting("active") != "true":
        return
    if get_setting("auto_reply_comments") != "true":
        return

    user_id = value.get("from", {}).get("id", "")
    username = value.get("from", {}).get("username", "there")
    comment_text = value.get("text", "")
    comment_id = value.get("id", "")
    media_id = value.get("media", {}).get("id", "")

    # Temporarily disabled to allow testing with own account
    if False and user_id == OWN_USER_ID:
        return

    event_id = add_event("comment", user_id, username, comment_text, media_id, comment_id)

    try:
        post_caption = get_media_caption(media_id) if media_id else ""
        persona = get_setting("persona")
        reply = generate_comment_reply(comment_text, username, persona, post_caption)
        success = reply_to_comment(comment_id, reply)
        update_event(event_id, reply, "replied" if success else "error")
    except Exception as e:
        update_event(event_id, str(e), "error")


async def handle_follow(value: dict):
    if get_setting("active") != "true":
        return

    user_id = value.get("id", "")
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


# ── OAuth login flow ──

@app.get("/auth")
async def auth_start():
    scope = "instagram_basic,instagram_manage_comments,pages_show_list,pages_read_engagement,pages_manage_metadata"
    url = f"https://www.facebook.com/dialog/oauth?client_id={APP_ID}&redirect_uri={CALLBACK_URL}&scope={scope}&response_type=code"
    return RedirectResponse(url)


@app.get("/callback")
async def auth_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error or not code:
        return HTMLResponse(f"<h2>❌ Auth failed: {request.query_params.get('error_description', 'Unknown error')}</h2><p><a href='/auth'>Try again</a></p>")

    app_secret = os.getenv("META_APP_SECRET", "")
    page_id = os.getenv("PAGE_ID", "")

    # Step 1: Exchange code for short-lived token
    r1 = http_requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": APP_ID,
        "client_secret": app_secret,
        "redirect_uri": CALLBACK_URL,
        "code": code
    })
    d1 = r1.json()
    if "error" in d1:
        return HTMLResponse(f"<h2>❌ Step 1 failed:</h2><pre>{json.dumps(d1, indent=2)}</pre>")
    short_token = d1["access_token"]

    # Step 2: Exchange for long-lived token (60 days)
    r2 = http_requests.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": app_secret,
        "fb_exchange_token": short_token
    })
    d2 = r2.json()
    if "error" in d2:
        return HTMLResponse(f"<h2>❌ Step 2 failed:</h2><pre>{json.dumps(d2, indent=2)}</pre>")
    long_token = d2["access_token"]

    # Step 3: Try /me/accounts first, then direct page lookup
    page_token = None
    r3 = http_requests.get("https://graph.facebook.com/v19.0/me/accounts", params={"access_token": long_token})
    d3 = r3.json()
    for page in d3.get("data", []):
        if page.get("id") == page_id:
            page_token = page.get("access_token")
            break
    if not page_token and d3.get("data"):
        page_token = d3["data"][0]["access_token"]
        page_id = d3["data"][0]["id"]

    # Fallback: request page token directly
    if not page_token:
        r3b = http_requests.get(f"https://graph.facebook.com/v19.0/{page_id}", params={
            "fields": "access_token",
            "access_token": long_token
        })
        d3b = r3b.json()
        page_token = d3b.get("access_token")
        if not page_token:
            return HTMLResponse(f"""
            <h2>❌ No page token found.</h2>
            <p>Try <a href='/auth'>logging in again</a> — when Facebook asks which pages to connect,
            make sure to <strong>select your page</strong> before clicking Continue.</p>
            <pre>/me/accounts: {json.dumps(d3, indent=2)}\ndirect lookup: {json.dumps(d3b, indent=2)}</pre>
            """)

    # Step 4: Subscribe page to webhooks
    r4 = http_requests.post(f"https://graph.facebook.com/v19.0/{page_id}/subscribed_apps", params={
        "subscribed_fields": "instagram_manage_comments,instagram_mentions",
        "access_token": page_token
    })
    d4 = r4.json()

    # Step 5: Save token to database
    set_setting("access_token", page_token)

    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;max-width:700px;margin:40px auto;padding:20px">
    <h2>✅ All done! Your Instagram agent is fully connected.</h2>
    <p>Webhook subscription: <strong>{d4}</strong></p>
    <hr>
    <p>Also update <code>INSTAGRAM_ACCESS_TOKEN</code> in Railway with this permanent token so it survives restarts:</p>
    <textarea style="width:100%;height:80px;font-size:11px">{page_token}</textarea>
    <br><br>
    <a href="/" style="background:#7c3aed;color:white;padding:10px 20px;text-decoration:none;border-radius:6px">Go to Dashboard</a>
    </body></html>
    """)


# ── Test API call to unlock advanced access ──

@app.get("/test-comments", response_class=HTMLResponse)
async def test_comments():
    from instagram import _token
    token = _token()
    ig_user_id = os.getenv("OWN_IG_USER_ID", "")

    # Get media list
    r1 = http_requests.get(f"https://graph.facebook.com/v19.0/{ig_user_id}/media", params={
        "access_token": token
    })
    d1 = r1.json()
    if "error" in d1:
        return HTMLResponse(f"<h2>❌ Error getting media:</h2><pre>{json.dumps(d1, indent=2)}</pre>")

    media = d1.get("data", [])
    if not media:
        return HTMLResponse("<h2>❌ No media found on your account.</h2>")

    # Get comments on first post
    media_id = media[0]["id"]
    r2 = http_requests.get(f"https://graph.facebook.com/v19.0/{media_id}/comments", params={
        "access_token": token
    })
    d2 = r2.json()

    return HTMLResponse(f"""
    <h2>✅ Test API call successful!</h2>
    <p>Found {len(media)} posts. Comments on first post:</p>
    <pre>{json.dumps(d2, indent=2)}</pre>
    <p>Now go back to Meta and wait up to 24 hours for the 'Request advanced access' button to activate.</p>
    """)


# ── Dashboard ──

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    events = get_events(100)
    settings = get_settings_all()
    stats = get_stats()
    return templates.TemplateResponse(request, "index.html", {
        "events": events,
        "settings": settings,
        "stats": stats,
    })


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
    data = await request.json()
    allowed = {"auto_reply_comments", "auto_dm_followers", "persona", "active"}
    for key, value in data.items():
        if key in allowed:
            set_setting(key, str(value))
    return {"status": "ok"}
