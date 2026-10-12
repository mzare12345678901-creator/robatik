
import os
import re
import json
import time
import base64
import tempfile
import subprocess
from pathlib import Path

import requests
import imageio_ffmpeg


BALE_TOKEN = os.getenv("BALE_BOT_TOKEN", "986540587U_VZeXT1CLfQoF8SOBh0vNzOGQG6ywaA7Z8").strip()
AI_KEY = os.getenv("AIMLAPI_KEY", "bf751be0f71269ae1b94e02785a15708").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@viiideoai").strip().lstrip("@").lower()
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "").strip()
AI_BASE_URL = os.getenv(
    "AIMLAPI_BASE_URL", "https://api.aimlapi.com/v1"
).rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.5-flash")

BALE_API = "https://tapi.bale.ai/bot" + BALE_TOKEN

# اطلاعات ویدئوهای در انتظار تأیید؛ تا زمان اجرای این پردازش در حافظه است.
pending = {}
offset = 0
session = requests.Session()


def bale(method, data=None, files=None, timeout=60):
    """ارسال درخواست به API بله."""
    response = session.post(
        f"{BALE_API}/{method}",
        data=data or {},
        files=files,
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Bale API {method}: {result.get('description', 'Unknown error')}"
        )

    return result.get("result")


def send_text(chat_id, text):
    return bale(
        "sendMessage",
        {"chat_id": str(chat_id), "text": str(text)[:4000]},
    )


def get_chat_id(update):
    message = update.get("message") or update.get("channel_post") or {}
    return (message.get("chat") or {}).get("id")


def source_matches(message):
    chat = message.get("chat") or {}
    username = str(chat.get("username", "")).lower()
    return username == SOURCE_CHANNEL


def download_video(file_id, destination):
    info = bale("getFile", {"file_id": file_id})
    file_path = info.get("file_path")

    if not file_path:
        raise RuntimeError("Bale did not return file_path")

    url = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{file_path}"

    with session.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        with open(destination, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    output.write(chunk)


def extract_frames(video_path, folder):
    """استخراج حداکثر چهار تصویر از ویدئو."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    pattern = str(Path(folder) / "frame_%02d.jpg")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-i", video_path,
        "-vf", "fps=1/5,scale=640:-1",
        "-frames:v", "4",
        "-y", pattern,
    ]

    subprocess.run(
        command,
        check=True,
        timeout=180,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    frames = sorted(Path(folder).glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError("No video frames could be extracted")

    return frames


def make_caption(video_path, folder):
    frames = extract_frames(video_path, folder)

    content = [{
        "type": "text",
        "text": (
            "تصاویر این ویدئو را بررسی کن و یک کپشن فارسی جذاب و دقیق "
            "برای انتشار در کانال بله بنویس. محتوای دیده‌شده را توصیف کن؛ "
            "اطلاعاتی را که از تصاویر معلوم نیست، جعل نکن. "
            "کپشن کوتاه باشد و در صورت مناسب بودن چند هشتگ مرتبط داشته باشد. "
            "فقط متن کپشن را برگردان."
        ),
    }]

    for frame in frames:
        with open(frame, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")

        content.append({
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + encoded
            },
        })

    response = session.post(
        AI_BASE_URL + "/chat/completions",
        headers={
            "Authorization": "Bearer " + AI_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 350,
            "temperature": 0.5,
        },
        timeout=180,
    )
    response.raise_for_status()

    result = response.json()
    caption = result["choices"][0]["message"]["content"]

    if isinstance(caption, list):
        caption = " ".join(
            item.get("text", "")
            for item in caption
            if isinstance(item, dict)
        )

    caption = str(caption).strip()
    if not caption:
        raise RuntimeError("AI returned an empty caption")

    return caption[:900]


def send_video(chat_id, video_path, caption, reply_markup=None):
    data = {
        "chat_id": str(chat_id),
        "caption": caption[:1000],
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup, ensure_ascii=False
        )

    with open(video_path, "rb") as video_file:
        return bale(
            "sendVideo",
            data=data,
            files={
                "video": (
                    Path(video_path).name,
                    video_file,
                    "video/mp4",
                )
            },
            timeout=240,
        )


def process_source_video(message):
    if not ADMIN_CHAT_ID:
        print("ADMIN_CHAT_ID is not configured; video skipped.")
        return

    video = message.get("video")
    if not video:
        return

    file_id = video.get("file_id")
    if not file_id:
        return

    with tempfile.TemporaryDirectory(prefix="bale_ai_") as folder:
        video_path = str(Path(folder) / "source_video.mp4")

        download_video(file_id, video_path)
        caption = make_caption(video_path, folder)

        # ویدئو و کپشن ابتدا برای تأیید مدیر ارسال می‌شوند.
        review_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ تأیید و انتشار",
                        "callback_data": "approve",
                    },
                    {
                        "text": "❌ رد ویدئو",
                        "callback_data": "reject",
                    },
                ]
            ]
        }

        review = send_video(
            ADMIN_CHAT_ID,
            video_path,
            caption,
            review_markup,
        )

        review_message_id = review.get("message_id")
        if not review_message_id:
            raise RuntimeError("Review message ID was not returned")

        # فایل موقت پس از پایان این تابع حذف می‌شود؛
        # برای انتشار بعد از تأیید، باید دوباره دریافتش کنیم.
        pending[str(review_message_id)] = {
            "file_id": file_id,
            "caption": caption,
        }


def handle_callback(update):
    query = update.get("callback_query") or {}
    data = query.get("data")
    query_id = query.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    message_id = message.get("message_id")

    if not query_id:
        return

    if not ADMIN_CHAT_ID or str(chat.get("id")) != ADMIN_CHAT_ID:
        bale("answerCallbackQuery", {
            "callback_query_id": query_id,
            "text": "اجازه انجام این کار را نداری.",
        })
        return

    item = pending.get(str(message_id))
    if not item:
        bale("answerCallbackQuery", {
            "callback_query_id": query_id,
            "text": "این درخواست در حافظه ربات موجود نیست.",
        })
        return

    if data == "reject":
        pending.pop(str(message_id), None)
        bale("answerCallbackQuery", {
            "callback_query_id": query_id,
            "text": "ویدئو رد شد.",
        })
        return

    if data != "approve":
        return

    if not DEST_CHANNEL:
        raise RuntimeError("DEST_CHANNEL is not configured")

    with tempfile.TemporaryDirectory(prefix="bale_publish_") as folder:
        video_path = str(Path(folder) / "approved_video.mp4")
        download_video(item["file_id"], video_path)
        send_video(DEST_CHANNEL, video_path, item["caption"])

    pending.pop(str(message_id), None)

    bale("answerCallbackQuery", {
        "callback_query_id": query_id,
        "text": "ویدئو منتشر شد.",
    })


def handle_update(update):
    callback = update.get("callback_query")
    if callback:
        handle_callback(update)
        return

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text", "")).strip()

    # فرمان‌های خصوصی برای راه‌اندازی اولیه و دریافت شناسه
    if chat.get("type") == "private" and text in ("/start", "/id"):
        send_text(
            chat["id"],
            "ربات فعال است.\n"
            f"شناسه گفت‌وگوی شما: {chat['id']}\n"
            "برای تأیید و انتشار ویدئو، ADMIN_CHAT_ID را در Render "
            "با همین شناسه تنظیم کن.",
        )
        return

    # فقط پست‌های کانال مبدأ پردازش می‌شوند.
    channel_post = update.get("channel_post") or {}
    if channel_post and source_matches(channel_post):
        if channel_post.get("video"):
            process_source_video(channel_post)


def main():
    global offset

    if not BALE_TOKEN:
        raise RuntimeError("BALE_BOT_TOKEN is missing")
    if not AI_KEY:
        raise RuntimeError("AIMLAPI_KEY is missing")

    print("Bale AI video worker is starting...")

    while True:
        try:
            updates = bale(
                "getUpdates",
                data={
                    "offset": str(offset),
                    "timeout": "25",
                    "allowed_updates": json.dumps([
                        "message", "channel_post", "callback_query"
                    ]),
                },
                timeout=40,
            )

            for update in updates or []:
                offset = max(offset, int(update["update_id"]) + 1)

                try:
                    handle_update(update)
                except Exception as exc:
                    print(
                        f"Update {update.get('update_id')} failed: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(
                f"Polling error: {type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
