"""
Configuration and Settings Module
Centralized configuration for the dental clinic agent
"""

from datetime import datetime
from typing import Optional
import os
import json

try:
    from zoneinfo import ZoneInfo
    ZONEINFO_AVAILABLE = True
except ImportError:
    ZONEINFO_AVAILABLE = False


class PromptConfig:
    """Configuration class for prompt customization."""

    # Receptionist Details
    RECEPTIONIST_NAME: str = os.getenv("RECEPTIONIST_NAME", "Neha")
    ORGANIZATION_NAME: str = os.getenv("ORGANIZATION_NAME", "ToothFairy Dental Clinic")
    ORGANIZATION_TYPE: str = os.getenv("ORGANIZATION_TYPE", "dental clinic")
    
    # Wake Words for Voice Activation
    WAKE_WORDS: list = ["neha", "hey neha", "hello neha", "neha please"]

    # Timezone Configuration
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")
    TIMEZONE_ABBR: str = os.getenv("TIMEZONE_ABBR", "IST")

    # Language Settings
    PRIMARY_LANGUAGE: str = os.getenv("PRIMARY_LANGUAGE", "English")

    # Model Configuration
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.5"))
    TTS_PROVIDER: str = os.getenv("TTS_PROVIDER", "cartesia")
    TTS_MODEL: str = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
    TTS_VOICE: str = os.getenv("TTS_VOICE", "694f9389-aac1-45b6-b726-9d9369183238")

    # Fallback Response
    FALLBACK_MESSAGE: str = os.getenv(
        "FALLBACK_MESSAGE",
        "I don't have that information right now, but I can connect you with our dental team who will assist further."
    )


# Deepgram STT Configuration
DEEPGRAM_CONFIG = {
    "model": "nova-3",
    "language": "en",
    "smart_format": True,
    "interim_results": True
}

# Database Connection Pool Configuration
DB_POOL_CONFIG = {
    "pool_size": 5,
    "pool_reset_session": True,
    "autocommit": False,
    "use_unicode": True,
    "charset": "utf8mb4"
}

# livgit Agent Configuration
LIVEKIT_AGENT_CONFIG = {
    "noise_cancellation_enabled": True,
    "room_input_options": {
        "echo_cancellation": True,
        "noise_suppression": True
    }
}


def load_clinic_config(config_path: str = "clinic_config.json") -> dict:
    """Load clinic configuration from JSON file."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Clinic configuration file not found: {config_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in clinic configuration: {e}")


def get_current_time(timezone_name: Optional[str] = None) -> str:
    """Get current formatted time for the specified timezone."""
    try:
        tz_name = timezone_name or PromptConfig.TIMEZONE

        if ZONEINFO_AVAILABLE:
            try:
                tz = ZoneInfo(tz_name)
                current_time = datetime.now(tz)
                return current_time.strftime("%A, %B %d, %Y at %I:%M %p %Z")
            except Exception:
                tz = ZoneInfo("UTC")
                current_time = datetime.now(tz)
                return current_time.strftime("%A, %B %d, %Y at %I:%M %p UTC")
        else:
            from datetime import timezone as dt_timezone
            current_time = datetime.now(dt_timezone.utc)
            return current_time.strftime("%A, %B %d, %Y at %I:%M %p UTC")

    except Exception:
        current_time = datetime.now()
        return f"{current_time.strftime('%A, %B %d, %Y at %I:%M %p')} (Local Time)"
