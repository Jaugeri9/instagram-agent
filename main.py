import os
import hmac
import hashlib
import json
import requests as http_requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse
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

    if user_id == OWN_USER_ID:
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


# ── One-time setup: subscribe page to webhooks ──

@app.get("/setup", response_class=HTMLResponse)
async def setup_subscription():
    page_id = os.getenv("PAGE_ID", "")
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    if not page_id or not token:
        return HTMLResponse("<h2>Error: PAGE_ID or INSTAGRAM_ACCESS_TOKEN not set in environment variables.</h2>")
    url = f"https://graph.facebook.com/v19.0/{page_id}/subscribed_apps"
    params = {
        "subscribed_fields": "instagram_manage_comments,instagram_mentions",
        "access_token": token
    }
    resp = http_requests.post(url, params=params)
    data = resp.json()
    if data.get("success"):
        return HTMLResponse("<h2>✅ Success! Your page is now subscribed to webhook events. Comments on your Instagram posts will now trigger the agent.</h2>")
    else:
        return HTMLResponse(f"<h2>❌ Error:</h2><pre>{json.dumps(data, indent=2)}</pre>")


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
