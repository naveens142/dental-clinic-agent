"""
AI Agent for ToothFairy Dental Clinic virtual receptionist.
Integrates LiveKit, Deepgram STT, OpenAI LLM/TTS with custom prompt management.
"""

import os
import logging
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, RoomInputOptions
from livekit.plugins import deepgram, openai, noise_cancellation, cartesia
from src.tools.appointments import get_availability, check_existing_appointments, book_appointment, cancel_appointment, reschedule_appointment, _session_id_context
from src.config.prompts import get_agent_instruction, get_session_instruction
from src.config import PromptConfig
from src.services.database import get_db
from src.models import initialize_memo

# Load environment and configure logging
load_dotenv(".env")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
server = AgentServer()

# Required environment variables
REQUIRED_ENV_VARS = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY",
    "DB_HOST", "DB_USER", "DB_NAME"
]

def validate_environment() -> None:
    """Validate required environment variables are present."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

def get_clinic_information() -> str:
    """Get clinic information from config file with fallback."""
    try:
        return get_session_instruction("clinic_config.json")
    except Exception as e:
        logger.error(f"Failed to load clinic config: {e}")
        config = PromptConfig()
        return f"""# {config.ORGANIZATION_NAME} - Service Temporarily Unavailable

I'm having trouble accessing clinic information. Please contact us directly.
For assistance: {config.FALLBACK_MESSAGE}

**Error:** {str(e)}
"""

def create_agent_session(config: PromptConfig) -> AgentSession:
    return AgentSession(
        stt = deepgram.STT(
            model="nova-3",      # better understanding & accuracy
            language="en",
            smart_format=True,
            interim_results=True
        ),
        llm=openai.LLM(model=config.LLM_MODEL,temperature=config.LLM_TEMPERATURE),
        tts=cartesia.TTS(api_key=os.getenv("CARTESIA_API_KEY"),voice=config.TTS_VOICE),
        tools=[get_availability, check_existing_appointments, book_appointment, cancel_appointment, reschedule_appointment]
    )


# ============================================================================
# AGENT SESSION HANDLER
# ============================================================================

@server.rtc_session()
async def my_agent(ctx: agents.JobContext):
    """Optimized agent session handler with database tracking."""
    db = None
    session_id = None
    user_id = None
    start_time = datetime.now()
    
    try:
        # Initialize database
        db = get_db()
        
        # Create session record in database
        session_id = db.create_session(ctx.room.name)
        logger.info(f"Created database session: {session_id}")
        
        validate_environment()
        
        # Parallel operations for faster startup
        config = PromptConfig()
        
        # Start room connection and instruction loading in parallel
        connect_task = ctx.connect()
        full_instruction = f"{get_agent_instruction()}\n\n{get_clinic_information()}"
        
        # Wait for connection to complete
        logger.info(f"Connecting to room: {ctx.room.name}")
        await connect_task
        
        logger.info(f"Agent configured as {config.RECEPTIONIST_NAME} for {config.ORGANIZATION_NAME}")
        
        # Create agent and session concurrently
        agent = Agent(instructions=full_instruction)
        session = create_agent_session(config)
        
        # Store session_id in context variable for tools to access
        _session_id_context.set(session_id)
        
        # Initialize conversation memo for this session        
        initialize_memo()
        
        logger.info(f"Set session context: {session_id}")
        
        # Start session WITH noise cancellation as requested
        logger.info("Starting optimized agent session...")
        await session.start(
            room=ctx.room, 
            agent=agent,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.NC()
            )
        )
        
        # Send greeting with shorter instruction
        greeting = f"Hello! I'm {config.RECEPTIONIST_NAME} from {config.ORGANIZATION_NAME}. How may I help you?"
        await session.generate_reply(instructions=f"Say: '{greeting}'")
        
        # Log agent greeting to conversation logs
        try:
            db.log_message(
                session_id=session_id,
                speaker='agent',
                message_text=greeting,
                user_id=None,
                audio_duration=None
            )
        except Exception as e:
            logger.warning(f"Error logging greeting to database: {e}")
        
        logger.info("Agent session started successfully")
        
    except (EnvironmentError, FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Configuration error: {e}")
        if session_id and db:
            db.end_session(session_id, duration_seconds=0)
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        if session_id and db:
            db.end_session(session_id, duration_seconds=0)
        raise
    finally:
        # End session and calculate duration
        if session_id and db:
            duration = int((datetime.now() - start_time).total_seconds())
            db.end_session(session_id, duration_seconds=duration)
            
            # Update daily analytics
            try:
                db.update_session_analytics()
            except Exception as e:
                logger.error(f"Error updating session analytics: {e}")
            
            logger.info(f"Session {session_id} ended. Duration: {duration} seconds")


if __name__ == "__main__":
    try:
        validate_environment()
        logger.info(f"Starting {PromptConfig().ORGANIZATION_NAME} AI Agent Server...")
        logger.info("Performance optimizations enabled (no caching)")
        agents.cli.run_app(server)
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        exit(1)