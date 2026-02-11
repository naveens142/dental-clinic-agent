"""
AI Agent for ToothFairy Dental Clinic virtual receptionist.
LiveKit Cloud compatible (livekit-agents >= 1.2.0)
"""

import os
import sys
import json
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from livekit.agents import WorkerOptions, cli, JobContext
from livekit.agents.voice import Agent, AgentSession, room_io
from livekit.agents.llm import ChatMessage
from livekit.plugins import deepgram, openai


from src.tools.appointments import (
    get_availability,
    check_existing_appointments,
    book_appointment,
    cancel_appointment,
    reschedule_appointment,
    _session_id_context,
)
from src.config.prompts import get_agent_instruction, get_session_instruction
from src.config import PromptConfig
from src.services.database import get_db
from src.models import initialize_memo, clear_memo

# ---------------------------------------------------------------------------
# ENV & LOGGING
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("dental-agent")

REQUIRED_ENV_VARS = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "DB_HOST",
    "DB_USER",
    "DB_NAME",
    "DB_PASSWORD",
    "CALCOM_API_KEY",
    "CALCOM_EVENT_TYPE_ID"
]

def validate_environment() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

# ---------------------------------------------------------------------------
# CLINIC INFO
# ---------------------------------------------------------------------------

def get_clinic_information() -> str:
    try:
        return get_session_instruction("clinic_config.json")
    except Exception as e:
        logger.error("Failed to load clinic config", exc_info=True)
        cfg = PromptConfig()
        return (
            f"# {cfg.ORGANIZATION_NAME}\n\n"
            "Clinic information is temporarily unavailable.\n\n"
            f"Please contact us directly.\n{cfg.FALLBACK_MESSAGE}"
        )


# AGENT ENTRYPOINT (NEW LIVEKIT MODEL)
# ---------------------------------------------------------------------------

async def dental_clinic_agent(ctx: JobContext):
    """
    LiveKit Cloud entrypoint.
    One function = one agent worker.
    """
    validate_environment()

    db = None
    session = None
    session_id = None
    start_time = datetime.utcnow()

    try:
        logger.info("Connecting to room...")
        await ctx.connect()

        # Wait for the first participant to join
        participant = await ctx.wait_for_participant()
        logger.info(f"Connected to participant: {participant.identity}")

        db = get_db()
        session_id = db.create_session(ctx.room.name)
        _session_id_context.set(session_id)

        logger.info(f"DB session created: {session_id}")

        initialize_memo()

        config = PromptConfig()

        full_instruction = (
            f"{get_agent_instruction()}\n\n{get_clinic_information()}"
        )

        # Select TTS provider (only OpenAI supported)
        tts_plugin = openai.TTS(voice=config.TTS_VOICE)

        # Create the Agent instance with all its components
        agent = Agent(
            instructions=full_instruction,
            stt=deepgram.STT(
                model="nova-2",
                language="en",
                smart_format=True,
                interim_results=True,
            ),
            llm=openai.LLM(
                model=config.LLM_MODEL,
                temperature=config.LLM_TEMPERATURE,
            ),
            tts=tts_plugin,
            tools=[
                get_availability,
                check_existing_appointments,
                book_appointment,
                cancel_appointment,
                reschedule_appointment,
            ]
        )

        # Initialize the session and start it for this participant
        session = AgentSession()
        await session.start(
            agent,
            room=ctx.room,
            room_input_options=room_io.RoomInputOptions(
                participant_identity=participant.identity
            )
        )


        greeting = (
            f"Hello! I'm {config.RECEPTIONIST_NAME} from "
            f"{config.ORGANIZATION_NAME}. How may I help you?"
        )

        await session.say(greeting)

        try:
            db.log_message(
                session_id=session_id,
                speaker="agent",
                message_text=greeting,
                user_id=None,
                audio_duration=None,
            )
        except Exception:
            logger.warning("Failed to log greeting", exc_info=True)

        logger.info("Agent started successfully")

        # Keep the session alive until the room disconnects or the job is cancelled
        logger.info("Agent session running...")
        while ctx.room.isconnected:
            await asyncio.sleep(1)

    except Exception as e:
        logger.error("Agent crashed", exc_info=True)
        raise

    finally:
        # Proper cleanup: close session and disconnect room
        if session:
            try:
                await session.aclose()
                logger.info("Agent session closed")
            except Exception as e:
                logger.warning(f"Error closing agent session: {e}")

        # Clear conversation memo to prevent memory leaks
        try:
            clear_memo()
            logger.info(f"Conversation memo cleared for session {session_id if session_id else 'unknown'}")
        except Exception as e:
            logger.warning(f"Failed to clear memo: {e}")
        
        if db and session_id:
            duration = int((datetime.utcnow() - start_time).total_seconds())
            try:
                db.end_session(session_id, duration_seconds=duration)
                logger.info(f"Session {session_id} ended (duration: {duration}s)")
            except Exception as e:
                logger.error(f"Failed to end DB session {session_id}: {e}")
            
            # Try to update analytics separately with additional error handling
            try:
                db.update_session_analytics()
                logger.debug("Session analytics updated successfully")
            except Exception as e:
                # Don't let analytics failures crash the agent
                logger.warning(f"Failed to update session analytics: {e}")

        elif session_id:
            logger.info(f"Session {session_id} ended (no DB connection for cleanup)")
        
        # Final shutdown call to ensure room disconnection
        try:
            ctx.shutdown()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# MAIN (REQUIRED FOR LIVEKIT CLOUD)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        validate_environment()
        logger.info("Starting ToothFairy Dental Clinic Agent")
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=dental_clinic_agent,
                agent_name=os.getenv("LIVEKIT_AGENT_NAME", "toothfairy-dental-agent")
            )
        )
    except Exception:
        logger.critical("Agent failed to start", exc_info=True)
        raise

