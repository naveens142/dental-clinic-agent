"""
AI Agent for ToothFairy Dental Clinic virtual receptionist.
LiveKit Cloud compatible (livekit-agents >= 1.2.0)
"""

import os
import sys
import json
import logging
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livekit.agents import WorkerOptions, cli, JobContext
from livekit.agents.voice import Agent, AgentSession, room_io
from livekit.plugins import deepgram, openai, noise_cancellation


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

# ---------------------------------------------------------------------------
# AGENT ENTRYPOINT (NEW LIVEKIT MODEL)
# ---------------------------------------------------------------------------

# AGENT ENTRYPOINT (NEW LIVEKIT MODEL)
# ---------------------------------------------------------------------------

async def dental_clinic_agent(ctx: JobContext):
    """
    LiveKit Cloud entrypoint.
    One function = one agent worker.
    """
    validate_environment()

    db = None
    session_id = None
    start_time = datetime.utcnow()

    try:
        logger.info("Connecting to room...")
        # ctx is already connected in JobContext

        db = get_db()
        session_id = db.create_session(ctx.room.name)
        _session_id_context.set(session_id)

        logger.info(f"DB session created: {session_id}")

        initialize_memo()

        config = PromptConfig()

        full_instruction = (
            f"{get_agent_instruction()}\n\n{get_clinic_information()}"
        )

        agent = Agent(
            instructions=full_instruction,
            stt=deepgram.STT(
                model="nova-3",
                language="en",
                smart_format=True,
                interim_results=True,
            ),
            llm=openai.LLM(
                model=config.LLM_MODEL,
                temperature=config.LLM_TEMPERATURE,
            ),
            tts=openai.TTS(
                voice=config.TTS_VOICE,
            ),
            tools=[
                get_availability,
                check_existing_appointments,
                book_appointment,
                cancel_appointment,
                reschedule_appointment,
            ],
        )

        # Create agent session and connect to context
        session = AgentSession(
            stt=deepgram.STT(
                model="nova-3",
                language="en",
                smart_format=True,
                interim_results=True,
            ),
            llm=openai.LLM(
                model=config.LLM_MODEL,
                temperature=config.LLM_TEMPERATURE,
            ),
            tts=openai.TTS(
                voice=config.TTS_VOICE,
            ),
        )
        
        session.update_agent(agent)
        run_result = await session.start(
            agent,
            room=ctx.room,
            room_input_options=room_io.RoomInputOptions(
                noise_cancellation=noise_cancellation.NC(),
                close_on_disconnect=False,
            ),
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

        # Wait for the session to complete
        if run_result:
            await run_result

    except Exception as e:
        logger.error("Agent crashed", exc_info=True)
        raise

    finally:
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

# ---------------------------------------------------------------------------
# MAIN (REQUIRED FOR LIVEKIT CLOUD)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        validate_environment()
        logger.info("Starting ToothFairy Dental Clinic Agent")
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=dental_clinic_agent
            )
        )
    except Exception:
        logger.critical("Agent failed to start", exc_info=True)
        raise
