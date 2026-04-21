from __future__ import annotations

import json
import os
import time
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from flask import Flask, Response, request

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = "/tmp/telegram-bot-data" if os.environ.get("VERCEL") else str(BASE_DIR)
DATA_DIR = Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_TEMPLATE: dict[str, Any] = {
    "bot_token": "",
    "developer_userid": "",
    "demo_channel_link": "",
    "proofs_channel_link": "",
    "target_channel_id": "",
}

DEFAULT_ADMIN_ID = 6627762162


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_config_file() -> Path:
    config_path = BASE_DIR / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(CONFIG_TEMPLATE, indent=2), encoding="utf-8")
    return config_path


def read_config_json() -> dict[str, Any]:
    config_path = ensure_config_file()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config_path.write_text(json.dumps(CONFIG_TEMPLATE, indent=2), encoding="utf-8")
        return dict(CONFIG_TEMPLATE)
    if not isinstance(data, dict):
        config_path.write_text(json.dumps(CONFIG_TEMPLATE, indent=2), encoding="utf-8")
        return dict(CONFIG_TEMPLATE)
    merged = dict(CONFIG_TEMPLATE)
    merged.update(data)
    return merged


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def load_runtime_config() -> tuple[str, int | None, list[str], Path]:
    load_local_env()
    config = read_config_json()
    bot_token = first_non_empty(os.environ.get("BOT_TOKEN"), config.get("bot_token"))
    admin_raw = first_non_empty(
        os.environ.get("ADMIN_ID"),
        os.environ.get("ADMIN_USER_ID"),
        DEFAULT_ADMIN_ID,
    )
    try:
        admin_id = int(admin_raw) if admin_raw else None
    except ValueError:
        admin_id = None

    missing_required: list[str] = []
    if not bot_token:
        missing_required.append("BOT_TOKEN or config.json.bot_token")

    return bot_token, admin_id, missing_required, BASE_DIR / "config.json"


BOT_TOKEN, ADMIN_ID, MISSING_REQUIRED_CONFIG, CONFIG_PATH = load_runtime_config()
POLL_TIMEOUT = 60
ADMIN_ONLY_COMMANDS = {
    "!admin",
    "!help",
    "!mode",
    "!setprice",
    "!setupi",
    "!setmid",
    "!settarget",
    "!setdemo",
    "!setproof",
    "!setimage",
    "!setstart",
    "!setpaymenttext",
    "!setnote",
    "!setredirect",
    "!addchannel",
    "!delchannel",
    "!broadcast",
}

START_TEXT = (
    "<b>Welcome!</b>\n\n"
    "Use the buttons below to view demo, proofs, or buy premium access."
)
PAYMENT_TEXT = (
    "<b>Premium Access</b>\n\n"
    "Scan the QR code or click the Pay ₹49 button to complete the payment.\nभुगतान करने के लिए QR कोड स्कैन करें या Pay ₹49 बटन पर क्लिक करें।\n\n"
    "After completing the payment, please send the screenshot or UTR number for verification.\nभुगतान पूर्ण करने के बाद कृपया स्क्रीनशॉट या UTR नंबर सत्यापन के लिए भेजें।"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "payment_mode": "manual",
    "price": "49",
    "payment_note": "Premium Access",
    "payment_redirect_url": "https://redirect-beta-lemon.vercel.app/",
    "upi_id": "xlgr@ptyes",
    "merchant_mid": "",
    "demo_link": "",
    "proof_link": "",
    "target_channel_id": "",
    "start_image": "",
    "start_text": START_TEXT,
    "payment_text": PAYMENT_TEXT,
    "extra_channels": [],
    "button_texts": {
        "demo": "CHECK DEMO",
        "proofs": "CHECK PROOFS",
        "join_channel": "JOIN CHANNEL",
        "premium": "BUY NOW",
        "pay_now": "pay ₹49",
        "verify_payment": "VERIFY PAYMENT",
        "back": "BACK",
        "private_link": "GET PRIVATE LINK",
    },
}

DEFAULT_USERS = {"users": {}}
DEFAULT_PAID: dict[str, Any] = {}
DEFAULT_AUTO_PAYMENT: dict[str, Any] = {}
DEFAULT_TRANSACTIONS: list[dict[str, Any]] = []

app = Flask(__name__)
BOOTSTRAP_ATTEMPTED = False


FILE_DEFAULTS: dict[str, Any] = {
    "settings.json": DEFAULT_SETTINGS,
    "users.json": DEFAULT_USERS,
    "paid.json": DEFAULT_PAID,
    "auto_payment.json": DEFAULT_AUTO_PAYMENT,
    "transactions.json": DEFAULT_TRANSACTIONS,
}


def deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def read_json(name: str, default: Any) -> Any:
    path = DATA_DIR / name
    if not path.exists():
        write_json(name, default)
        return deep_copy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        write_json(name, default)
        return deep_copy(default)


def write_json(name: str, payload: Any) -> None:
    path = DATA_DIR / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_config_json(payload: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_dict(defaults: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deep_copy(defaults)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def migrate_legacy_settings() -> None:
    settings = read_json("settings.json", DEFAULT_SETTINGS)
    changed = False

    legacy_map = {
        "config.json": {
            "demo_channel_link": "demo_link",
            "proofs_channel_link": "proof_link",
            "target_channel_id": "target_channel_id",
        },
        "price.json": {
            "amount": "price",
            "message": "payment_note",
        },
        "img.json": {"url": "start_image"},
        "caption.json": {"text": "start_text"},
        "caption2.json": {"text": "payment_text"},
    }

    for file_name, field_map in legacy_map.items():
        legacy_path = DATA_DIR / file_name
        if not legacy_path.exists():
            legacy_path = BASE_DIR / file_name
        if not legacy_path.exists():
            continue
        try:
            legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for old_key, new_key in field_map.items():
            value = legacy_data.get(old_key)
            if value and not settings.get(new_key):
                settings[new_key] = value
                changed = True

    button_path = DATA_DIR / "button_texts.json"
    if not button_path.exists():
        button_path = BASE_DIR / "button_texts.json"
    if button_path.exists():
        try:
            button_data = json.loads(button_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            button_data = {}
        if isinstance(button_data, dict):
            settings["button_texts"] = merge_dict(DEFAULT_SETTINGS["button_texts"], button_data)
            changed = True

    channels_path = DATA_DIR / "channels.json"
    if not channels_path.exists():
        channels_path = BASE_DIR / "channels.json"
    if channels_path.exists():
        try:
            channels = json.loads(channels_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            channels = []
        if isinstance(channels, list) and channels and not settings.get("extra_channels"):
            settings["extra_channels"] = channels
            changed = True

    settings = merge_dict(DEFAULT_SETTINGS, settings)
    if changed:
        write_json("settings.json", settings)
    else:
        write_json("settings.json", settings)


for file_name, default_value in FILE_DEFAULTS.items():
    if not (DATA_DIR / file_name).exists():
        write_json(file_name, default_value)

migrate_legacy_settings()


def get_settings() -> dict[str, Any]:
    stored = read_json("settings.json", DEFAULT_SETTINGS)
    merged = merge_dict(DEFAULT_SETTINGS, stored if isinstance(stored, dict) else {})
    write_json("settings.json", merged)
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    write_json("settings.json", merge_dict(DEFAULT_SETTINGS, settings))


def is_admin(user_id: int | str | None) -> bool:
    return ADMIN_ID is not None and str(user_id or "") == str(ADMIN_ID)


def is_admin_command(text: str) -> bool:
    command = text.partition(" ")[0].lower()
    return command in ADMIN_ONLY_COMMANDS


def bot_api(method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        return {"ok": False, "description": str(exc)}


def send_message(chat_id: int | str, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return bot_api("sendMessage", payload)


def send_photo(chat_id: int | str, photo: str, caption: str = "", reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return bot_api("sendPhoto", payload)


def answer_callback(callback_id: str, text: str | None = None, show_alert: bool = False) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    bot_api("answerCallbackQuery", payload)


def create_invite_link() -> str | None:
    target = str(get_settings().get("target_channel_id", "")).strip()
    if not target:
        return None
    response = bot_api(
        "createChatInviteLink",
        {"chat_id": target, "expire_date": int(time.time()) + 900, "member_limit": 1},
    )
    if response.get("ok"):
        return response["result"].get("invite_link")
    return None


def users_store() -> dict[str, Any]:
    data = read_json("users.json", DEFAULT_USERS)
    if not isinstance(data, dict) or "users" not in data or not isinstance(data["users"], dict):
        data = deep_copy(DEFAULT_USERS)
    return data


def save_users(data: dict[str, Any]) -> None:
    write_json("users.json", data)


def update_user(user: dict[str, Any]) -> None:
    user_id = str(user.get("id", "")).strip()
    if not user_id:
        return
    data = users_store()
    info = data["users"].get(user_id, {})
    info.update(
        {
            "user_id": int(user_id),
            "username": user.get("username", info.get("username", "")),
            "full_name": " ".join(filter(None, [user.get("first_name", ""), user.get("last_name", "")])).strip(),
            "state": info.get("state", "idle"),
            "last_seen": int(time.time()),
        }
    )
    data["users"][user_id] = info
    save_users(data)


def get_user_info(user_id: int | str) -> dict[str, Any]:
    return users_store()["users"].get(str(user_id), {})


def set_user_info(user_id: int | str, **fields: Any) -> None:
    data = users_store()
    key = str(user_id)
    info = data["users"].get(key, {"user_id": int(key)})
    info.update(fields)
    data["users"][key] = info
    save_users(data)


def button_texts() -> dict[str, str]:
    return merge_dict(DEFAULT_SETTINGS["button_texts"], get_settings().get("button_texts", {}))


def start_keyboard() -> dict[str, Any]:
    labels = button_texts()
    settings = get_settings()
    rows: list[list[dict[str, Any]]] = []
    demo = str(settings.get("demo_link", "")).strip()
    proof = str(settings.get("proof_link", "")).strip()
    if demo:
        rows.append([{"text": labels["demo"], "url": demo}])
    if proof:
        rows.append([{"text": labels["proofs"], "url": proof}])
    for item in settings.get("extra_channels", [])[:6]:
        if isinstance(item, dict) and item.get("link"):
            rows.append([{"text": labels["join_channel"], "url": item["link"]}])
    rows.append([{"text": labels["premium"], "callback_data": "get_premium", "style": "primary"}])
    return {"inline_keyboard": rows}


def payment_keyboard(amount: str | None = None) -> dict[str, Any]:
    labels = button_texts()
    mode = get_settings().get("payment_mode", "manual")
    if mode == "auto":
        return {
            "inline_keyboard": [
                [{"text": labels["private_link"], "callback_data": "check_payment", "style": "success"}],
                [{"text": labels["back"], "callback_data": "back_menu", "style": "danger"}],
            ]
        }
    rows = []
    if amount:
        rows.append([{"text": labels["pay_now"], "url": build_payment_redirect_url(amount)}])
    rows.extend(
        [
            [{"text": labels["verify_payment"], "callback_data": "send_manual_proof", "style": "success"}],
            [{"text": labels["back"], "callback_data": "back_menu", "style": "danger"}],
        ]
    )
    return {
        "inline_keyboard": rows
    }


def payment_text_fallback(amount: str) -> str:
    settings = get_settings()
    caption = settings.get("payment_text") or PAYMENT_TEXT
    mode = settings.get("payment_mode", "manual")
    return (
        f"{caption}\n\nAmount: Rs {amount}\nMode: {'Auto Merchant' if mode == 'auto' else 'Manual UPI'}"
        f"\n\nUPI ID: <code>{settings.get('upi_id', '')}</code>"
        "\nPayment ke baad VERIFY PAYMENT dabao aur screenshot ya UTR bhejo."
    )


def upi_link_text(amount: str) -> str:
    return (
        f"<b>Pay Rs {amount}</b>\n\n"
        f"UPI ID: <code>{get_settings().get('upi_id', '')}</code>\n"
        f"UPI Link: <code>{build_upi_link(amount)}</code>\n\n"
        "Agar phone me UPI apps configured hain, is link ko tap ya copy karke open kar sakte ho."
    )


def send_start(chat_id: int | str) -> None:
    settings = get_settings()
    caption = settings.get("start_text") or START_TEXT
    image = str(settings.get("start_image", "")).strip()
    if image:
        response = send_photo(chat_id, image, caption, start_keyboard())
        if response.get("ok"):
            return
    send_message(chat_id, caption, start_keyboard())


def build_upi_link(amount: str) -> str:
    settings = get_settings()
    upi_id = str(settings.get("upi_id", "")).strip()
    note = str(settings.get("payment_note", "Payment")).strip() or "Payment"
    encoded_note = quote(note, safe="")
    encoded_upi = quote(upi_id, safe="")
    return f"upi://pay?pa={encoded_upi}&pn=Payment&am={amount}&cu=INR&tn={encoded_note}"


def build_payment_redirect_url(amount: str) -> str:
    settings = get_settings()
    template = str(settings.get("payment_redirect_url", "")).strip()
    upi_link = build_upi_link(amount)
    if not template:
        return upi_link
    return (
        template
        .replace("{amount}", quote(str(amount), safe=""))
        .replace("{upi_id}", quote(str(settings.get("upi_id", "")).strip(), safe=""))
        .replace("{note}", quote(str(settings.get("payment_note", "Payment")).strip() or "Payment", safe=""))
        .replace("{upi_link}", quote(upi_link, safe=""))
    )


def qr_image_for_data(data: str) -> str:
    return f"https://api.qrserver.com/v1/create-qr-code/?size=320x320&data={quote(data, safe='')}"


def ensure_auto_session(user_id: int | str) -> dict[str, Any]:
    settings = get_settings()
    amount = str(settings.get("price", "")).strip()
    upi_id = str(settings.get("upi_id", "")).strip()
    merchant_mid = str(settings.get("merchant_mid", "")).strip()
    note = str(settings.get("payment_note", "Premium Access")).strip() or "Premium Access"
    if not amount:
        raise RuntimeError("Price set nahi hai.")
    if not upi_id:
        raise RuntimeError("UPI ID set nahi hai.")
    if not merchant_mid:
        raise RuntimeError("Merchant MID set nahi hai.")

    auto_data = read_json("auto_payment.json", DEFAULT_AUTO_PAYMENT)
    key = str(user_id)
    existing = auto_data.get(key)
    if isinstance(existing, dict) and existing.get("status") == "pending":
        created_at = int(existing.get("created_at", 0) or 0)
        if time.time() - created_at < 1800:
            return existing

    try:
        response = requests.get(
            "https://payment.pikaapis.workers.dev/",
            params={"id": merchant_mid, "upi": upi_id, "amount": amount, "note": note},
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Merchant QR generate nahi hua: {exc}") from exc

    if data.get("status") != "success":
        raise RuntimeError(data.get("message") or "Merchant QR generate nahi hua.")

    session_data = {
        "track_id": data.get("trackId", ""),
        "amount": amount,
        "qr_image_url": data.get("qrImageUrl", ""),
        "status": "pending",
        "claimed": False,
        "created_at": int(time.time()),
    }
    auto_data[key] = session_data
    write_json("auto_payment.json", auto_data)
    return session_data


def send_payment_details(chat_id: int | str, user_id: int | str) -> None:
    settings = get_settings()
    amount = str(settings.get("price", "")).strip() or "99"
    mode = settings.get("payment_mode", "manual")
    caption = settings.get("payment_text") or PAYMENT_TEXT
    caption = f"{caption}\n\nAmount: Rs {amount}\nMode: {'Auto Merchant' if mode == 'auto' else 'Manual UPI'}"

    if mode == "auto":
        session_data = ensure_auto_session(user_id)
        qr_url = session_data.get("qr_image_url", "")
        if qr_url:
            response = send_photo(chat_id, qr_url, caption, payment_keyboard())
            if response.get("ok"):
                return
        response = send_message(chat_id, caption, payment_keyboard())
        if response.get("ok"):
            return
        raise RuntimeError(response.get("description") or "Payment details send nahi ho paye.")
        return

    upi_link = build_upi_link(amount)
    qr_url = qr_image_for_data(upi_link)
    extra = (
        f"\n\nUPI ID: <code>{get_settings().get('upi_id', '')}</code>"
        "\nPayment ke baad VERIFY PAYMENT dabao aur screenshot ya UTR bhejo."
    )
    response = send_photo(chat_id, qr_url, caption + extra, payment_keyboard(amount))
    if response.get("ok"):
        return
    response = send_message(chat_id, payment_text_fallback(amount), payment_keyboard(amount))
    if response.get("ok"):
        return
    raise RuntimeError(response.get("description") or "Payment details send nahi ho paye.")


def append_transaction(entry: dict[str, Any]) -> None:
    items = read_json("transactions.json", DEFAULT_TRANSACTIONS)
    if not isinstance(items, list):
        items = []
    items.append(entry)
    write_json("transactions.json", items[-500:])


def mark_paid(user_id: int | str, mode: str, transaction_ref: str) -> str | None:
    invite_link = create_invite_link()
    if not invite_link:
        return None
    paid = read_json("paid.json", DEFAULT_PAID)
    key = str(user_id)
    info = get_user_info(user_id)
    paid[key] = {
        "user_id": int(key),
        "username": info.get("username", ""),
        "approved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "transaction_ref": transaction_ref,
        "invite_link": invite_link,
    }
    write_json("paid.json", paid)
    append_transaction(paid[key])
    return invite_link


def verify_auto_payment(user_id: int | str) -> tuple[bool, str]:
    settings = get_settings()
    merchant_mid = str(settings.get("merchant_mid", "")).strip()
    if not merchant_mid:
        return False, "Merchant MID set nahi hai."

    auto_data = read_json("auto_payment.json", DEFAULT_AUTO_PAYMENT)
    key = str(user_id)
    session_data = auto_data.get(key)
    if not isinstance(session_data, dict):
        return False, "Auto payment session missing hai. Naya QR generate karo."
    if session_data.get("claimed"):
        return False, "Payment already claim ho chuka hai."

    try:
        response = requests.get(
            "https://verify.pikaapis.workers.dev/",
            params={"id": merchant_mid, "trn": session_data.get("track_id", "")},
            timeout=25,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException:
        return False, "Payment verify service tak reach nahi hua."

    status = str(data.get("STATUS", "")).upper()
    if status in {"FAILURE", "TXN_FAILED"}:
        return False, "Payment failed dikh raha hai."
    if status == "PENDING":
        return False, "Payment abhi pending hai."
    if status != "TXN_SUCCESS":
        return False, "Payment abhi receive nahi hua."
    if str(data.get("TXNAMOUNT", "")) != str(session_data.get("amount", "")):
        return False, f"Amount mismatch. Expected Rs {session_data.get('amount')}"

    invite_link = mark_paid(user_id, "auto", str(session_data.get("track_id", "")))
    if not invite_link:
        return False, "Target channel ID set nahi hai."
    session_data["status"] = "paid"
    session_data["claimed"] = True
    auto_data[key] = session_data
    write_json("auto_payment.json", auto_data)
    set_user_info(user_id, state="idle")
    return True, invite_link


def forward_manual_review(user_id: int | str, proof_type: str, proof_value: str, caption: str = "") -> None:
    user_info = get_user_info(user_id)
    caption_text = (
        "<b>Manual Payment Review</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Username: @{user_info.get('username', 'unknown')}\n"
        f"Type: {proof_type}\n"
    )
    if proof_type == "photo":
        if caption:
            caption_text += f"Caption: {caption}\n"
        bot_api(
            "sendPhoto",
            {
                "chat_id": ADMIN_ID,
                "photo": proof_value,
                "caption": caption_text,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "Approve", "callback_data": f"approve_{user_id}", "style": "success"},
                        {"text": "Reject", "callback_data": f"reject_{user_id}", "style": "danger"},
                    ]]
                },
            },
        )
        return
    caption_text += f"Proof: <code>{proof_value}</code>"
    send_message(
        ADMIN_ID,
        caption_text,
        {
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": f"approve_{user_id}", "style": "success"},
                {"text": "Reject", "callback_data": f"reject_{user_id}", "style": "danger"},
            ]]
        },
    )


def approve_manual_payment(user_id: int | str) -> tuple[bool, str]:
    info = get_user_info(user_id)
    ref = str(info.get("last_manual_proof", "manual-approved"))
    invite_link = mark_paid(user_id, "manual", ref)
    if not invite_link:
        return False, "Target channel ID set nahi hai."
    set_user_info(user_id, state="idle", last_manual_proof=ref)
    send_message(user_id, f"<b>Payment approved.</b>\n\nYeh raha aapka private link:\n{invite_link}")
    return True, invite_link


def reject_manual_payment(user_id: int | str) -> None:
    set_user_info(user_id, state="idle")
    send_message(user_id, "<b>Payment rejected.</b>\n\nClear screenshot ya sahi UTR ke saath dubara bhejo.")


def settings_summary() -> str:
    settings = get_settings()
    return (
        "<b>Admin Panel</b>\n\n"
        f"Admin ID: <code>{ADMIN_ID}</code>\n"
        f"Mode: <code>{settings.get('payment_mode')}</code>\n"
        f"Price: <code>Rs {settings.get('price')}</code>\n"
        f"UPI: <code>{settings.get('upi_id') or '-'}</code>\n"
        f"MID: <code>{settings.get('merchant_mid') or '-'}</code>\n"
        f"Redirect URL: <code>{settings.get('payment_redirect_url') or '-'}</code>\n"
        f"Demo: <code>{settings.get('demo_link') or '-'}</code>\n"
        f"Proof: <code>{settings.get('proof_link') or '-'}</code>\n"
        f"Target Channel: <code>{settings.get('target_channel_id') or '-'}</code>\n"
        f"Users: <code>{len(users_store().get('users', {}))}</code>\n\n"
        "Commands:\n"
        "!mode manual or !mode auto\n"
        "!setprice 49\n"
        "!setupi yourupi@bank\n"
        "!setmid MID\n"
        "!settarget -100...\n"
        "!setdemo https://...\n"
        "!setproof https://...\n"
        "!setimage https://...\n"
        "!setstart your text\n"
        "!setpaymenttext your text\n"
        "!setnote Premium Access\n"
        "!setredirect https://redirect-beta-lemon.vercel.app/\n"
        "!addchannel https://...\n"
        "!delchannel 1\n"
        "!broadcast message"
    )


def admin_help_text() -> str:
    settings = get_settings()
    return (
        "<b>Admin Help</b>\n\n"
        "Yeh bot Telegram se hi fully manage hoga. Sirf admin commands se settings change hongi.\n\n"
        "<b>Current Setup</b>\n"
        f"Mode: <code>{settings.get('payment_mode')}</code>\n"
        f"Price: <code>Rs {settings.get('price')}</code>\n"
        f"UPI: <code>{settings.get('upi_id') or '-'}</code>\n"
        f"MID: <code>{settings.get('merchant_mid') or '-'}</code>\n"
        f"Redirect URL: <code>{settings.get('payment_redirect_url') or '-'}</code>\n"
        f"Target Channel: <code>{settings.get('target_channel_id') or '-'}</code>\n\n"
        "<b>Main Commands</b>\n"
        "<code>!admin</code> - current settings aur quick status dikhata hai\n"
        "<code>!help</code> - yeh full help message dikhata hai\n"
        "<code>!broadcast your message</code> - sab users ko message bhejta hai\n\n"
        "<b>Payment Mode Commands</b>\n"
        "<code>!mode manual</code> - normal UPI payment + screenshot/UTR + admin approve/reject\n"
        "<code>!mode auto</code> - merchant detect payment mode on karta hai\n"
        "<code>!setprice 49</code> - payment amount set karta hai\n"
        "<code>!setnote Premium Access</code> - payment note/remark set karta hai\n"
        "<code>!setredirect https://...</code> - Pay button ke liye redirect/open link set karta hai\n"
        "<code>!setupi xlgr@ptyes</code> - UPI ID set karta hai\n"
        "<code>!setmid YOUR_MID</code> - merchant MID set karta hai, auto mode ke liye zaroori\n\n"
        "<b>Channel And Links</b>\n"
        "<code>!settarget -100...</code> - private channel/group ID set karta hai jahan se invite link banega\n"
        "<code>!setdemo https://...</code> - demo button ka link set karta hai\n"
        "<code>!setproof https://...</code> - proof button ka link set karta hai\n"
        "<code>!addchannel https://...</code> - extra join channel button add karta hai\n"
        "<code>!delchannel 1</code> - extra channel list me given number wala item remove karta hai\n\n"
        "<b>Content Commands</b>\n"
        "<code>!setimage https://...</code> - start screen image set karta hai\n"
        "<code>!setstart your text</code> - /start par dikhne wala message set karta hai\n"
        "<code>!setpaymenttext your text</code> - payment page ka caption set karta hai\n\n"
        "<b>Manual Payment Flow</b>\n"
        "1. <code>!mode manual</code> use karo\n"
        "2. User Pay button dabayega aur configured redirect/open link par jayega\n"
        "3. User payment karega aur VERIFY PAYMENT dabayega\n"
        "4. User screenshot ya UTR bhejega\n"
        "5. Admin ko Approve/Reject buttons milenge\n"
        "6. Approve par private invite link user ko chala jayega\n\n"
        "<b>Auto Payment Flow</b>\n"
        "1. <code>!mode auto</code> use karo\n"
        "2. <code>!setupi</code> aur <code>!setmid</code> dono set hone chahiye\n"
        "3. User ke liye merchant QR generate hoga\n"
        "4. User GET PRIVATE LINK button se verify karega\n"
        "5. Successful payment par link automatically mil jayega\n\n"
        "<b>UPI QR Format</b>\n"
        "Manual mode me QR is type ke UPI link se banta hai:\n"
        "<code>upi://pay?pa=xlgr@ptyes&pn=Payment&am=49&cu=INR</code>\n\n"
        "<b>Note</b>\n"
        "Approve/Reject button sirf admin ke liye kaam karega. Normal user settings change nahi kar sakta."
    )


def handle_admin_command(message: dict[str, Any], text: str) -> bool:
    chat_id = message["chat"]["id"]
    command, _, arg = text.partition(" ")
    arg = arg.strip()
    settings = get_settings()

    if command == "!admin":
        send_message(chat_id, settings_summary())
        return True
    if command == "!help":
        send_message(chat_id, admin_help_text())
        return True
    if command == "!mode" and arg.lower() in {"manual", "auto"}:
        settings["payment_mode"] = arg.lower()
        save_settings(settings)
        send_message(chat_id, f"Payment mode set to <b>{arg.lower()}</b>.")
        return True
    if command == "!setprice" and arg:
        settings["price"] = arg
        save_settings(settings)
        send_message(chat_id, f"Price updated to Rs {arg}.")
        return True
    if command == "!setupi" and arg:
        settings["upi_id"] = arg
        save_settings(settings)
        send_message(chat_id, f"UPI updated to <code>{arg}</code>.")
        return True
    if command == "!setmid" and arg:
        settings["merchant_mid"] = arg
        save_settings(settings)
        send_message(chat_id, f"Merchant MID updated to <code>{arg}</code>.")
        return True
    if command == "!settarget" and arg:
        settings["target_channel_id"] = arg
        save_settings(settings)
        send_message(chat_id, "Target channel updated.")
        return True
    if command == "!setdemo" and arg:
        settings["demo_link"] = arg
        save_settings(settings)
        send_message(chat_id, "Demo link updated.")
        return True
    if command == "!setproof" and arg:
        settings["proof_link"] = arg
        save_settings(settings)
        send_message(chat_id, "Proof link updated.")
        return True
    if command == "!setimage" and arg:
        settings["start_image"] = arg
        save_settings(settings)
        send_message(chat_id, "Start image updated.")
        return True
    if command == "!setstart" and arg:
        settings["start_text"] = arg
        save_settings(settings)
        send_message(chat_id, "Start text updated.")
        return True
    if command == "!setpaymenttext" and arg:
        settings["payment_text"] = arg
        save_settings(settings)
        send_message(chat_id, "Payment text updated.")
        return True
    if command == "!setnote" and arg:
        settings["payment_note"] = arg
        save_settings(settings)
        send_message(chat_id, "Payment note updated.")
        return True
    if command == "!setredirect" and arg:
        settings["payment_redirect_url"] = arg
        save_settings(settings)
        send_message(chat_id, "Payment redirect URL updated.")
        return True
    if command == "!addchannel" and arg:
        channels = settings.get("extra_channels", [])
        channels.append({"link": arg, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        settings["extra_channels"] = channels[-10:]
        save_settings(settings)
        send_message(chat_id, "Extra channel added.")
        return True
    if command == "!delchannel" and arg.isdigit():
        index = int(arg) - 1
        channels = settings.get("extra_channels", [])
        if 0 <= index < len(channels):
            channels.pop(index)
            settings["extra_channels"] = channels
            save_settings(settings)
            send_message(chat_id, "Extra channel removed.")
        else:
            send_message(chat_id, "Invalid channel index.")
        return True
    if command == "!broadcast" and arg:
        recipients = list(users_store().get("users", {}).keys())
        sent = 0
        for user_id in recipients:
            response = send_message(user_id, arg)
            if response.get("ok"):
                sent += 1
            time.sleep(0.05)
        send_message(chat_id, f"Broadcast complete. Sent: {sent}/{len(recipients)}")
        return True
    return False


def handle_start(message: dict[str, Any]) -> None:
    send_start(message["chat"]["id"])


def handle_text_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    update_user(user)

    if text.startswith("/start"):
        handle_start(message)
        return

    if is_admin(user_id) and text.startswith("!") and handle_admin_command(message, text):
        return

    if text.startswith("!"):
        if is_admin_command(text):
            send_message(chat_id, "Yeh command sirf admin use kar sakta hai.")
            return
        send_message(chat_id, "Normal users ke liye sirf /start available hai.")
        return

    if text.startswith("/"):
        if is_admin_command("!" + text[1:]):
            send_message(chat_id, "Admin commands ab ! prefix se chalti hain.")
            return
        send_message(chat_id, "Normal users ke liye sirf /start available hai.")
        return

    info = get_user_info(user_id)
    state = info.get("state", "idle")
    mode = get_settings().get("payment_mode", "manual")

    if state == "awaiting_manual_proof" and mode == "manual":
        set_user_info(user_id, state="pending_admin_review", last_manual_proof=text)
        forward_manual_review(user_id, "text", text)
        send_message(chat_id, "Proof admin ko bhej diya gaya hai. Approval ke baad private link milega.")
        return

    send_start(chat_id)


def handle_photo_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    update_user(user)
    info = get_user_info(user_id)
    if info.get("state") != "awaiting_manual_proof":
        send_message(chat_id, "Pehle payment flow open karo aur VERIFY PAYMENT dabao.")
        return
    photos = message.get("photo") or []
    if not photos:
        send_message(chat_id, "Photo read nahi hua. Dobara bhejo.")
        return
    file_id = photos[-1].get("file_id")
    set_user_info(user_id, state="pending_admin_review", last_manual_proof=file_id)
    forward_manual_review(user_id, "photo", file_id, message.get("caption", ""))
    send_message(chat_id, "Screenshot admin ko bhej diya gaya hai. Approval ke baad private link milega.")


def handle_callback_query(callback_query: dict[str, Any]) -> None:
    data = callback_query.get("data", "")
    callback_id = callback_query.get("id", "")
    message = callback_query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    user = callback_query.get("from") or {}
    user_id = user.get("id")
    update_user(user)

    if data == "back_menu":
        answer_callback(callback_id)
        if chat_id:
            send_start(chat_id)
        return

    if data == "get_premium":
        answer_callback(callback_id)
        try:
            send_payment_details(chat_id, user_id)
        except RuntimeError as exc:
            if chat_id:
                send_message(chat_id, str(exc))
        return

    if data == "send_manual_proof":
        set_user_info(user_id, state="awaiting_manual_proof")
        answer_callback(callback_id)
        if chat_id:
            send_message(chat_id, "Screenshot ya UTR bhejo. Admin manually approve ya reject karega.")
        return

    if data.startswith("show_upi_"):
        amount = data.split("show_upi_", 1)[1] or str(get_settings().get("price", "")).strip() or "99"
        answer_callback(callback_id)
        if chat_id:
            send_message(chat_id, upi_link_text(amount))
        return

    if data == "check_payment":
        answer_callback(callback_id)
        ok, result = verify_auto_payment(user_id)
        if chat_id:
            if ok:
                send_message(chat_id, f"Payment successful. Yeh raha aapka private link:\n{result}")
            else:
                send_message(chat_id, result)
        return

    if data.startswith("approve_"):
        if not is_admin(user_id):
            answer_callback(callback_id, "Access denied", True)
            return
        target_user_id = data.split("_", 1)[1]
        ok, result = approve_manual_payment(target_user_id)
        answer_callback(callback_id, "Approved" if ok else result, not ok)
        if chat_id:
            send_message(chat_id, f"Approval result: {result}")
        return

    if data.startswith("reject_"):
        if not is_admin(user_id):
            answer_callback(callback_id, "Access denied", True)
            return
        target_user_id = data.split("_", 1)[1]
        reject_manual_payment(target_user_id)
        answer_callback(callback_id, "Rejected")
        if chat_id:
            send_message(chat_id, f"Payment rejected for user {target_user_id}.")
        return

    answer_callback(callback_id)


def set_bot_commands() -> None:
    public_commands = [
        {"command": "start", "description": "Open user menu"},
    ]
    bot_api("setMyCommands", {"commands": public_commands})
    if ADMIN_ID is not None:
        bot_api(
            "setMyCommands",
            {
                "scope": {"type": "chat", "chat_id": ADMIN_ID},
                "commands": public_commands,
            },
        )


def process_update(update: dict[str, Any]) -> None:
    if message := update.get("message"):
        if message.get("photo"):
            handle_photo_message(message)
        else:
            handle_text_message(message)
    elif callback_query := update.get("callback_query"):
        handle_callback_query(callback_query)


@app.get("/")
def home() -> Response:
    bootstrap_runtime()
    return Response(render_status_page(), mimetype="text/html")


@app.get("/health")
def health() -> Response:
    bootstrap_runtime()
    return Response("ok", mimetype="text/plain")


@app.post("/telegram")
def telegram_webhook() -> Response:
    bootstrap_runtime()
    update = request.get_json(silent=True) or {}
    if not isinstance(update, dict):
        return Response("invalid payload", status=400, mimetype="text/plain")
    process_update(update)
    return Response("ok", mimetype="text/plain")


def poll_forever() -> None:
    offset = 0
    while True:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": offset},
                timeout=POLL_TIMEOUT + 10,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException:
            time.sleep(3)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            process_update(update)


def webhook_mode_enabled() -> bool:
    return bool(
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("VERCEL_URL")
    )


def webhook_target_url() -> str:
    base = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("VERCEL_URL")
        or ""
    )
    if base and not base.startswith("http"):
        base = f"https://{base}"
    path = os.environ.get("WEBHOOK_PATH", "/telegram")
    return f"{base.rstrip('/')}{path}"


def set_webhook(url: str) -> None:
    if not url:
        return
    bot_api("setWebhook", {"url": url})


def running_on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def bootstrap_runtime() -> None:
    global BOOTSTRAP_ATTEMPTED
    if BOOTSTRAP_ATTEMPTED or not BOT_TOKEN:
        BOOTSTRAP_ATTEMPTED = True
        return
    set_bot_commands()
    if webhook_mode_enabled():
        url = webhook_target_url()
        if url:
            set_webhook(url)
    BOOTSTRAP_ATTEMPTED = True


def status_badge(label: str, ok: bool) -> str:
    badge_class = "ok" if ok else "warn"
    badge_text = "Ready" if ok else "Missing"
    return (
        f"<div class='row'><span>{escape(label)}</span>"
        f"<span class='badge {badge_class}'>{badge_text}</span></div>"
    )


def render_status_page() -> str:
    settings = get_settings()
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("VERCEL_URL", "")
    if base_url and not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    webhook_url = webhook_target_url() if webhook_mode_enabled() else f"{base_url.rstrip('/')}/telegram" if base_url else ""
    mode = str(settings.get("payment_mode", "manual")).strip() or "manual"
    price = str(settings.get("price", "49")).strip() or "49"
    storage_type = "Temporary /tmp storage" if running_on_vercel() else "Local file storage"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Bot Status</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #08111f;
      --panel: rgba(9, 18, 34, 0.88);
      --line: rgba(255, 255, 255, 0.1);
      --text: #f5f7fb;
      --muted: #9eb1c7;
      --ok: #21c17a;
      --warn: #ffb84d;
      --accent: #56a3ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Segoe UI", Tahoma, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top, rgba(86, 163, 255, 0.28), transparent 34%),
        linear-gradient(180deg, #06101d 0%, #0b1628 100%);
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    .card {{
      width: min(760px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 28px;
      box-shadow: 0 20px 70px rgba(0, 0, 0, 0.35);
      backdrop-filter: blur(18px);
    }}
    .eyebrow {{
      color: #8eb8ff;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      font-size: 12px;
      margin-bottom: 12px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(28px, 5vw, 44px);
    }}
    p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-top: 24px;
    }}
    .tile {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.03);
    }}
    .label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: 18px;
      font-weight: 600;
      word-break: break-word;
    }}
    .stack {{
      margin-top: 24px;
      padding: 18px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.03);
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }}
    .row:last-child {{ border-bottom: 0; }}
    .badge {{
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 700;
    }}
    .badge.ok {{ background: rgba(33, 193, 122, 0.16); color: #7ff0b2; }}
    .badge.warn {{ background: rgba(255, 184, 77, 0.15); color: #ffd089; }}
    code {{
      font-family: Consolas, monospace;
      color: #d2e6ff;
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 8px;
    }}
  </style>
</head>
<body>
  <main class="card">
    <div class="eyebrow">Vercel Telegram Bot</div>
    <h1>Bot Running</h1>
    <p>This project is now serving like a website on the root route and listening for Telegram updates on <code>/telegram</code>.</p>

    <section class="grid">
      <div class="tile">
        <div class="label">Platform</div>
        <div class="value">{escape("Vercel" if running_on_vercel() else "Local / Other")}</div>
      </div>
      <div class="tile">
        <div class="label">Payment Mode</div>
        <div class="value">{escape(mode.title())}</div>
      </div>
      <div class="tile">
        <div class="label">Price</div>
        <div class="value">Rs {escape(price)}</div>
      </div>
      <div class="tile">
        <div class="label">Storage</div>
        <div class="value">{escape(storage_type)}</div>
      </div>
      <div class="tile">
        <div class="label">Data Directory</div>
        <div class="value"><code>{escape(str(DATA_DIR))}</code></div>
      </div>
      <div class="tile">
        <div class="label">Webhook URL</div>
        <div class="value"><code>{escape(webhook_url or 'Set WEBHOOK_URL in Vercel')}</code></div>
      </div>
    </section>

    <section class="stack">
      {status_badge("BOT_TOKEN", bool(BOT_TOKEN))}
      {status_badge("ADMIN_ID", ADMIN_ID is not None)}
      {status_badge("WEBHOOK_URL", bool(os.environ.get("WEBHOOK_URL") or os.environ.get("VERCEL_URL")))}
      {status_badge("Target Channel", bool(str(settings.get("target_channel_id", "")).strip()))}
    </section>
  </main>
</body>
</html>"""


def run_webhook_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    path = os.environ.get("WEBHOOK_PATH", "/telegram")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            if self.path != path:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                update = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            process_update(update)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("", port), Handler)
    print(f"Webhook server listening on 0.0.0.0:{port} path {path}")
    server.serve_forever()


def main() -> None:
    if MISSING_REQUIRED_CONFIG:
        missing = ", ".join(MISSING_REQUIRED_CONFIG)
        raise RuntimeError(
            f"Missing required config: {missing}. Fill .env or {CONFIG_PATH.name}."
        )
    set_bot_commands()
    print(f"Bot running with admin ID {ADMIN_ID or 'not-set'} and data dir {DATA_DIR}")
    if webhook_mode_enabled():
        url = webhook_target_url()
        if url:
            set_webhook(url)
            print(f"Webhook enabled: {url}")
        run_webhook_server()
    else:
        poll_forever()


if __name__ == "__main__":
    main()
