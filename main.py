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
APP_SECRET   = os.getenv("META_APP_SECRET", "")
OWN_USER_ID  = os.getenv("OWN_IG_USER_ID", "17841445556387920")
PAGE_ID      = os.getenv("PAGE_ID", "113420497129209")
IG_USER_ID   = os.getenv("OWN_IG_USER_ID", "17841445556387920")
APP_ID       = "4306924772888428"
CALLBACK_URL = "https://instagram-agent-production-4998.up.railway.app/callback"
BASE_GQL     = "https://graph.facebook.com/v19.0"


def get_token() -> str:
    db_token = get_setting("access_token")
    if db_token:
        return db_token
    return os.getenv("INSTAGRAM_ACCESS_TOKEN", "")


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
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "invalid json"}
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
        return HTMLResponse(f"<h2>Auth error: {error}</h2><p><a href='/auth'>Try again</a></p>")
    if not code:
        return HTMLResponse("<h2>No code received</h2><p><a href='/auth'>Try again</a></p>")
    app_secret = os.getenv("META_APP_SECRET", "")
    r1 = http_requests.get(f"{BASE_GQL}/oauth/access_token", params={
        "client_id": APP_ID, "client_secret": app_secret,
        "redirect_uri": CALLBACK_URL, "code": code,
    })
    d1 = r1.json()
    if "error" in d1:
        return HTMLResponse(f"<h2>Code exchange failed:</h2><pre>{json.dumps(d1, indent=2)}</pre>")
    short_token = d1.get("access_token", "")
    r2 = http_requests.get(f"{BASE_GQL}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": APP_ID,
        "client_secret": app_secret, "fb_exchange_token": short_token,
    })
    d2 = r2.json()
    if "error" in d2:
        return HTMLResponse(f"<h2>Long token exchange failed:</h2><pre>{json.dumps(d2, indent=2)}</pre>")
    long_user_token = d2.get("access_token", "")
    set_setting("long_token", long_user_token)
    r3 = http_requests.get(f"{BASE_GQL}/{PAGE_ID}", params={
        "fields": "access_token,name", "access_token": long_user_token,
    })
    d3 = r3.json()
    page_token = d3.get("access_token", "")
    page_name  = d3.get("name", "Unknown")
    if not page_token:
        r3b = http_requests.get(f"{BASE_GQL}/me/accounts", params={"access_token": long_user_token})
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
            f"<h2>Could not get page token</h2>"
            f"<p>Long user token saved. Try <a href='/subscribe-ig'>/subscribe-ig</a>.</p>"
            f"<p><pre>{json.dumps(d3, indent=2)}</pre></p>"
        )
    set_setting("access_token", page_token)
    r4a = http_requests.post(
        f"{BASE_GQL}/{IG_USER_ID}/subscribed_apps",
        params={"subscribed_fields": "comments,mentions", "access_token": long_user_token}
    )
    d4a = r4a.json()
    ig_sub_ok = d4a.get("success", False)
    r4b = http_requests.post(
        f"{BASE_GQL}/{PAGE_ID}/subscribed_apps",
        params={"subscribed_fields": "mention,feed", "access_token": page_token}
    )
    d4b = r4b.json()
    page_sub_ok = d4b.get("success", False)
    return HTMLResponse(f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:40px auto;padding:20px">
    <h2>Authentication Complete</h2>
    <p><b>Page:</b> {page_name}</p>
    <p><b>Token saved:</b> ...{page_token[-10:]}</p>
    <hr>
    <p><b>IG User subscription:</b> {"SUCCESS - comments + mentions" if ig_sub_ok else "ERROR: " + json.dumps(d4a)}</p>
    <p><b>Page subscription:</b> {"SUCCESS" if page_sub_ok else "ERROR: " + json.dumps(d4b)}</p>
    <hr>
    <p style='color:green'><b>Token saved. Go to /debug to check full status.</b></p>
    <p><a href="/debug">Check debug status</a> | <a href="/">Dashboard</a></p>
    </body></html>
    """)


@app.get("/subscribe-ig")
async def subscribe_ig():
    token = get_token()
    if not token:
        return HTMLResponse("<h2>No token. Please visit <a href='/auth'>/auth</a> first.</h2>")
    long_tok = get_setting("long_token") or ""
    token_for_ig = long_tok if long_tok else token
    r1 = http_requests.post(
        f"{BASE_GQL}/{IG_USER_ID}/subscribed_apps",
        params={"subscribed_fields": "comments,mentions", "access_token": token_for_ig}
    )
    d1 = r1.json()
    ig_ok = d1.get("success", False)
    r2 = http_requests.post(
        f"{BASE_GQL}/{PAGE_ID}/subscribed_apps",
        params={"subscribed_fields": "mention,feed", "access_token": token}
    )
    d2 = r2.json()
    page_ok = d2.get("success", False)
    return HTMLResponse(f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px">
    <h2>Subscription Results</h2>
    <p><b>IG User ({IG_USER_ID}):</b> {"SUCCESS" if ig_ok else "ERROR: " + json.dumps(d1)}</p>
    <p><b>Page ({PAGE_ID}):</b> {"SUCCESS" if page_ok else "ERROR: " + json.dumps(d2)}</p>
    <p><a href="/debug">Debug status</a> | <a href="/">Dashboard</a></p>
    </body></html>
    """)


@app.get("/debug")
async def debug():
    token     = get_token()
    db_token  = get_setting("access_token") or ""
    env_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    me        = http_requests.get(f"{BASE_GQL}/me", params={"fields": "id,name", "access_token": token}).json()
    ig_sub    = http_requests.get(f"{BASE_GQL}/{IG_USER_ID}/subscribed_apps", params={"access_token": token}).json()
    page_sub  = http_requests.get(f"{BASE_GQL}/{PAGE_ID}/subscribed_apps", params={"access_token": token}).json()
    tok_debug = http_requests.get(f"{BASE_GQL}/debug_token", params={
        "input_token": token,
        "access_token": f"{APP_ID}|{os.getenv('META_APP_SECRET', '')}",
    }).json()
    tok_preview = f"...{token[-15:]}" if token else "MISSING"
    ig_fields = []
    for item in ig_sub.get("data", []):
        ig_fields.extend(item.get("subscribed_fields", []))
    comments_ok = "comments" in ig_fields
    return HTMLResponse(f"""
    <html><body style="font-family:monospace;padding:20px;max-width:900px;margin:auto">
    <h2>Debug Status</h2>
    <h3>Token</h3>
    <p>Using: <b>{tok_preview}</b></p>
    <p>DB token: {"SET ..."+db_token[-8:] if db_token else "NOT SET"}</p>
    <p>ENV token: {"SET ..."+env_token[-8:] if env_token else "not set"}</p>
    <h3>/me</h3>
    <pre>{json.dumps(me, indent=2)}</pre>
    <h3>Instagram User Subscription (IG ID: {IG_USER_ID})</h3>
    <p><b>Comments active: {"YES" if comments_ok else "NO"}</b></p>
    <pre>{json.dumps(ig_sub, indent=2)}</pre>
    <h3>Page Subscription (Page ID: {PAGE_ID})</h3>
    <pre>{json.dumps(page_sub, indent=2)}</pre>
    <h3>Token Scopes</h3>
    <pre>{json.dumps(tok_debug, indent=2)}</pre>
    <hr>
    <p><a href="/auth">Re-authenticate</a> | <a href="/subscribe-ig">Subscribe IG</a> | <a href="/">Dashboard</a></p>
    </body></html>
    """)


@app.get("/privacy")
async def privacy():
    return HTMLResponse("""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px">
    <h1>Privacy Policy</h1>
    <p>This app automates Instagram comment replies using AI on behalf of the account owner.</p>
    <h2>Data Collected</h2>
    <ul><li>Instagram usernames and user IDs of commenters</li>
    <li>Comment text</li><li>Post IDs and captions</li></ul>
    <h2>How Data Is Used</h2>
    <p>Data is used only to generate replies. Never sold or shared except with
    Anthropic (AI) and Meta (sending replies).</p>
    <h2>Contact</h2><p>Contact the account owner via Instagram.</p>
    </body></html>
    """)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    events   = get_events(100)
    settings = get_settings_all()
    stats    = get_stats()
    return templates.TemplateResponse(request, "index.html", {
        "events": events, "settings": settings, "stats": stats,
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
    data    = await request.json()
    allowed = {"auto_reply_comments", "auto_dm_followers", "persona", "active"}
    for key, value in data.items():
        if key in allowed:
            set_setting(key, str(value))
    return {"status": "ok"}
