"""
Agent Tools Module

Defines all function tools for the dental clinic agent including:
- Appointment availability checking
- Appointment booking with validation
- Appointment cancellation
- Appointment rescheduling
"""

import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from contextvars import ContextVar
from livekit.agents.llm import function_tool
from src.services.calcom import (
    get_availability as _get_availability,
    book_appointment as _book_appointment,
    cancel_appointment as _cancel_appointment,
    find_booking_by_patient_info as _find_booking_by_patient_info
)
from src.services.database import get_db
from src.models import get_memo, get_memo_context_for_prompt, clear_memo
from src.config.prompts import get_available_doctors, should_ask_for_doctor, get_doctor_selection_options, get_default_doctor

logger = logging.getLogger(__name__)

# Context variable to store session_id across async calls
_session_id_context: ContextVar[Optional[str]] = ContextVar('session_id', default=None)

# Context variable to store the last booked appointment details for quick cancel/reschedule
_last_booking_context: ContextVar[Optional[dict]] = ContextVar('last_booking', default=None)


def add_memo_context_to_response(base_response: str) -> str:
    """
    Enhance tool response with current memo context to remind the LLM of stored information.
    This helps the agent remember patient details across the conversation.
    """
    try:
        memo_context = get_memo_context_for_prompt()
        if memo_context:
            # Add memo context as a system reminder
            return f"{base_response}\n\n[SYSTEM MEMO: {memo_context}]"
        return base_response
    except Exception as e:
        logger.warning(f"Failed to get memo context: {e}")
        return base_response

def parse_booking_time(user_input: str, reference_date: str = None, timezone_name: str = "Asia/Kolkata") -> str:
    """
    Parse user time input and convert to proper ISO format for booking.
    
    Args:
        user_input: User's time input (e.g., "10:30", "2:30 PM", etc.)
        reference_date: Reference date in ISO format (defaults to tomorrow)
        timezone_name: Target timezone
    
    Returns:
        ISO formatted datetime string
    """
    try:
        # If no reference date provided, use tomorrow
        if not reference_date:
            now = datetime.now(ZoneInfo(timezone_name))
            tomorrow = now + timedelta(days=1)
            reference_date = tomorrow.strftime("%Y-%m-%d")
        else:
            # Extract date part if full ISO string is provided
            if "T" in reference_date:
                reference_date = reference_date.split("T")[0]
        # Parse time input and create full datetime
        # This will handle various time formats
        base_dt = datetime.strptime(f"{reference_date} {user_input}", "%Y-%m-%d %H:%M")
        # Set timezone
        tz_dt = base_dt.replace(tzinfo=ZoneInfo(timezone_name))
        return tz_dt.isoformat()
    except Exception as e:
        logger.error(f"Error parsing booking time '{user_input}': {e}")
        # Return the original input if parsing fails
        return user_input


# ============================================================================
# APPOINTMENT TOOLS
# ============================================================================

@function_tool(
    description="Get FRESH available appointment slots from CalCom API for a specific date range. CRITICAL: This makes a LIVE API call to CalCom - do NOT reuse old results from conversation history. ALWAYS call this function even if you asked about the same date before. Returns REAL-TIME available time slots for booking dental appointments."
)
async def get_availability(
    start: str,
    end: str,
    timezone_name: str = "Asia/Kolkata",
    duration_minutes: int = 30
):
    """
    Get available appointment slots.
    
    Args:
        start: Start datetime in ISO format (e.g., "2026-02-06T00:00:00+00:00")
        end: End datetime in ISO format (e.g., "2026-02-06T23:59:59+00:00")
        timezone_name: Timezone for the slots (default: "Asia/Calcutta")
        duration_minutes: Duration of each slot in minutes (default: 30)
    """
    logger.info(f"[GET_AVAILABILITY] Called with: start={start}, end={end}, timezone={timezone_name}, duration={duration_minutes}")
    
    # Quick weekend check without date iteration
    try:
        tz = ZoneInfo(timezone_name)
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(tz)
        # Fast check: only check if the range is just 1-2 days and both are weekends
        date_diff = (end_dt - start_dt).days
        if date_diff <= 2:
            start_weekday = start_dt.isoweekday()
            if start_weekday in [6, 7]:  # Saturday or Sunday
                start_day = start_dt.strftime("%A, %B %d")
                return add_memo_context_to_response(f"Our dental clinic is closed on weekends ({start_day}). We're open Monday through Friday, 10:00 AM to 2:00 PM (Asia/Kolkata timezone). Would you like me to check availability for a weekday instead?")
    except Exception as e:
        logger.warning(f"Could not check weekend status: {e}")
    
    logger.info(f"[GET_AVAILABILITY] Calling CalCom API for range: {start} to {end}")
    result = _get_availability(start, end, timezone_name, duration_minutes)
    logger.info(f"[GET_AVAILABILITY] CalCom response: {result}")
    
    # If no slots returned, provide helpful message with explanation
    if isinstance(result, dict) and result.get("status") == "success":
        if not result.get("data") or all(len(slots) == 0 for slots in result.get("data", {}).values()):
            try:
                tz = ZoneInfo(timezone_name)
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
                start_day = start_dt.strftime("%A, %B %d")
                return f"Unfortunately, there are no available appointment slots on {start_day}. This is likely because all time slots are already booked for that day. Our clinic operates Monday through Friday, 10:00 AM to 2:00 PM ({timezone_name}). Would you like to try a different date?"
            except:
                return f"Unfortunately, there are no available appointment slots for the requested dates. Our clinic operates Monday through Friday, 10:00 AM to 2:00 PM ({timezone_name}). Would you like to try a different date?"
    
    return result


@function_tool(
    description="Look up patient's existing booked appointments. CRITICAL: If patient provided email earlier in conversation, call this function WITHOUT the email parameter - it will auto-retrieve from memory. Only ask for email if this is the first time looking up appointments. Returns a list of confirmed upcoming appointments from CalCom."
)
async def check_existing_appointments(
    patient_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None
):
    """
    Check existing ACTIVE appointments for a patient using CalCom as source of truth.
    
    ✓ EMAIL IS MANDATORY for secure lookup (server-side filtering via CalCom API)
    ✓ MEMO-ENABLED: Reuses stored email to avoid asking twice
    
    Args:
        email: Patient's email address (REQUIRED - enables server-side CalCom API filtering)
        patient_name: Patient's full name (optional - smart word-matching for family members)
        phone: Patient's phone number (optional - normalized matching for family members)
    
    Returns:
        ACTIVE appointment details or message
    """
    memo = get_memo()
    
    # If no email provided, check memo first
    if not email:
        email = memo.get_patient_email()
    
    # ★ EMAIL IS MANDATORY ★
    if not email:
        return add_memo_context_to_response("I need your email address to look up your appointments securely. Could you please provide your email address?")
    
    # Store email in memo for future use
    memo.update_patient_email(email)
    
    # Also store phone if provided
    if phone:
        memo.update_patient_phone(phone)
    
    try:
        # Use CalCom as ONLY source of truth - search for ACTIVE appointments
        logger.info(f"[CHECK_APPOINTMENTS] Searching CalCom for ACTIVE appointments: email={email}, name={patient_name}, phone={phone}")
        
        from src.services.calcom import find_all_bookings_by_patient_info
        from datetime import datetime
        from zoneinfo import ZoneInfo
        
        # CRITICAL: Use new function that returns ALL matching appointments, not just one
        bookings = find_all_bookings_by_patient_info(
            patient_email=email,
            patient_name=patient_name,
            patient_phone=phone,
            search_days=90
        )
        
        if not bookings:
            msg = f"I couldn't find any ACTIVE appointments for email: {email}"
            if patient_name:
                msg += f" and name: {patient_name}"
            if phone:
                msg += f" and phone: {phone}"
            msg += ". Would you like to book a new appointment?"
            memo.set_appointments([])  # Store empty list
            return add_memo_context_to_response(msg)
        
        logger.info(f"[CHECK_APPOINTMENTS] Found {len(bookings)} appointment(s) for {email}")
        
        # Format ALL appointments
        try:
            appointment_list = []
            
            for booking in bookings:
                appt_time = datetime.fromisoformat(booking.get('start', '').replace('Z', '+00:00'))
                formatted_time = appt_time.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%I:%M %p on %B %d, %Y").lstrip('0')
                status = booking.get('status', 'Confirmed')
                booking_id = booking.get('uid', 'N/A')
                attendee_name = booking.get('attendees', [{}])[0].get('name', 'Patient')
                
                # Extract doctor info from event title or description
                event_title = booking.get('title', '')
                doctor_name = booking.get('user', {}).get('name', 'Dr. Available') if isinstance(booking.get('user'), dict) else 'Dr. Available'
                
                # Try to extract service type from title or description
                service_type = None
                description = booking.get('description', '')
                title = booking.get('title', '')
                
                # Check if service type is in title or description
                for service in ['Cleaning', 'Checkup', 'Root Canal', 'Whitening', 'Emergency', 'Cosmetic']:
                    if service.lower() in title.lower() or service.lower() in description.lower():
                        service_type = service
                        break
                
                if not service_type:
                    service_type = 'General Appointment'
                
                appointment_list.append({
                    'name': attendee_name,
                    'time': formatted_time,
                    'status': status,
                    'id': booking_id,
                    'uid': booking_id,
                    'start': booking.get('start', ''),
                    'email': email,
                    'phone': booking.get('attendees', [{}])[0].get('phone', ''),
                    'doctor': doctor_name,
                    'service': service_type
                })
            
            # Store ALL appointments in memo and context
            memo.set_appointments(appointment_list)
            
            if appointment_list:
                first_appt = appointment_list[0]
                
                # Extract and store phone from first appointment if available
                if first_appt.get('phone'):
                    memo.update_patient_phone(first_appt['phone'])
                    logger.info(f"[CHECK_APPOINTMENTS] ✓ Stored phone from appointment in memo")
                
                # Extract and store doctor from first appointment
                if first_appt.get('doctor'):
                    memo.set_preferred_doctor(first_appt['doctor'])
                    logger.info(f"[CHECK_APPOINTMENTS] ✓ Stored doctor from appointment in memo: {first_appt['doctor']}")
                
                # Extract and store service type from first appointment
                if first_appt.get('service'):
                    memo.set_appointment_reason(first_appt['service'])
                    logger.info(f"[CHECK_APPOINTMENTS] ✓ Stored appointment reason from appointment in memo: {first_appt['service']}")
                
                # Also store name if not already provided
                if first_appt.get('name'):
                    memo.update_patient_name(first_appt['name'])
                
                appointment_context = {
                    'booking_id': first_appt['id'],
                    'name': first_appt['name'],
                    'email': email,
                    'phone': first_appt['phone'],
                    'appointment_time': first_appt['start'],
                    'formatted_time': first_appt['time'],
                    'status': first_appt['status'],
                    'all_appointments': appointment_list
                }
                _last_booking_context.set(appointment_context)
                logger.info(f"[CHECK_APPOINTMENTS] Stored {len(appointment_list)} appointment(s) in context and memo")
            
            # Build response message
            if len(appointment_list) == 1:
                appt = appointment_list[0]
                msg = f"✓ I found your ACTIVE appointment:\n{appt['name']}: {appt['time']}\nStatus: {appt['status']}\nID: {appt['id']}\n\nWould you like to reschedule or cancel it?"
            else:
                # Multiple appointments - list them all
                msg = f"✓ I found {len(appointment_list)} ACTIVE appointment(s) for you:\n\n"
                for i, appt in enumerate(appointment_list, 1):
                    msg += f"{i}. {appt['name']}: {appt['time']} (ID: {appt['id']})\n"
                msg += f"\nWould you like to reschedule or cancel any of these appointments?"
            
            return add_memo_context_to_response(msg)
            
        except Exception as e:
            logger.warning(f"[CHECK_APPOINTMENTS] Error formatting appointment: {e}")
            return add_memo_context_to_response(f"I found your appointment (ID: {booking.get('uid', 'N/A')}) at {booking.get('start', 'Unknown')}. Would you like to reschedule or cancel it?")
            
    except Exception as e:
        logger.error(f"[CHECK_APPOINTMENTS] Error checking appointments: {e}", exc_info=True)
        return add_memo_context_to_response(f"I'm having trouble looking up your appointments at the moment. Could you please provide your email or phone number?")


@function_tool(
    description="Book a dental appointment for a patient. CRITICAL: You MUST collect patient name, phone number, email, preferred doctor, and appointment reason BEFORE calling this function. The start_time parameter MUST be the EXACT ISO timestamp string from get_availability results (e.g., '2026-02-10T13:30:00+05:30'). DO NOT construct your own timestamp - ALWAYS copy the exact 'start' value from the availability slot the patient chose. Parameters: name (required), start_time (required - EXACT ISO timestamp from get_availability), phone (required), email (required), doctor (required - doctor name or 'Any Available'), reason (required - appointment reason/service type), timezone_name (optional), duration_minutes (optional)."
)
async def book_appointment(
    name: str,
    start_time: str,
    phone: str,
    email: str,
    doctor: str = "Any Available",
    reason: str = "General Appointment",
    timezone_name: str = "Asia/Kolkata",
    duration_minutes: int = 30
):
    """
    Book an appointment using the EXACT start_time from availability results.
    
    ✓ MEMO-ENABLED: Stores booking details in memo for quick reschedule/cancel
    
    Args:
        name: Patient's FULL name (required)
        start_time: EXACT ISO timestamp from get_availability (required) - e.g., "2026-02-10T13:30:00+05:30"
        phone: Patient's phone number (required for booking confirmation)
        email: Patient's email address (required for booking confirmation)
        doctor: Preferred doctor name or "Any Available" (required - provides context)
        reason: Appointment reason/service type (required - e.g., "General Checkup", "Root Canal", "Cleaning")
        timezone_name: Timezone for the appointment (default: "Asia/Kolkata")
        duration_minutes: Duration in minutes (default: 30)
    """
    memo = get_memo()
    memo.set_action("book")
    
    # Store doctor and reason in memo
    if doctor and doctor.strip():
        memo.set_preferred_doctor(doctor)
    if reason and reason.strip():
        memo.set_appointment_reason(reason)
    
    # Validate required fields
    logger.info(f"[BOOKING] Received call with name={name}, phone={phone}, email={email}, doctor={doctor}, reason={reason}, start_time={start_time}")
    
    if not name or not name.strip():
        logger.warning("[BOOKING] Validation failed: name is empty")
        return add_memo_context_to_response("I need your full name to book the appointment. Could you please provide your name?")
    
    if not phone or not phone.strip():
        logger.warning(f"[BOOKING] Validation failed: phone is empty or None (phone={repr(phone)})")
        return add_memo_context_to_response("I need your phone number to confirm the booking. Could you please provide your phone number?")
    
    if not email or not email.strip():
        logger.warning(f"[BOOKING] Validation failed: email is empty or None (email={repr(email)})")
        return add_memo_context_to_response("I need your email address to confirm the booking. Could you please provide your email address?")
    
    # Store in memo for future use
    memo.update_patient_name(name)
    memo.update_patient_phone(phone)
    memo.update_patient_email(email)
    
    logger.info(f"[BOOKING] ✓ Validation passed. Attempting to book appointment for {name} (doctor: {doctor}, reason: {reason}, phone: {phone}, email: {email}) at {start_time}")
    
    # ★ NATURAL INTERIM RESPONSE ★
    # Send an acknowledgment message that shows the agent is processing
    try:
        confirm_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        slot_time = confirm_dt.strftime("%I:%M %p").lstrip('0')
        interim_msg = f"Got it! Let me book that for you on {slot_time}..."
        logger.info(f"[BOOKING] Sending interim response: {interim_msg}")
    except:
        interim_msg = "Got it! Let me book that for you..."
    
    # Proceed with booking directly using the EXACT time from availability
    result = _book_appointment(name, email, start_time, timezone_name, duration_minutes)
    
    logger.info(f"Booking result: {result}")
    
    # Check if booking failed 
    if isinstance(result, dict) and result.get("status") == "error":
        error_msg = result.get("error", "Unknown error")
        logger.warning(f"Booking failed: {error_msg}")
        
        # Check if it's an availability issue
        if any(keyword in error_msg.lower() for keyword in ["not available", "booked", "conflict", "unavailable"]):
            # Re-check availability to show current slots
            try:
                requested_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                day_start = requested_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = requested_dt.replace(hour=23, minute=59, second=59, microsecond=0)
                
                availability = _get_availability(
                    day_start.isoformat(),
                    day_end.isoformat(),
                    timezone_name,
                    duration_minutes
                )
                
                if availability.get("status") == "success" and availability.get("data"):
                    # Show all available slots
                    remaining_slots = []
                    for date_key, day_slots in availability.get("data", {}).items():
                        for slot in day_slots:
                            slot_time = datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
                            time_str = slot_time.strftime("%I:%M %p").lstrip('0')
                            remaining_slots.append(time_str)
                    
                    if remaining_slots:
                        slots_text = "\n- ".join(remaining_slots)
                        return add_memo_context_to_response(f"I'm having trouble with that time slot at the moment. But here are all the available times for that day - you're welcome to try one of these:\n\n- {slots_text}\n\nWhich time works best for you?")
                    else:
                        return add_memo_context_to_response("That time doesn't seem to be available right now. No problem though - I'd be happy to check availability for another day that works for you. What other dates would you like me to look at?")
            except Exception as e:
                logger.error(f"Error re-checking availability: {e}")
            
            return add_memo_context_to_response(f"That time slot just became unavailable, but that's okay! Let me find some other great times for you on that day.")
        
        return add_memo_context_to_response(f"I'm having difficulty completing that booking at the moment. But don't worry - I'd love to help you find another appointment time that works just as well. Would you like me to suggest some alternatives?")
    
    # Success case - format the confirmation nicely
    logger.info(f"[BOOKING] Booking response: {result}")
    if isinstance(result, dict) and "data" in result:
        logger.info("[BOOKING] ✓ Got successful booking response from Cal.com")
        booking_data = result["data"]
        booking_id = booking_data.get("uid", "N/A")
        logger.info(f"[BOOKING] booking_id from Cal.com: {booking_id}")
        
        # Store booking in memo
        memo.set_current_appointment({
            'id': booking_id,
            'name': name,
            'email': email,
            'phone': phone,
            'time': start_time
        })
        
        # Store booking in database
        try:
            logger.info("[BOOKING] Starting database storage...")
            db = get_db()
            session_id = _session_id_context.get()  # Get from context variable
            user_id = None
            
            logger.info(f"[BOOKING] session_id from context: {session_id}")
            
            # CRITICAL: session_id must not be None
            if not session_id:
                logger.warning("[BOOKING] ⚠️  session_id is None in context, attempting fallback...")
                # Fallback: Get the most recent session from database (any status)
                try:
                    conn = db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT session_id FROM sessions 
                        ORDER BY start_time DESC 
                        LIMIT 1
                    """)
                    result = cursor.fetchone()
                    cursor.close()
                    conn.close()
                    
                    if result:
                        session_id = result[0]
                        logger.info(f"[BOOKING] ✓ Recovered session_id from database: {session_id}")
                    else:
                        logger.error("[BOOKING] ✗✗✗ FATAL: No sessions found in database")
                        return add_memo_context_to_response(f"I'm experiencing a technical issue with the booking system. Please start a new session and try again.")
                except Exception as e:
                    logger.error(f"[BOOKING] ✗✗✗ Error recovering session_id: {e}")
                    return add_memo_context_to_response(f"I'm experiencing a technical issue with the booking system. Please try again later.")
            
            logger.info(f"[BOOKING] Creating/retrieving user for phone={phone}, email={email}")
            
            # Create user with real phone/email (now required, not optional)
            user_id = db.get_or_create_user(phone=phone, name=name, email=email)
            logger.info(f"[BOOKING] ✓ User created/retrieved: user_id={user_id}")
            
            # Link any past conversation logs to this user
            if user_id and session_id:
                db.link_conversation_logs_to_user(session_id, user_id)
                logger.info(f"[BOOKING] ✓ Linked conversation logs to user {user_id}")
            
            if user_id:
                logger.info(f"[BOOKING] Parsing appointment times from {start_time} with duration {duration_minutes} min")
                # Parse appointment times
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_dt = start_dt + timedelta(minutes=duration_minutes)
                
                logger.info(f"[BOOKING] Creating booking record: session_id={session_id}, user_id={user_id}, start={start_dt}, end={end_dt}, calcom_uid={booking_id}")
                # Create booking record with Cal.com UID
                created_booking_id = db.create_booking(
                    session_id=session_id,
                    user_id=user_id,
                    appointment_start=start_dt,
                    appointment_end=end_dt,
                    service_type="General Appointment",
                    notes=f"Booked via AI agent. Phone: {phone}, Email: {email}",
                    calcom_uid=booking_id
                )
                logger.info(f"[BOOKING] ✓✓✓ BOOKING STORED SUCCESSFULLY: {created_booking_id}")
                
                # Log booking to booking_history table for audit trail
                logger.info(f"[BOOKING] Adding to booking history...")
                db.log_booking_to_history(created_booking_id, appointment_start_time=start_dt)
                logger.info(f"[BOOKING] ✓ Booking history logged")
                
                # NEW: Upsert user contact and mark sync
                try:
                    db.upsert_user_contact(name=name, phone=phone, email=email, calcom_uid=booking_id)
                    db.mark_calcom_sync(created_booking_id, booking_id, sync_status="synced")
                    logger.info(f"[BOOKING] ✓ Synced user contact and marked booking as synced with CalCom")
                except Exception as e:
                    logger.error(f"[BOOKING] Warning: Could not sync user contact or mark sync: {e}")
            else:
                logger.error("[BOOKING] ✗✗✗ Could not store booking: user_id is None")
        except Exception as e:
            logger.error(f"[BOOKING] ✗✗✗ Error storing booking in database: {e}", exc_info=True)
        
        logger.info(f"[BOOKING] Booking process complete for {name}")
        
        # Store booking details in context for potential quick cancel/reschedule
        booking_details = {
            "booking_id": booking_id,
            "name": name,
            "phone": phone,
            "email": email,
            "appointment_time": start_time,
            "duration_minutes": duration_minutes,
            "timezone": timezone_name
        }
        _last_booking_context.set(booking_details)
        logger.info(f"[BOOKING] Stored booking details in context for quick cancellation/rescheduling")
        
        # Format the confirmation time nicely
        try:
            confirm_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            formatted_time = confirm_dt.strftime("%I:%M %p on %B %d, %Y").lstrip('0')
            
            # Build confirmation message with doctor and reason
            msg = f"Perfect! Your appointment is all set for {formatted_time}."
            if doctor and doctor.strip() and doctor != "Any Available":
                msg += f" You'll be with {doctor}."
            if reason and reason.strip() and reason != "General Appointment":
                msg += f" Service: {reason}."
            msg += f" Your booking ID is {booking_id} — save this for any changes later. See you soon!"
            
            return msg
        except:
            return add_memo_context_to_response(f"Perfect! Your appointment has been successfully booked. Your booking ID is {booking_id} — save this for any changes later. See you soon!")
    else:
        logger.error(f"[BOOKING] ✗✗✗ Booking failed or returned unexpected format: {result}")
    
    logger.info(f"Successfully booked appointment for {name}")
    return result


@function_tool(
    description="Cancel an existing appointment. CRITICAL: If patient provided email earlier, call WITHOUT patient_email - auto-retrieves from memory. For family bookings, provide patient_name (e.g., 'Rakesh Jain') and appointment_time auto-retrieves from stored appointments. Simple call: cancel_appointment(patient_name='Mohit Sharma')"
)
async def cancel_appointment(
    patient_name: Optional[str] = None,
    patient_email: Optional[str] = None,
    appointment_time: Optional[str] = None,
    phone: Optional[str] = None,
    cancellation_reason: str = "Cancelled by patient",
    booking_uid: Optional[str] = None
):
    """
    Cancel an appointment using secure email-based lookup via CalCom.
    
    ✓ EMAIL IS MANDATORY for security (server-side CalCom API filtering)
    
    FLOW:
    1. Email-first lookup (server-side CalCom API filtering)
    2. Filter by name and phone (smart Python-side filtering) - optional, for family member cancellations
    3. Match time if provided
    
    Args:
        patient_email: Patient's email (REQUIRED - enables secure server-side CalCom API filtering)
        appointment_time: Appointment date and time (REQUIRED - helps identify specific booking)
        patient_name: Patient's full name (optional - for family member lookup)
        phone: Patient's phone number (optional - for family member lookup)
        cancellation_reason: Reason for cancellation
        booking_uid: Direct booking UID (alternative if available)
    """
    # ★ EMAIL IS MANDATORY ★
    # First check if email provided, if not try memo
    memo = get_memo()
    booking_to_cancel = None
    stored_email = memo.get_patient_email()
    
    if not patient_email:
        patient_email = stored_email
    
    # Also check context from last booking
    if not patient_email:
        last_booking = _last_booking_context.get()
        if last_booking and last_booking.get('email'):
            patient_email = last_booking.get('email')
            logger.info(f"[CANCEL] Retrieved email from last_booking context: {patient_email}")
    
    if not patient_email:
        return add_memo_context_to_response("I need your email address to cancel your appointment securely. Could you please provide your email address?")
    
    logger.info(f"[CANCEL] Using email for cancellation: {patient_email} (stored={stored_email is not None})")
    
    # Store email in memo for future operations
    memo.update_patient_email(patient_email)
    
    # Check if user is referring to the appointment we just booked or found
    last_booking = _last_booking_context.get()
    logger.info(f"[CANCEL] Reusing email from earlier appointment lookup: {patient_email}")
    
    # CRITICAL: If patient_name is provided but appointment_time is not, find it from all_appointments
    if patient_name and not appointment_time and last_booking and last_booking.get('all_appointments'):
        for appt in last_booking['all_appointments']:
            if appt.get('name', '').lower() == patient_name.lower():
                appointment_time = appt.get('start')  # ISO format
                logger.info(f"[CANCEL] Found appointment time for {patient_name} from context: {appointment_time}")
                break
    
    if not patient_name and not appointment_time and not booking_uid and last_booking:
        # User just wants to cancel the appointment we just booked
        booking_uid = last_booking.get('booking_id')
        patient_name = last_booking.get('name')
        appointment_time = last_booking.get('appointment_time')
        booking_to_cancel = last_booking
        logger.info(f"[CANCEL] Using recently booked appointment for cancellation: {booking_uid}")
        # Proceed directly to cancellation - user already confirmed by saying "cancel"
    
    # Try to find booking in CalCom by email (primary), then filter by name and phone
    if patient_email and appointment_time:
        try:
            logger.info(f"[CANCEL] Looking up ACTIVE booking in CalCom for {patient_email} (with optional name/phone filters)")
            
            # Use CalCom as ONLY source of truth
            # Email-first with optional name and phone filters
            booking_to_cancel = _find_booking_by_patient_info(
                patient_email=patient_email,
                patient_name=patient_name,
                patient_phone=phone,
                appointment_time=appointment_time
            )
            
            if booking_to_cancel:
                booking_uid = booking_to_cancel.get('uid')  # Use CalCom UID
                logger.info(f"[CANCEL] ✓ Found ACTIVE booking {booking_uid} in CalCom for {patient_email}")
                # Proceed directly to cancellation - booking is confirmed
            else:
                return add_memo_context_to_response(f"I couldn't find an ACTIVE appointment for {patient_email} (name: {patient_name or 'any'}, phone: {phone or 'any'}) in our system. Could you please double-check? Or if you have your booking ID from the confirmation email, I can use that instead.")
        except Exception as e:
            logger.error(f"[CANCEL] Error looking up booking in CalCom: {e}")
            return add_memo_context_to_response("I'm having a small technical issue looking up your appointment. Could you provide your booking ID instead? You can find it in your confirmation email.")
    
    # If we have a booking UID (either from lookup or direct), cancel it
    if booking_uid and booking_uid.strip():
        # ★ NATURAL INTERIM RESPONSE ★
        logger.info(f"[CANCEL] ✓ Found appointment. Now processing cancellation for booking {booking_uid}")
        
        result = _cancel_appointment(booking_uid, cancellation_reason)
        
        if isinstance(result, dict) and result.get("status") == "error":
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower() or "invalid" in error_msg.lower():
                return add_memo_context_to_response(f"I'm having trouble finding that booking. Could you double-check the email and date, or provide your booking ID from the confirmation email?")
            return add_memo_context_to_response("I'd love to help cancel your appointment, but I'm having a small technical issue. Our team at the clinic can definitely help - would you like me to connect you with them?")
        
        # Update database status
        try:
            db = get_db()
            db.update_booking_status(booking_uid, 'cancelled', cancellation_reason)
            logger.info(f"[CANCEL] Updated booking {booking_uid} status to cancelled in database")
            
            # Mark sync as cancelled
            try:
                db.mark_calcom_sync(booking_uid, booking_uid, sync_status="cancelled")
                logger.info(f"[CANCEL] ✓ Marked booking {booking_uid} sync status as cancelled")
            except Exception as e:
                logger.error(f"[CANCEL] Warning: Could not mark sync status: {e}")
        except Exception as e:
            logger.error(f"[CANCEL] Error updating booking status in database: {e}")
        
        # Provide confirmation with details if we have them
        if booking_to_cancel:
            appt_time = booking_to_cancel.get('appointment_time', '')
            patient = booking_to_cancel.get('patient_name', patient_name or 'Your appointment')
            return add_memo_context_to_response(f"Perfect! Your appointment (ID: {booking_uid}) on {appt_time} has been successfully cancelled. You'll receive a confirmation message shortly. Is there anything else I can help you with today?")
        
        return add_memo_context_to_response(f"Perfect! Your appointment (ID: {booking_uid}) has been successfully cancelled. You'll receive a confirmation message shortly. Is there anything else I can help you with today?")
    else:
        # No valid booking information provided
        return add_memo_context_to_response("""I'd be happy to help you cancel your appointment! I can do this in a couple of ways:

1. **Tell me your name and appointment time** - I'll look up your appointment and cancel it
2. **If you have your booking ID** from your confirmation email, you can provide that

Which option works better for you?""")


@function_tool(
    description="Reschedule an existing appointment to a new time. CRITICAL: Auto-detects recent bookings from memory - for immediate rescheduling after booking, just call reschedule_appointment(new_start_time='ISO_timestamp'). For specific patients, provide patient_name. The new_start_time MUST be exact ISO from get_availability results. Simple calls: reschedule_appointment(new_start_time='2026-02-11T13:00:00+05:30') OR reschedule_appointment(patient_name='John Doe', new_start_time='ISO_timestamp')"
)
async def reschedule_appointment(
    patient_name: Optional[str] = None,
    current_appointment_time: Optional[str] = None,
    new_start_time: Optional[str] = None,
    patient_email: Optional[str] = None,
    phone: Optional[str] = None,
    timezone_name: str = "Asia/Kolkata",
    duration_minutes: int = 30
):
    """
    Reschedule an appointment by looking it up by email and current time, or using a recent booking.
    
    ✓ EMAIL IS MANDATORY for security (server-side CalCom API filtering)
    ✓ MEMO-ENABLED: Reuses email from earlier lookup to avoid asking twice
    
    Args:
        patient_email: Patient's email address (auto-retrieves from memo if not provided)
        current_appointment_time: Current appointment date and time (optional - uses recent booking if not provided) 
        new_start_time: New appointment start time in ISO format (REQUIRED)
        patient_name: Patient's full name (optional - for family member lookup)
        phone: Patient's phone number (optional - for family member lookup)
        timezone_name: Timezone for the appointment (default: "Asia/Kolkata")
        duration_minutes: Duration in minutes (default: 30)
    """
    memo = get_memo()
    memo.set_action("reschedule")
    
    if not new_start_time:
        return add_memo_context_to_response("I'd be happy to help you reschedule! What date and time would you like to move your appointment to?")
    
    # CRITICAL: Validate ISO timestamp format - must have timezone
    try:
        test_dt = datetime.fromisoformat(new_start_time.replace('Z', '+00:00'))
        logger.info(f"[RESCHEDULE] ✓ Valid timestamp format: {new_start_time}")
    except Exception as e:
        logger.error(f"[RESCHEDULE] ✗ CRITICAL: Invalid timestamp {new_start_time}: {e}")
        return add_memo_context_to_response(f"The time format seems incorrect. Please use one of the times from the available slots list. For example: use '12:00 PM' from the list, not a different format.")
    
    # ★ AUTO-RETRIEVE EMAIL FROM MEMO ★
    if not patient_email:
        patient_email = memo.get_patient_email()
        logger.info(f"[RESCHEDULE] Auto-retrieved email from memo: {patient_email}")
    
    # ★ AUTO-RETRIEVE PHONE FROM MEMO ★
    if not phone:
        phone = memo.get_patient_phone()
        logger.info(f"[RESCHEDULE] Auto-retrieved phone from memo: {phone}")
    
    # ★ AUTO-RETRIEVE PATIENT NAME FROM MEMO ★
    if not patient_name:
        patient_name = memo.get_patient_name()
        logger.info(f"[RESCHEDULE] Auto-retrieved name from memo: {patient_name}")
    
    # ★ REUSE DOCTOR & REASON FROM ORIGINAL APPOINTMENT ★
    doctor = memo.get_preferred_doctor()
    reason = memo.get_appointment_reason()
    
    if not doctor:
        doctor = "Any Available"
    if not reason:
        reason = "General Appointment"
    
    logger.info(f"[RESCHEDULE] Using doctor={doctor} and reason={reason} from original appointment (memo)")
    
    # Initialize variables
    booking_uid = None
    booking_to_reschedule = None
    
    # ★ PRIORITY 1: Check if user wants to reschedule the RECENT booking ★
    last_booking = _last_booking_context.get()
    logger.info(f"[RESCHEDULE] Last booking context: {last_booking}")
    
    # If NO specific patient details provided, use the most recent booking
    if ((not patient_name or not current_appointment_time) and last_booking and 
        last_booking.get('booking_id') and last_booking.get('email')):
        
        booking_uid = last_booking.get('booking_id')
        patient_name = last_booking.get('name') or patient_name
        patient_email = last_booking.get('email') or patient_email
        phone = last_booking.get('phone') or phone
        current_appointment_time = last_booking.get('appointment_time')
        booking_to_reschedule = last_booking
        
        # Update memo with retrieved info
        memo.update_patient_email(patient_email)
        memo.update_patient_name(patient_name)
        if phone:
            memo.update_patient_phone(phone)
        
        logger.info(f"[RESCHEDULE] ✓ Using recent booking: {booking_uid} for {patient_name} ({patient_email})")
        
    # ★ PRIORITY 2: Look up in CalCom if we have email + time ★
    elif patient_email and current_appointment_time:
        try:
            logger.info(f"[RESCHEDULE] Looking up ACTIVE booking in CalCom for {patient_email}")
            
            booking_to_reschedule = _find_booking_by_patient_info(
                patient_email=patient_email,
                patient_name=patient_name,
                patient_phone=phone,
                appointment_time=current_appointment_time
            )
            
            if booking_to_reschedule:
                booking_uid = booking_to_reschedule.get('uid')
                logger.info(f"[RESCHEDULE] ✓ Found ACTIVE booking {booking_uid} in CalCom for {patient_email}")
            else:
                return add_memo_context_to_response(f"I couldn't find an active appointment for {patient_email}. Could you double-check the details?")
                
        except Exception as e:
            logger.error(f"[RESCHEDULE] Error looking up booking: {e}")
            return add_memo_context_to_response("I'm having trouble looking up your appointment. Could you try again?")
    
    # ★ PRIORITY 3: Need more info ★
    else:
        if not patient_email:
            return add_memo_context_to_response("I need your email address to find and reschedule your appointment. Could you provide it?")
        return add_memo_context_to_response("I need to find your current appointment first. Could you provide your appointment details?")
    
    # ★ PROCEED WITH RESCHEDULING IF WE FOUND THE BOOKING ★
    if not booking_uid:
        return add_memo_context_to_response("I couldn't locate your current appointment. Could you provide more details?")
    
    # Use the booking_uid as CalCom UID (from our CalCom lookup)
    calcom_uid = booking_uid
    
    logger.info(f"[RESCHEDULE] Proceeding with reschedule. CalCom UID: {calcom_uid}")
    
    # ★ NATURAL INTERIM RESPONSE ★
    try:
        new_dt = datetime.fromisoformat(new_start_time.replace("Z", "+00:00"))
        new_slot_time = new_dt.strftime("%I:%M %p").lstrip('0')
        interim_reschedule_msg = f"Got it! Let me reschedule your appointment to {new_slot_time}..."
        logger.info(f"[RESCHEDULE] Interim response: {interim_reschedule_msg}")
    except:
        interim_reschedule_msg = "Got it! Let me reschedule that for you..."
    
    # Proceed with rescheduling if we have all required info
    if not booking_uid:
        return add_memo_context_to_response("I need to find your current appointment first. Could you please provide your name and current appointment time?")
    
    # Use the booking_uid as CalCom UID (from our CalCom lookup)
    calcom_uid = booking_uid
    
    logger.info(f"[RESCHEDULE] Proceeding with reschedule. CalCom UID: {calcom_uid}")
    
    # ★ NATURAL INTERIM RESPONSE ★
    try:
        new_dt = datetime.fromisoformat(new_start_time.replace("Z", "+00:00"))
        new_slot_time = new_dt.strftime("%I:%M %p").lstrip('0')
        interim_reschedule_msg = f"Got it! Let me reschedule your appointment to {new_slot_time}..."
        logger.info(f"[RESCHEDULE] Interim response: {interim_reschedule_msg}")
    except:
        interim_reschedule_msg = "Got it! Let me reschedule that for you..."
    
    try:
        # First, cancel the existing appointment using Cal.com UID
        logger.info(f"[RESCHEDULE] Cancelling old booking {booking_uid} (calcom_uid={calcom_uid})")
        cancel_result = _cancel_appointment(calcom_uid, "Rescheduled to new time")
        
        # ★ CRITICAL: Proper error checking for cancellation response ★
        # Check for error status OR missing data OR error key
        is_cancel_error = (
            isinstance(cancel_result, dict) and (
                cancel_result.get("status") == "error" or
                cancel_result.get("status") == "DRY_RUN" or
                "error" in cancel_result or
                (not cancel_result.get("data") and cancel_result.get("status") != "cancelled")
            )
        )
        
        if is_cancel_error:
            logger.error(f"[RESCHEDULE] Failed to cancel booking: {cancel_result}")
            return add_memo_context_to_response(f"I found your appointment (ID: {booking_uid}), but I'm having difficulty cancelling it for rescheduling. Would you like me to connect you with our team?")
        
        logger.info(f"[RESCHEDULE] ✓ Cancelled old booking {booking_uid}")
        
        # Then, book the new appointment
        logger.info(f"[RESCHEDULE] Booking new appointment at {new_start_time}")
        
        # CRITICAL: Detect if using old appointment time (causes duplicates)
        if booking_to_reschedule:
            old_time = booking_to_reschedule.get('start')
            if old_time:
                old_time_obj = datetime.fromisoformat(old_time.replace('Z', '+00:00'))
                new_time_obj = datetime.fromisoformat(new_start_time.replace('Z', '+00:00'))
                old_hour = old_time_obj.strftime('%H:%M')
                new_hour = new_time_obj.strftime('%H:%M')
                if old_hour == new_hour:
                    logger.error(f"[RESCHEDULE] ✗✗✗ DUPLICATE ALERT: Using old time {old_hour}! Old: {old_time}, New: {new_start_time}")
                    return add_memo_context_to_response(f"I notice you're choosing the same time (:{new_hour}) as your current appointment. This causes duplicates. Please choose a different time from the available slots.")
                logger.info(f"[RESCHEDULE] ✓ Time changed: {old_hour} → {new_hour}")
        
        book_result = _book_appointment(patient_name, patient_email, new_start_time, timezone_name, duration_minutes)
        
        if isinstance(book_result, dict) and book_result.get("status") == "error":
            error_msg = book_result.get("error", "Unknown error")
            logger.error(f"[RESCHEDULE] Failed to book new time: {error_msg}")
            return add_memo_context_to_response(f"I was able to cancel your old appointment (ID: {booking_uid}), but the new time slot is no longer available. Would you like to choose another time?")
        
        logger.info(f"[RESCHEDULE] ✓ Successfully booked new appointment")
        
        # Extract new booking ID from result
        new_booking_id = None
        if isinstance(book_result, dict) and book_result.get("data"):
            new_booking_id = book_result.get("data", {}).get("uid")
        
        # ★ CRITICAL: Update database properly for the reschedule ★
        try:
            db = get_db()
            new_start_dt = datetime.fromisoformat(new_start_time.replace("Z", "+00:00"))
            new_end_dt = new_start_dt + timedelta(minutes=duration_minutes)
            
            # ★ Mark OLD booking as CANCELLED (this is what was missing!) ★
            logger.info(f"[RESCHEDULE] Marking OLD booking {booking_uid} as cancelled in database")
            cursor_conn = db.get_connection()
            cursor_db = cursor_conn.cursor()
            cursor_db.execute(
                "UPDATE bookings SET status = %s, notes = %s WHERE calcom_uid = %s OR booking_id = %s",
                ('cancelled', 'Cancelled due to reschedule by patient', booking_uid, booking_uid)
            )
            cursor_conn.commit()
            cursor_db.close()
            cursor_conn.close()
            logger.info(f"[RESCHEDULE] ✓ Marked old booking {booking_uid} as cancelled")
            
            # ★ Create NEW booking record with new Cal.com UID ★
            if new_booking_id and patient_name and patient_email:
                logger.info(f"[RESCHEDULE] Creating NEW booking record with calcom_uid={new_booking_id}")
                try:
                    # Get or create user for the new booking
                    user_id = db.get_or_create_user(phone=phone, name=patient_name, email=patient_email)
                    session_id = _session_id_context.get() or f"sess_reschedule_{uuid.uuid4().hex[:8]}"
                    
                    # Create new booking entry
                    new_local_booking_id = db.create_booking(
                        session_id=session_id,
                        user_id=user_id,
                        appointment_start=new_start_dt,
                        appointment_end=new_end_dt,
                        service_type=reason,
                        notes=f"Rescheduled from {current_appointment_time}",
                        calcom_uid=new_booking_id
                    )
                    logger.info(f"[RESCHEDULE] ✓ Created NEW local booking record: {new_local_booking_id} with calcom_uid={new_booking_id}")
                    new_booking_id = new_local_booking_id
                except Exception as e:
                    logger.error(f"[RESCHEDULE] Warning: Could not create new booking record: {e}")
            
            # Update user contact
            try:
                db.upsert_user_contact(name=patient_name, phone=phone, email=patient_email, calcom_uid=new_booking_id or booking_uid)
                db.mark_calcom_sync(new_booking_id or booking_uid, new_booking_id or booking_uid, sync_status="rescheduled")
                logger.info(f"[RESCHEDULE] ✓ Synced user contact and marked booking as rescheduled")
            except Exception as e:
                logger.error(f"[RESCHEDULE] Warning: Could not sync user contact or mark sync: {e}")
        except Exception as e:
            logger.error(f"[RESCHEDULE] Error updating database: {e}")
        
        # Provide confirmation
        try:
            new_time_formatted = datetime.fromisoformat(new_start_time.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%A, %B %d at %I:%M %p").lstrip('0')
        except:
            new_time_formatted = new_start_time
        
        confirmation = f"Perfect! All set — your appointment has been rescheduled to {new_time_formatted}. Your old appointment has been cancelled, and you'll get confirmation shortly. Is there anything else?"
        
        logger.info(f"[RESCHEDULE] ✓ Reschedule complete: {booking_uid} → {new_booking_id}")
        return confirmation
        
    except Exception as e:
        logger.error(f"[RESCHEDULE] Unexpected error during reschedule: {e}")
        return add_memo_context_to_response("I'm experiencing a technical issue with the rescheduling. Please try again or contact our team for assistance.")
    
    # Need more information
    return add_memo_context_to_response(f"I need a bit more information to complete the reschedule. Could you please provide your email address?")


# ============================================================================
# ADMIN TOOLS
# ============================================================================

@function_tool(
    description="Admin control tool for system maintenance. Validates admin password and provides cleanup options for database tables, memo memory, and sessions. Only available to authorized admin users. Parameters: password (required), action (optional - 'menu' to show options, 'confirm_cleanup' to execute cleanup after confirmation)"
)
async def admin_cleanup(
    password: str,
    action: str = "menu"
):
    """
    Admin cleanup control tool.
    
    Args:
        password: Admin password (must be "admin123")
        action: "menu" to show cleanup options, "confirm_cleanup" to execute cleanup
    
    Returns:
        Menu with cleanup confirmation or cleanup status
    """
    # Validate password
    ADMIN_PASSWORD = "admin123"
    
    if password != ADMIN_PASSWORD:
        logger.warning(f"[ADMIN_CLEANUP] Unauthorized access attempt with wrong password")
        return "Access denied: Invalid admin password."
    
    logger.info(f"[ADMIN_CLEANUP] Admin verified. Action: {action}")
    
    # Show cleanup menu
    if action == "menu":
        menu = """[ADMIN] Access verified!

SYSTEM CLEANUP OPTIONS:
The following items will be cleaned:

1. Database Tables (All):
   - users (total records will be deleted)
   - bookings (total records will be deleted)
   - booking_history (total records will be deleted)
   - sessions (total records will be deleted)
   - conversation_logs (total records will be deleted)
   - session_analytics (total records will be deleted)

2. Memory Systems:
   - Memo conversation memory (cleared)
   - All user context and session data (reset)

3. Database Auto-Increment Counters:
   - All AUTO_INCREMENT values reset to 1

Would you like to proceed with this cleanup? 
Please say "yes, proceed with cleanup" to continue."""
        return menu
    
    # Execute cleanup
    elif action == "confirm_cleanup":
        try:
            logger.info(f"[ADMIN_CLEANUP] Starting full system cleanup...")
            
            db = get_db()
            tables_to_clear = [
                'session_analytics',
                'conversation_logs',
                'booking_history',
                'bookings',
                'sessions',
                'users'
            ]
            
            cleared_tables = []
            errors = []
            
            # Clear all database tables
            for table in tables_to_clear:
                try:
                    conn = db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute(f"DELETE FROM {table}")
                    cursor.execute(f"ALTER TABLE {table} AUTO_INCREMENT = 1")
                    conn.commit()
                    cursor.close()
                    conn.close()
                    cleared_tables.append(table)
                    logger.info(f"[ADMIN_CLEANUP] Cleared {table}")
                except Exception as e:
                    error_msg = f"{table}: {str(e)}"
                    errors.append(error_msg)
                    logger.error(f"[ADMIN_CLEANUP] Error clearing {table}: {e}")
            
            # Clear memo memory
            try:
                clear_memo()
                logger.info(f"[ADMIN_CLEANUP] Cleared memo memory")
            except Exception as e:
                error_msg = f"Memo memory: {str(e)}"
                errors.append(error_msg)
                logger.error(f"[ADMIN_CLEANUP] Error clearing memo: {e}")
            
            # Build response
            response = "CLEANUP COMPLETED!\n\n"
            response += "Cleared items:\n"
            for table in cleared_tables:
                response += f"  [OK] {table}\n"
            response += "  [OK] Memo conversation memory\n"
            response += "  [OK] All AUTO_INCREMENT counters reset\n"
            
            if errors:
                response += "\nErrors encountered:\n"
                for error in errors:
                    response += f"  [ERROR] {error}\n"
            
            logger.info(f"[ADMIN_CLEANUP] [OK] Full cleanup complete. Tables cleared: {len(cleared_tables)}")
            return response
            
        except Exception as e:
            error_response = f"Error during cleanup: {str(e)}"
            logger.error(f"[ADMIN_CLEANUP] Unexpected error: {e}")
            return error_response
    
    else:
        return "Invalid action. Use 'menu' to show options or 'confirm_cleanup' to execute cleanup."

