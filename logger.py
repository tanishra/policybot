import sqlite3
import os
import json
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import partial

DB_PATH = "dispositions.db"
_executor = ThreadPoolExecutor(max_workers=2)
_init_done = False

def _sync_init():
    global _init_done
    if _init_done:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            customer_name TEXT,
            mobile_number TEXT,
            policy_number TEXT,
            call_status TEXT,
            duration INTEGER,
            disposition TEXT,
            promise_to_pay_date TEXT,
            concern_category TEXT,
            concern_notes TEXT,
            alt_number TEXT,
            detected_language TEXT,
            sentiment TEXT,
            partial_amount TEXT,
            emi_option TEXT,
            call_back_time TEXT,
            transcript TEXT,
            recording_url TEXT
        )
    """)
    conn.commit()
    conn.close()
    _init_done = True

def init_sync():
    _sync_init()

async def init_db():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _sync_init)

def _sync_log_call(**kwargs):
    _sync_init()
    conn = sqlite3.connect(DB_PATH)
    timestamp = kwargs.pop("timestamp", datetime.now().isoformat())
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?" for _ in kwargs])
    vals = list(kwargs.values())
    conn.execute(
        f"INSERT INTO call_logs (timestamp, {cols}) VALUES (?, {placeholders})",
        [timestamp] + vals
    )
    conn.commit()
    conn.close()
    print(f"[LOGGER] Logged: {kwargs.get('customer_name', '?')} -> {kwargs.get('disposition', '?')}")

async def log_call(**kwargs):
    loop = asyncio.get_event_loop()
    kwargs.setdefault("timestamp", datetime.now().isoformat())
    await loop.run_in_executor(_executor, partial(_sync_log_call, **kwargs))
    asyncio.ensure_future(_fire_webhook(kwargs))

async def _fire_webhook(payload: dict):
    webhook_url = os.getenv("WEBHOOK_URL")
    sms_webhook = os.getenv("SMS_WEBHOOK_URL")
    whatsapp_api = os.getenv("WHATSAPP_API_URL")
    whatsapp_token = os.getenv("WHATSAPP_API_TOKEN")

    if webhook_url:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status >= 400:
                        print(f"[WEBHOOK] Failed status {resp.status}")
        except Exception as e:
            print(f"[WEBHOOK] Error: {e}")

    disposition = payload.get("disposition", "")
    mobile = payload.get("mobile_number", "")
    customer_name = payload.get("customer_name", "")

    # SMS fallback for failed calls
    if disposition in ("No Response", "Wrong Number", "Voicemail") and sms_webhook and mobile:
        sms_payload = {
            "to": mobile,
            "message": f"Dear {customer_name}, we tried reaching you regarding your insurance renewal. Please call us back at your earliest convenience."
        }
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(sms_webhook, json=sms_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"[SMS] Fallback sent to {mobile}: status {resp.status}")
        except Exception as e:
            print(f"[SMS] Error: {e}")

    # WhatsApp payment link for PTP
    if disposition == "Promise to Pay" and whatsapp_api and whatsapp_token and mobile:
        ptp_date = payload.get("ptp_date", "soon")
        wa_payload = {
            "to": mobile,
            "type": "template",
            "template": "payment_reminder",
            "parameters": {"customer_name": customer_name, "due_date": ptp_date}
        }
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    whatsapp_api, json=wa_payload,
                    headers={"Authorization": f"Bearer {whatsapp_token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    print(f"[WHATSAPP] Link sent to {mobile}: status {resp.status}")
        except Exception as e:
            print(f"[WHATSAPP] Error: {e}")

init_sync()
