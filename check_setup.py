import os
import sys

import requests
from google import genai

failed = False


def check_telegram():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    base_url = f"https://api.telegram.org/bot{token}"

    def call(method, data=None):
        try:
            response = requests.post(
                f"{base_url}/{method}",
                json=data or {},
                timeout=30,
            )
            result = response.json()
        except Exception:
            raise RuntimeError(
                "Telegram connection failed. Check the token and try again."
            ) from None

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram rejected {method}. "
                "Check the bot token and chat ID."
            )
        return result["result"]

    bot = call("getMe")
    call("getChat", {"chat_id": chat_id})
    print(f"Telegram OK: @{bot['username']}; destination chat found.")


def check_gemini():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

    try:
        response = client.models.generate_content(
            model=model,
            contents="Reply with only the word OK.",
        )
        if not response.text:
            raise RuntimeError("Empty response")
    except Exception as error:
        code = getattr(error, "code", None)
        raise RuntimeError(
            f"Gemini test failed (status: {code or 'unknown'}). "
            "Possible causes: API key, model access, region, or free-tier quota."
        ) from None

    print("Gemini OK: generated a test response.")


for name, check in [
    ("Telegram", check_telegram),
    ("Gemini", check_gemini),
]:
    try:
        check()
    except Exception as error:
        failed = True
        print(f"{name}: {error}")

if failed:
    sys.exit(1)

print("Both connection checks passed. No Telegram message was sent.")
