import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def generate_comment_reply(comment_text: str, username: str, persona: str, post_caption: str = "") -> str:
    context = f'The post caption is: "{post_caption}"' if post_caption else "No post caption available."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=120,
        system=[
            {
                "type": "text",
                "text": persona,
                "cache_control": {"type": "ephemeral"}
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"{context}\n\n"
                    f"@{username} commented: \"{comment_text}\"\n\n"
                    "Write a short, natural reply (1-2 sentences). "
                    "Do not start with their username. Do not use hashtags."
                )
            }
        ]
    )
    return response.content[0].text.strip()


def generate_follower_dm(username: str, persona: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=120,
        system=[
            {
                "type": "text",
                "text": persona,
                "cache_control": {"type": "ephemeral"}
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"@{username} just followed this Instagram account. "
                    "Write a short, warm welcome DM (1-2 sentences). "
                    "Be genuine and friendly, not salesy or robotic."
                )
            }
        ]
    )
    return response.content[0].text.strip()
