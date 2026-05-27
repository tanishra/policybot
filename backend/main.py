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
import aiohttp
import ssl
import certifi
from dotenv import load_dotenv
from livekit import api
import logging
import sqlite3

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

# macOS Python 3.13 fix: use certifi's CA bundle for SSL verification
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
_LK_SESSION: aiohttp.ClientSession | None = None


async def _get_lk_session() -> aiohttp.ClientSession:
    global _LK_SESSION
    if _LK_SESSION is None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        _LK_SESSION = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
    return _LK_SESSION

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

_DND_SET: set[str] = set()
_DND_LAST_LOADED: float = 0.0

def load_dnd_list() -> set[str]:
    global _DND_SET, _DND_LAST_LOADED
    path = os.getenv("DND_LIST_PATH", "dnd_list.csv")
    if not os.path.exists(path):
        # Fallback to parent directory relative to backend main.py
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", path))
    if not os.path.exists(path):
        if _DND_LAST_LOADED == 0.0:
            logger.warning(f"DND list file not found at {path}. Bypassing DND check.")
            _DND_LAST_LOADED = 1.0
        return _DND_SET
    try:
        mtime = os.path.getmtime(path)
        if mtime > _DND_LAST_LOADED:
            new_set = set()
            with open(path, mode="r") as f:
                for line in f:
                    val = line.strip()
                    if val and not val.lower().startswith(("mobile", "phone")):
                        new_set.add(val)
            _DND_SET = new_set
            _DND_LAST_LOADED = mtime
            logger.info(f"Loaded {len(_DND_SET)} DND numbers from {path}")
    except Exception as e:
        logger.error(f"Failed to load DND list: {e}")
    return _DND_SET

def is_in_dnd(mobile_number: str, dnd_set: set[str]) -> bool:
    normalized = "".join(c for c in mobile_number if c.isdigit())
    if len(normalized) < 10:
        return False
    last_10 = normalized[-10:]
    for dnd_num in dnd_set:
        dnd_norm = "".join(c for c in dnd_num if c.isdigit())
        if len(dnd_norm) >= 10 and dnd_norm[-10:] == last_10:
            return True
    return False

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

    # Perform DND check (Phase 8)
    dnd_set = load_dnd_list()
    if is_in_dnd(mobile_number, dnd_set):
        logger.info(f"Skipping outbound call to {metadata['name']} ({mobile_number}) - DND Blocked")
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dispositions.db"))
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                INSERT INTO call_logs (timestamp, customer_name, mobile_number, policy_number, call_status, duration, disposition)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                metadata["name"],
                mobile_number,
                metadata.get("policy_number"),
                "Failed",
                0,
                "DND Blocked"
            ))
            conn.commit()
            conn.close()
            logger.info(f"[LOGGER] Logged DND Blocked call: {metadata['name']} -> DND Blocked")
        except Exception as db_e:
            logger.error(f"Failed to log DND Blocked outcome: {db_e}")
        return call_id, room_name, "DND Blocked"

    try:
        lk_session = await _get_lk_session()
        livekit_api = api.LiveKitAPI(url, api_key, api_secret, session=lk_session)

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

        # ── Agent Dispatch with retry ────────────────────────────────────
        # Dispatch may fail if agent hasn't registered yet. Retry with backoff.
        max_dispatch_retries = 5
        dispatch_delay = 2.0
        dispatched = False
        for attempt in range(1, max_dispatch_retries + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(dispatch_delay)
                    dispatch_delay = min(dispatch_delay * 1.5, 10.0)
                await livekit_api.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(
                        agent_name="priya",
                        room=room_name,
                        metadata=metadata_str,
                    )
                )
                logger.info(f"Agent 'priya' dispatched to room {room_name}")
                dispatched = True
                break
            except Exception as e:
                logger.warning(f"Dispatch attempt {attempt}/{max_dispatch_retries} failed: {e}")

        if not dispatched:
            logger.error(f"Failed to dispatch agent after {max_dispatch_retries} attempts")

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

class SuccessMetrics(BaseModel):
    total_dialed: int
    connect_rate: float
    rpc_success_rate: float
    ptp_capture_rate: float
    avg_call_duration: float
    compliance_violations: int
    period: str

@app.get("/api/metrics", response_model=SuccessMetrics, dependencies=[Depends(verify_auth)])
async def get_metrics():
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dispositions.db"))
    if not os.path.exists(db_path):
        # Database does not exist yet (no calls dialed)
        return SuccessMetrics(
            total_dialed=0,
            connect_rate=0.0,
            rpc_success_rate=0.0,
            ptp_capture_rate=0.0,
            avg_call_duration=0.0,
            compliance_violations=0,
            period="No data"
        )
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='call_logs'")
        if not cursor.fetchone():
            conn.close()
            return SuccessMetrics(
                total_dialed=0,
                connect_rate=0.0,
                rpc_success_rate=0.0,
                ptp_capture_rate=0.0,
                avg_call_duration=0.0,
                compliance_violations=0,
                period="No data"
            )
            
        cursor.execute("SELECT timestamp, duration, disposition, agent_trace FROM call_logs")
        rows = cursor.fetchall()
        conn.close()
        
        total_dialed = len(rows)
        if total_dialed == 0:
            return SuccessMetrics(
                total_dialed=0,
                connect_rate=0.0,
                rpc_success_rate=0.0,
                ptp_capture_rate=0.0,
                avg_call_duration=0.0,
                compliance_violations=0,
                period="No data"
            )
            
        connected_count = 0
        rpc_success_count = 0
        ptp_count = 0
        total_duration = 0
        compliance_violations = 0
        timestamps = []
        
        rpc_success_dispositions = {
            "Promise to Pay", 
            "Concern Captured", 
            "Partial Payment Arranged", 
            "Call Back Scheduled", 
            "Escalated", 
            "Consent Denied"
        }
        
        for row in rows:
            ts = row["timestamp"]
            dur = row["duration"] or 0
            disp = row["disposition"] or ""
            trace_json = row["agent_trace"] or ""
            
            if ts:
                timestamps.append(ts)
                
            if dur > 0:
                connected_count += 1
                total_duration += dur
                
                if disp in rpc_success_dispositions:
                    rpc_success_count += 1
                else:
                    if trace_json:
                        try:
                            traces = json.loads(trace_json)
                            if any(isinstance(t, dict) and t.get("agent_name") == "rpc" for t in traces):
                                rpc_success_count += 1
                        except Exception:
                            pass
                            
                if disp == "Promise to Pay":
                    ptp_count += 1
                    
            if trace_json:
                try:
                    traces = json.loads(trace_json)
                    for t in traces:
                        if isinstance(t, dict) and t.get("agent_name") == "compliance":
                            compliance_violations += 1
                except Exception:
                    pass
                    
        connect_rate = connected_count / total_dialed
        rpc_success_rate = rpc_success_count / connected_count if connected_count > 0 else 0.0
        ptp_capture_rate = ptp_count / rpc_success_count if rpc_success_count > 0 else 0.0
        avg_call_duration = total_duration / connected_count if connected_count > 0 else 0.0
        
        if timestamps:
            try:
                min_ts = min(timestamps).split("T")[0]
                max_ts = max(timestamps).split("T")[0]
                period = f"{min_ts} to {max_ts}"
            except Exception:
                period = "Unknown"
        else:
            period = "Unknown"
            
        return SuccessMetrics(
            total_dialed=total_dialed,
            connect_rate=round(connect_rate, 4),
            rpc_success_rate=round(rpc_success_rate, 4),
            ptp_capture_rate=round(ptp_capture_rate, 4),
            avg_call_duration=round(avg_call_duration, 2),
            compliance_violations=compliance_violations,
            period=period
        )
    except Exception as e:
        logger.error(f"Error compiling metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Error compiling metrics: {str(e)}")

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
        lk_session = await _get_lk_session()
        lk_api = api.LiveKitAPI(url, api_key, api_secret, session=lk_session)
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
