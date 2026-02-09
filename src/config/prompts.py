"""
AI Avatar Prompts Module

This module provides production-ready prompt templates for an AI receptionist agent.
Includes dynamic time handling, configurable settings, and proper error handling.

Usage:
    from src.config.prompts import get_agent_instruction, get_session_instruction
    
    system_prompt = get_agent_instruction()
    context_prompt = get_session_instruction()
"""

from datetime import datetime
from typing import Optional
import json

from .settings import PromptConfig, load_clinic_config, get_current_time, ZONEINFO_AVAILABLE

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def get_available_doctors(config_path: str = "clinic_config.json") -> list:
    """Get list of available doctors from clinic configuration."""
    try:
        clinic_config = load_clinic_config(config_path)
        doctors = []
        
        # Try to get doctors list if it exists
        if 'doctors' in clinic_config and isinstance(clinic_config['doctors'], list):
            doctors = clinic_config['doctors']
        # Fallback: check for single doctor in 'doctor' field
        elif 'doctor' in clinic_config:
            doctor_info = clinic_config['doctor']
            if isinstance(doctor_info, dict) and 'name' in doctor_info:
                doctors = [doctor_info]
            elif isinstance(doctor_info, list):
                doctors = doctor_info
        
        return doctors
    except Exception as e:
        # If config can't be loaded, assume single default doctor
        return [{
            'name': 'Dr. David Mishra DDS, MDS (Oral & Maxillofacial Surgery)',
            'specialization': 'Comprehensive dental care'
        }]


def should_ask_for_doctor() -> bool:
    """Check if we should ask for doctor preference (only if multiple doctors available)."""
    doctors = get_available_doctors()
    return len(doctors) > 1


def get_doctor_selection_options() -> str:
    """Get formatted list of doctors for user selection."""
    doctors = get_available_doctors()
    
    if len(doctors) == 1:
        # Single doctor - no selection needed
        return f"Doctor: {doctors[0].get('name', 'Dr. Available')}"
    
    # Multiple doctors - format selection options
    options = []
    for i, doctor in enumerate(doctors, 1):
        name = doctor.get('name', f'Doctor {i}')
        spec = doctor.get('specialization', '')
        if spec:
            options.append(f"- {name} ({spec})")
        else:
            options.append(f"- {name}")
    
    return "\n".join(options)


def get_default_doctor() -> str:
    """Get the default doctor name (for single doctor clinics)."""
    doctors = get_available_doctors()
    if doctors:
        return doctors[0].get('name', 'Dr. Available')
    return 'Dr. Available'


# ============================================================================
# AGENT SYSTEM INSTRUCTION
# ============================================================================

def get_agent_instruction() -> str:
    """
    System-level instruction defining agent persona, rules, and tool usage.
    """
    config = PromptConfig()
    
    # Get today and tomorrow dates (cached approach)
    try:
        if ZoneInfo:
            tz = ZoneInfo(config.TIMEZONE)
            now = datetime.now(tz)
        else:
            now = datetime.now()
        today = now.strftime("%A, %B %d, %Y")
        tomorrow = (now + __import__('datetime').timedelta(days=1)).strftime("%A, %B %d, %Y")
    except:
        today = "today"
        tomorrow = "tomorrow"

    return f"""
# CRITICAL CONTEXT: Current Dates
**Today: {today}**
**Tomorrow: {tomorrow}**

When user says "tomorrow", use {tomorrow}.
When user says "today", use {today}.
When user says next Monday/Tuesday, calculate FROM today.

IMPORTANT:
- For ANY clinic-related question (doctor, services, fees, address, timings, policies),
  USE ONLY information from SESSION_INSTRUCTION.
- Do NOT use outside knowledge, assumptions, or internet memory.
- If information is missing, reply exactly:
  "{config.FALLBACK_MESSAGE}"

# Persona
You are a professional {config.ORGANIZATION_TYPE} receptionist named {config.RECEPTIONIST_NAME},
working for {config.ORGANIZATION_NAME}.

# Context
You are a real-time virtual receptionist assisting patients via voice.

# Core Responsibilities
1. Answer clinic-related questions using SESSION_INSTRUCTION only
2. Assist with appointment availability, booking, and cancellation
3. Collect and confirm patient details clearly
4. Escalate medical or complex policy questions to human staff

# Appointment Handling Rules (CRITICAL)
- You DO NOT decide availability yourself
- You MUST use system tools to:
  • Check availability
  • Book appointments
  • Cancel appointments
- Never promise or guarantee a slot without tool confirmation
- NEVER book an appointment without first checking availability using get_availability tool

# DATA FRESHNESS RULES (CRITICAL - READ CAREFULLY)
- ALWAYS call get_availability for EVERY availability question - NEVER reuse old results
- The CalCom API is the ONLY source of truth for availability
- Database is ONLY for storing bookings - NEVER query database to check availability
- OLD function call results in conversation history are INVALID - always make fresh API calls
- If a patient asks about availability twice, call get_availability twice - results may have changed
- IGNORE any availability information from earlier in the conversation - always check fresh

# Booking Workflow (MANDATORY - FOLLOW THIS ORDER)
- ALWAYS ask about doctor preference (if multiple available) and appointment reason BEFORE checking availability
- If only 1 doctor exists in clinic, skip doctor selection and go directly to asking appointment reason
- ALWAYS ask appointment reason/service type - this is mandatory context for every booking
- NEVER skip availability check (get_availability) - even if the user requests a specific time
- NEVER call book_appointment for a time that wasn't verified to be in the available slots
- ALWAYS collect doctor, reason, phone, email, and name BEFORE calling book_appointment

# Tool Usage Rules
- Checking existing appointments → require patient email
- Asking for available slots → call get_availability
- Booking an appointment → FIRST call get_availability, show available slots, THEN collect name and call book_appointment
- Cancelling an appointment → require patient email, optionally name/phone
- Rescheduling an appointment → require patient email and new appointment time

# Required Patient Details
- Booking requires: full name, date, time, phone number, email address
- Cancellation requires: patient email (mandatory), optionally name/phone
- Rescheduling requires: patient email (mandatory), current and new appointment times
- CRITICAL: If information was given earlier in conversation, DO NOT ask again - retrieve from memo

# Communication Guidelines
- Speak warmly, conversationally, and with genuine care
- Be genuinely friendly and approachable, like chatting with a trusted friend
- Use a professional yet warm, conversational tone
- All dates and times are in {config.TIMEZONE} ({config.TIMEZONE_ABBR})

# Date & Time Rules
- All dates/times reference: TODAY is {today}, TOMORROW is {tomorrow}
- Interpret dates without a year as the nearest future date
- Always restate confirmed appointment time with timezone

# Safety & Compliance Commitment
- Protect patient privacy - never share personal health information
- Provide accurate information only
- Stay in your professional scope - medical questions go to the dentist team
"""


# ============================================================================
# SESSION INSTRUCTION (CLINIC DATA)
# ============================================================================

def get_session_instruction(config_path: str = "clinic_config.json") -> str:
    """
    Session-specific clinic information (single source of truth).
    """
    try:
        clinic_config = load_clinic_config(config_path)
        config = PromptConfig()

        doctor = clinic_config.get("doctor", {})
        clinic = clinic_config.get("clinic", {})
        services = clinic_config.get("services", [])
        fees = clinic_config.get("fees", {})
        policies = clinic_config.get("policies", {})

        services_text = "\n".join([f"- {service}" for service in services])

        fees_text = "\n".join([
            f"- General Consultation: {fees.get('general_consultation', 'Contact clinic')}",
            f"- Follow-up Visit: {fees.get('followup', 'Contact clinic')}",
            f"- Emergency Consultation: {fees.get('emergency', 'Contact clinic')}",
            f"- Surgical Consultation: {fees.get('surgical_consultation', 'Contact clinic')}"
        ])

        contact_text = "\n".join([
            f"- Phone: {clinic.get('phone', 'Contact clinic')}",
            f"- Emergency: {clinic.get('emergency', 'Contact clinic')}",
            f"- Email: {clinic.get('email', 'Contact clinic')}",
            f"- Website: {clinic.get('website', 'Contact clinic')}"
        ])

        policies_text = "\n".join([
            "**Appointment Policies:**",
            f"- {policies.get('appointment_info', 'Contact clinic for details')}",
            f"- {policies.get('arrival_time', 'Please arrive on time')}",
            f"- {policies.get('cancellation', 'Contact clinic for cancellation policy')}",
            "",
            "**Payment Methods:**",
            f"- {policies.get('payment_methods', 'Contact clinic')}",
            f"- {policies.get('insurance', 'Contact clinic')}",
            "",
            "**What to Bring:**",
            f"- {policies.get('what_to_bring', 'Contact clinic')}"
        ])

        return f"""
# {config.ORGANIZATION_NAME} - Information Database

## Dentist Information
**Name:** {doctor.get('name', 'Contact clinic')}
**Specialization:** {doctor.get('specialization', 'Contact clinic')}

## Services Offered
{services_text}

## Consultation Fees
{fees_text}

## Clinic Location
{clinic.get('address', 'Contact clinic')}

## Operating Hours
{clinic.get('hours', 'Contact clinic')}

## Contact Information
{contact_text}

## Additional Information
{policies_text}

---
IMPORTANT:
- This is the ONLY source of clinic truth
- If information is missing, use fallback response
- Do NOT infer or guess

**Last Updated:** {get_current_time()}
**Timezone:** {config.TIMEZONE} ({config.TIMEZONE_ABBR})
"""

    except Exception as e:
        config = PromptConfig()
        return f"""
# {config.ORGANIZATION_NAME} - Information Database

Configuration error: {str(e)}

Please contact clinic directly:
{config.FALLBACK_MESSAGE}

---
Last Updated: {get_current_time()}
"""
