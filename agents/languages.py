import logging
import os

logger = logging.getLogger("renewal-bot")

LANGUAGE_CONFIG = {
    "en-IN": {
        "display_name": "English (India)",
        "tts_model": "bulbul:v3",
        "tts_speaker": "priya",
        "tts_language_code": "en-IN",
        "stt_language": "en",
        "greeting": "Hello, I'm Priya calling from Fairvalue Insuretech regarding your insurance policy renewal. Am I speaking with {name}?",
        "consent_prompt": "This call is being recorded for quality and training purposes. Is that okay?",
        "reprompt": "I didn't quite catch that. By when would you be able to make the payment?",
        "closing_messages": {
            "Promise to Pay": "Thank you for confirming. We are sending you the payment link shortly.",
            "Concern Captured": "Thank you for sharing your concern. Our team will review and get back to you with an appropriate resolution. Have a good day.",
            "Wrong Number": "I apologize for the inconvenience. Have a good day.",
            "Alternate Number Captured": "Thank you for providing the alternate number. We will try reaching them. Have a good day.",
            "Call Failed": "We will try reaching you again later. Thank you. Have a good day.",
            "No Response": "We were unable to reach you. We will try again later. Thank you.",
            "Consent Denied": "Thank you for your time. Have a good day.",
            "fallback": "Thank you for your time. Have a good day."
        }
    },
    "hi": {
        "display_name": "Hindi",
        "tts_model": "bulbul:v3",
        "tts_speaker": "priya",
        "tts_language_code": "hi-IN",
        "stt_language": "hi",
        "greeting": "नमस्ते, मैं Fairvalue Insuretech प्राइवेट लिमिटेड की तरफ से आपकी इंश्योरेंस पॉलिसी रिन्यूअल के बारे में बात कर रही हूँ। क्या मेरी बात {name} जी से हो रही है?",
        "consent_prompt": "यह कॉल गुणवत्ता और प्रशिक्षण के लिए रिकॉर्ड की जा रही है। क्या यह ठीक है?",
        "reprompt": "मैं समझ नहीं पाई। आप कब तक भुगतान कर सकते हैं?",
        "closing_messages": {
            "Promise to Pay": "पुष्टि करने के लिए धन्यवाद। हम जल्द ही आपको भुगतान लिंक भेज रहे हैं।",
            "Concern Captured": "अपनी चिंता साझा करने के लिए धन्यवाद। हमारी टीम इसकी समीक्षा करेगी और उचित समाधान के साथ आपसे संपर्क करेगी। आपका दिन शुभ हो।",
            "Wrong Number": "असुविधा के लिए मैं क्षमा चाहती हूँ। आपका दिन शुभ हो।",
            "Alternate Number Captured": "वैकल्पिक नंबर प्रदान करने के लिए धन्यवाद। हम उनसे संपर्क करने का प्रयास करेंगे। आपका दिन शुभ हो।",
            "Call Failed": "हम बाद में फिर से आपसे संपर्क करने का प्रयास करेंगे। धन्यवाद। आपका दिन शुभ हो।",
            "No Response": "हम आपसे संपर्क करने में असमर्थ रहे। हम बाद में पुनः प्रयास करेंगे। धन्यवाद।",
            "Consent Denied": "आपके समय के लिए धन्यवाद। आपका दिन शुभ हो।",
            "fallback": "आपके समय के लिए धन्यवाद। आपका दिन शुभ हो।"
        }
    }
}

def get_language_config(lang_code: str | None) -> dict:
    if not lang_code:
        lang_code = os.getenv("LANGUAGE", "en-IN")
    
    normalized = lang_code.strip()
    if normalized == "en":
        normalized = "en-IN"
        
    if normalized not in LANGUAGE_CONFIG:
        logger.warning(f"Unsupported language code '{lang_code}' requested. Falling back to 'en-IN'.")
        return LANGUAGE_CONFIG["en-IN"]
        
    return LANGUAGE_CONFIG[normalized]
