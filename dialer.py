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

DND_LIST = ["+910000000000"]
MAX_RETRIES = 3
RETRY_DELAYS = [10, 30, 60]
NON_RETRYABLE_ERRORS = ["invalid", "not found", "unallocated", "disconnected"]

def is_retryable_error(error_msg: str) -> bool:
    error_lower = str(error_msg).lower()
    return not any(err in error_lower for err in NON_RETRYABLE_ERRORS)

async def dial_customer(livekit_api, customer, sip_trunk_id, semaphore):
    async with semaphore:
        mobile_number = customer.get('mobile_number', '')
        name = customer.get('customer_name', 'Unknown')

        if mobile_number in DND_LIST:
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

    logger.info("Starting Campaign dialing...")
    with open('campaign.csv', mode='r') as file:
        reader = csv.DictReader(file)
        tasks = [dial_customer(livekit_api, row, sip_trunk_id, semaphore) for row in reader]

    await asyncio.gather(*tasks)
    logger.info("Campaign dispatch complete.")
    await livekit_api.aclose()

if __name__ == "__main__":
    asyncio.run(main())
