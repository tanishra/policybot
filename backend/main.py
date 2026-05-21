"""
Production-grade FastAPI backend for the Renewal Voice Bot.
Handles outbound calls, campaign management, status tracking, and webhooks.
"""
from fastapi import FastAPI, HTTPException, Request, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import asyncio
import os
import json
import time
import uuid
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from livekit import api
import logging

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

# Load customer database
CUSTOMER_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "customers.json")
CUSTOMER_DB: dict[str, dict] = {}

def _load_customer_db():
    global CUSTOMER_DB
    try:
        with open(CUSTOMER_DB_PATH) as f:
            CUSTOMER_DB = json.load(f)
        logger.info(f"Loaded {len(CUSTOMER_DB)} customers from database")
    except FileNotFoundError:
        logger.warning(f"No customer database found. Create customers.json to auto-populate policy details.")
        CUSTOMER_DB = {}

_load_customer_db()

def lookup_customer(mobile_number: str) -> dict:
    if mobile_number in CUSTOMER_DB:
        return CUSTOMER_DB[mobile_number]
    stripped = mobile_number.lstrip("+")
    for key, val in CUSTOMER_DB.items():
        if key.lstrip("+") == stripped:
            return val
    return {}

# ── Auth ─────────────────────────────────────────────────────────────────────

API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN")

async def verify_auth(authorization: Optional[str] = Header(None)):
    if not API_AUTH_TOKEN:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != API_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")

# ── Rate Limiting ────────────────────────────────────────────────────────────

RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
_rate_buckets: dict[str, list] = defaultdict(list)

async def rate_limit(request: Request):
    if not API_AUTH_TOKEN:
        return
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_buckets[client_ip] = [t for t in _rate_buckets[client_ip] if t > window_start]
    if len(_rate_buckets[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Max {RATE_LIMIT_MAX} requests per {RATE_LIMIT_WINDOW}s")
    _rate_buckets[client_ip].append(now)

# ── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Renewal Voice Bot API",
    description="Backend API for triggering outbound calls and managing campaigns",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CALLS_STORE: Dict[str, dict] = {}

# ── Pydantic Models ─────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    mobile_number: str = Field(..., description="Customer mobile number with country code")
    customer_name: str = Field(default="Customer", description="Customer name")
    policy_number: Optional[str] = None
    plan_name: Optional[str] = None
    due_amount: Optional[str] = None
    due_date: Optional[str] = None

class CallResponse(BaseModel):
    call_id: str
    room_name: str
    status: str
    message: str
    mobile_number: str

class CampaignRequest(BaseModel):
    csv_path: str = Field(default="campaign.csv", description="Path to CSV file")
    max_concurrent: int = Field(default=5, ge=1, le=50)
    delay_between_calls: int = Field(default=2, ge=0, le=60)

class CampaignResponse(BaseModel):
    campaign_id: str
    total_numbers: int
    status: str
    message: str

class CallStatus(BaseModel):
    call_id: str
    mobile_number: str
    status: str
    room_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[int] = None
    disposition: Optional[str] = None
    transcript: Optional[str] = None

class WebhookPayload(BaseModel):
    event: str
    customer_name: Optional[str] = None
    mobile_number: Optional[str] = None
    disposition: Optional[str] = None
    ptp_date: Optional[str] = None
    concern_category: Optional[str] = None
    concern_notes: Optional[str] = None
    duration: Optional[int] = None
    timestamp: Optional[str] = None

# ── Helpers ──────────────────────────────────────────────────────────────────

async def initiate_outbound_call(
    mobile_number: str,
    customer_data: dict
) -> tuple[str, str, str]:
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    sip_trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID")

    if not all([url, api_key, api_secret]):
        raise ValueError("LiveKit credentials not configured")

    call_id = str(uuid.uuid4())
    room_name = f"outbound_{mobile_number.replace('+', '')}_{int(time.time())}"

    db_record = lookup_customer(mobile_number)
    ui_name = customer_data.get("customer_name")
    if ui_name in (None, "", "Customer"):
        ui_name = None
    metadata = {
        "name": ui_name or db_record.get("customer_name", "Customer"),
        "mobile_number": mobile_number,
        **{k: customer_data.get(k) or db_record.get(k, "") for k in [
            "policy_number", "plan_name", "due_amount", "due_date",
            "policy_purchase_date", "policy_term_years", "sum_assured",
            "premium_frequency", "last_payment_date", "payment_method",
            "email", "agent_name", "branch"
        ]}
    }
    metadata_str = json.dumps(metadata)
    logger.info(f"Call metadata: {json.dumps(metadata, indent=2)}")

    try:
        livekit_api = api.LiveKitAPI(url, api_key, api_secret)

        await livekit_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=mobile_number,
                room_name=room_name,
                participant_identity=mobile_number,
                participant_metadata=metadata_str,
            )
        )
        logger.info(f"SIP participant created for {mobile_number} in room {room_name}")

        try:
            await livekit_api.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="priya",
                    room=room_name,
                    metadata=metadata_str,
                )
            )
            logger.info(f"Agent 'priya' dispatched to room {room_name}")
        except Exception as e:
            logger.warning(f"Failed to dispatch agent: {e}")

        await livekit_api.aclose()
        logger.info(f"Call initiated: {call_id} to {mobile_number}")
        return call_id, room_name, "initiated"

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to initiate call to {mobile_number}: {error_msg}")
        if "sip trunk" in error_msg.lower() or "missing sip trunk" in error_msg.lower():
            return call_id, room_name, "sip_not_configured"
        raise

async def _process_campaign_row(row: dict, semaphore: asyncio.Semaphore, results: list):
    async with semaphore:
        try:
            call_id, room_name, status = await initiate_outbound_call(
                mobile_number=row.get("mobile_number", ""),
                customer_data=row,
            )
            CALLS_STORE[call_id] = {
                "call_id": call_id,
                "mobile_number": row.get("mobile_number", ""),
                "room_name": room_name,
                "status": status,
                "start_time": datetime.now().isoformat(),
                "customer_data": row,
            }
            results.append({"mobile_number": row.get("mobile_number"), "status": status})
            logger.info(f"Campaign call {call_id} -> {row.get('mobile_number')}: {status}")
        except Exception as e:
            logger.error(f"Campaign call failed for {row.get('mobile_number')}: {e}")
            results.append({"mobile_number": row.get("mobile_number"), "status": "failed", "error": str(e)})

# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "healthy",
        "service": "Renewal Voice Bot API",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/health")
async def health_check():
    required_vars = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "SARVAM_API_KEY"]
    missing_vars = [v for v in required_vars if not os.getenv(v)]
    return {
        "status": "healthy" if not missing_vars else "degraded",
        "missing_env_vars": missing_vars,
        "timestamp": datetime.now().isoformat(),
    }

@app.post("/api/call", response_model=CallResponse, dependencies=[Depends(verify_auth), Depends(rate_limit)])
async def create_call(request: CallRequest):
    try:
        call_id, room_name, status = await initiate_outbound_call(
            mobile_number=request.mobile_number,
            customer_data=request.model_dump(),
        )
        CALLS_STORE[call_id] = {
            "call_id": call_id,
            "mobile_number": request.mobile_number,
            "room_name": room_name,
            "status": status,
            "start_time": datetime.now().isoformat(),
            "customer_data": request.model_dump(),
        }
        return CallResponse(
            call_id=call_id,
            room_name=room_name,
            status=status,
            message="Call initiated successfully" if status == "initiated" else "SIP trunk not configured",
            mobile_number=request.mobile_number,
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")

@app.post("/api/campaign", response_model=CampaignResponse, dependencies=[Depends(verify_auth), Depends(rate_limit)])
async def start_campaign(request: CampaignRequest, background_tasks: BackgroundTasks):
    import csv
    try:
        with open(request.csv_path, mode="r") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
        if not rows:
            raise HTTPException(status_code=400, detail="CSV file is empty")

        campaign_id = str(uuid.uuid4())
        total_numbers = len(rows)

        logger.info(f"Queuing campaign {campaign_id} with {total_numbers} numbers (max {request.max_concurrent} concurrent)")
        background_tasks.add_task(_run_campaign, campaign_id, rows, request.max_concurrent, request.delay_between_calls)

        return CampaignResponse(
            campaign_id=campaign_id,
            total_numbers=total_numbers,
            status="queued",
            message=f"Campaign {campaign_id} queued. {total_numbers} numbers will be processed in the background.",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"CSV file not found: {request.csv_path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Campaign failed: {str(e)}")


async def _run_campaign(campaign_id: str, rows: list, max_concurrent: int, delay: int):
    """Run campaign in background task so HTTP request returns immediately."""
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []
    tasks = []
    for i, row in enumerate(rows):
        tasks.append(_process_campaign_row(row, semaphore, results))
        if i < len(rows) - 1:
            tasks.append(asyncio.sleep(delay))
    await asyncio.gather(*tasks)
    success_count = sum(1 for r in results if r["status"] == "initiated")
    logger.info(f"Campaign {campaign_id} complete: {success_count}/{len(rows)} initiated ({len(rows) - success_count} failed)")

@app.get("/api/calls", response_model=List[CallStatus], dependencies=[Depends(verify_auth)])
async def list_calls(limit: int = 50):
    calls = list(CALLS_STORE.values())[-limit:]
    return [
        CallStatus(
            call_id=call["call_id"],
            mobile_number=call["mobile_number"],
            status=call.get("status", "unknown"),
            room_name=call.get("room_name"),
            start_time=call.get("start_time"),
            end_time=call.get("end_time"),
            duration=call.get("duration"),
            disposition=call.get("disposition"),
            transcript=call.get("transcript"),
        )
        for call in calls
    ]

@app.get("/api/call/{call_id}", response_model=CallStatus, dependencies=[Depends(verify_auth)])
async def get_call_status(call_id: str):
    if call_id not in CALLS_STORE:
        raise HTTPException(status_code=404, detail="Call not found")
    call = CALLS_STORE[call_id]
    return CallStatus(
        call_id=call["call_id"],
        mobile_number=call["mobile_number"],
        status=call.get("status", "unknown"),
        room_name=call.get("room_name"),
        start_time=call.get("start_time"),
        end_time=call.get("end_time"),
        duration=call.get("duration"),
        disposition=call.get("disposition"),
        transcript=call.get("transcript"),
    )

@app.delete("/api/call/{call_id}", dependencies=[Depends(verify_auth)])
async def delete_call(call_id: str):
    if call_id not in CALLS_STORE:
        raise HTTPException(status_code=404, detail="Call not found")
    del CALLS_STORE[call_id]
    return {"message": "Call deleted successfully"}

@app.post("/api/webhook", dependencies=[Depends(verify_auth)])
async def receive_webhook(payload: WebhookPayload):
    """Receive call disposition updates from the agent."""
    logger.info(f"Webhook received: event={payload.event}, disposition={payload.disposition}")
    # Update in-memory store with disposition data
    return {"status": "ok", "message": "Webhook received"}

@app.get("/api/recording/{room_name}", dependencies=[Depends(verify_auth)])
async def get_recording_url(room_name: str):
    """Get the recording download URL for a room from LiveKit."""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    if not all([url, api_key, api_secret]):
        raise HTTPException(status_code=500, detail="LiveKit not configured")

    try:
        lk_api = api.LiveKitAPI(url, api_key, api_secret)
        egress = await lk_api.egress.list_egress(
            api.ListEgressRequest(room_name=room_name)
        )
        await lk_api.aclose()
        recordings = []
        for item in egress.items:
            for file in item.file_results:
                recordings.append({
                    "filename": file.filename,
                    "download_url": file.download_url,
                    "size": file.size,
                })
        if not recordings:
            return {"room_name": room_name, "recordings": [], "message": "No recordings found"}
        return {"room_name": room_name, "recordings": recordings}
    except Exception as e:
        logger.error(f"Failed to fetch recordings for {room_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch recordings: {str(e)}")

# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
