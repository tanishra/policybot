"""
Chainlit UI for Renewal Voice Bot.
Simple interface to trigger outbound calls with auto-lookup from customer database.
"""
import chainlit as cl
import requests

BACKEND_URL = "http://localhost:8000"

@cl.on_chat_start
async def start():
    await cl.Message(
        content="""
# 🎙️ Renewal Voice Bot

Welcome to the **Renewal Voice Bot** - an AI-powered calling system for insurance renewals.

## Quick Start
Enter a phone number with country code to make a call.
The system will auto-lookup policy details from the customer database.

**e.g.,** `+919690190921`

## Features
- 📞 **Single Call** - Just type a phone number
- 📊 **Campaign** - Edit `campaign.csv` and run `python dialer.py`
- 📈 **History** - Calls logged to database automatically
        """
    ).send()

@cl.on_message
async def main(message: cl.Message):
    user_input = message.content.strip()

    if user_input.startswith("+") and len(user_input) >= 10:
        await handle_single_call(user_input)
    elif user_input.lower() in ["help", "status", "history"]:
        await show_help()
    else:
        await cl.Message(
            content="❓ Enter a phone number with country code (e.g., +919690190921)\n\nOr type 'help' for options."
        ).send()

async def handle_single_call(phone_number: str):
    msg = cl.Message(content=f"📞 Initiating call to {phone_number}...")
    await msg.send()

    try:
        response = requests.post(
            f"{BACKEND_URL}/api/call",
            json={"mobile_number": phone_number, "customer_name": ""},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            if data["status"] == "sip_not_configured":
                await cl.Message(
                    content=f"""
## ⚠️ SIP Not Configured

**Call initiated but SIP trunk is not configured.**

Details:
- Phone: {phone_number}
- Room: {data['room_name']}
- Call ID: {data['call_id']}

**Next Steps:**
1. Configure SIP trunk credentials in `.env` file
2. Restart the backend
3. Try again
                    """
                ).send()
            else:
                await cl.Message(
                    content=f"""
## ✅ Call Initiated Successfully!

- 📱 Phone: {phone_number}
- 🏠 Room: {data['room_name']}
- 🆔 Call ID: `{data['call_id']}`

The AI agent will call now. You should receive a call shortly.
                    """
                ).send()
        else:
            error = response.json().get("detail", "Unknown error")
            await cl.Message(content=f"❌ **Error:** {error}").send()

    except requests.exceptions.ConnectionError:
        await cl.Message(
            content="""
## 🔌 Connection Error

Cannot connect to the backend server.
Start the backend first: `python backend/main.py`
            """
        ).send()
    except Exception as e:
        await cl.Message(content=f"❌ **Error:** {str(e)}").send()

async def show_help():
    await cl.Message(
        content="""
## 📋 Available Commands

| Input | Action |
|-------|--------|
| `+91XXXXXXXXXX` | Make a call to this number |
| `help` | Show this help |

## How Customer Data Works
When you enter a phone number:

1. **Backend** looks up the number in `customers.json`
2. If found → policy details (name, plan, amount, due date) are auto-filled
3. Agent Priya uses these details in the conversation
4. If NOT found → agent greets with "Customer" (generic)

**Adding customers:** Edit `customers.json` and add entries like:
```json
"+919XXXXXXXXX": {
  "customer_name": "...",
  "policy_number": "POL-2024-...",
  "plan_name": "...",
  "due_amount": "25000",
  "due_date": "2026-06-15"
}
```
        """
    ).send()