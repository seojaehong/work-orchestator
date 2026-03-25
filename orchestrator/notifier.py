import os
import urllib.request
import urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8591177067:AAH0AmDjK0w7cSKQYt1ypwUO0ASsbZmAU1U")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8699916672")

def send(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message}).encode()
    try:
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}")

if __name__ == "__main__":
    send("🔔 notifier 테스트 — 오케스트레이터에서 보냄")
