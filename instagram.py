import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://graph.facebook.com/v19.0"


def _token() -> str:
    return os.getenv("INSTAGRAM_ACCESS_TOKEN", "")


def reply_to_comment(comment_id: str, message: str) -> bool:
    url = f"{BASE_URL}/{comment_id}/replies"
    resp = requests.post(url, data={"message": message, "access_token": _token()}, timeout=10)
    return resp.status_code == 200


def send_dm(user_id: str, message: str) -> bool:
    """Send an Instagram Direct message. Requires instagram_manage_messages permission."""
    page_id = os.getenv("PAGE_ID", "")
    url = f"{BASE_URL}/{page_id}/messages"
    resp = requests.post(url, json={
        "recipient": {"id": user_id},
        "message": {"text": message},
        "access_token": _token()
    }, timeout=10)
    return resp.status_code == 200


def get_media_caption(media_id: str) -> str:
    url = f"{BASE_URL}/{media_id}"
    resp = requests.get(url, params={
        "fields": "caption,media_type",
        "access_token": _token()
    }, timeout=10)
    if resp.status_code == 200:
        return resp.json().get("caption", "")
    return ""


def get_ig_user_id() -> str:
    """Returns the Instagram Business Account ID linked to the page."""
    page_id = os.getenv("PAGE_ID", "")
    url = f"{BASE_URL}/{page_id}"
    resp = requests.get(url, params={
        "fields": "instagram_business_account",
        "access_token": _token()
    }, timeout=10)
    if resp.status_code == 200:
        return resp.json().get("instagram_business_account", {}).get("id", "")
    return ""
