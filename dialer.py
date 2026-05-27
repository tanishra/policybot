import asyncio
import os
import csv
import json
import logging
from livekit import api
from dotenv import load_dotenv
import logger as db_logger

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dialer")

def load_dnd_list() -> set[str]:
    path = os.getenv("DND_LIST_PATH", "dnd_list.csv")
    dnd_set = set()
    if not os.path.exists(path):
        logger.warning(f"DND list file not found at {path}. Bypassing DND check.")
        return dnd_set
    try:
        with open(path, mode="r") as f:
            for line in f:
                val = line.strip()
                if val and not val.lower().startswith(("mobile", "phone")):
                    dnd_set.add(val)
        logger.info(f"Loaded {len(dnd_set)} DND numbers from {path}")
    except Exception as e:
        logger.error(f"Failed to load DND list: {e}")
    return dnd_set

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

MAX_RETRIES = 3
RETRY_DELAYS = [10, 30, 60]
NON_RETRYABLE_ERRORS = ["invalid", "not found", "unallocated", "disconnected"]

def is_retryable_error(error_msg: str) -> bool:
    error_lower = str(error_msg).lower()
    return not any(err in error_lower for err in NON_RETRYABLE_ERRORS)

async def dial_customer(livekit_api, customer, sip_trunk_id, dnd_set, semaphore):
    async with semaphore:
        mobile_number = customer.get('mobile_number', '')
        name = customer.get('customer_name', 'Unknown')

        if is_in_dnd(mobile_number, dnd_set):
            logger.info(f"Skipping {name} ({mobile_number}) - DND")
            await db_logger.log_call(
                customer_name=name, mobile_number=mobile_number,
                policy_number=customer.get('policy_number'), status="Failed",
                duration=0, disposition="DND Blocked"
            )
            return

        room_prefix = f"outbound_{mobile_number.replace('+', '')}_{int(asyncio.get_event_loop().time())}"
        metadata_str = json.dumps(customer)
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                await livekit_api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        sip_trunk_id=sip_trunk_id,
                        sip_call_to=mobile_number,
                        room_name=room_prefix,
                        participant_identity=mobile_number,
                        participant_metadata=metadata_str,
                    )
                )
                logger.info(f"Call initiated for {name} ({mobile_number})")
                return
            except Exception as e:
                last_error = e
                if not is_retryable_error(str(e)):
                    logger.warning(f"Non-retryable error for {name}: {e}")
                    break
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.info(f"Attempt {attempt + 1} failed for {name}, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)

        logger.error(f"Failed to connect {name} after {MAX_RETRIES} attempts: {last_error}")
        await db_logger.log_call(
            customer_name=name, mobile_number=mobile_number,
            policy_number=customer.get('policy_number'), status="Failed",
            duration=0, disposition=f"Call Failed ({last_error})"
        )

async def main():
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    sip_trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID")

    if not all([url, api_key, api_secret]):
        logger.error("LiveKit credentials missing in .env")
        return

    livekit_api = api.LiveKitAPI(url, api_key, api_secret)
    semaphore = asyncio.Semaphore(5)
    dnd_set = load_dnd_list()

    logger.info("Starting Campaign dialing...")
    with open('campaign.csv', mode='r') as file:
        reader = csv.DictReader(file)
        tasks = [dial_customer(livekit_api, row, sip_trunk_id, dnd_set, semaphore) for row in reader]

    await asyncio.gather(*tasks)
    logger.info("Campaign dispatch complete.")
    await livekit_api.aclose()

if __name__ == "__main__":
    asyncio.run(main())
