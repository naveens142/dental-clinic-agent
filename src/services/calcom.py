"""
Cal.com v2 Service – Hybrid Working Version
- Uses v2 API for user info and schedule data
- Generates availability slots from schedule working hours  
- Uses v2 API for booking appointments
"""

import os
import requests
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

# ----------------------------
# ENV + LOGGING
# ----------------------------
load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CALCOM_API_KEY = os.getenv("CALCOM_API_KEY")
CALCOM_EVENT_TYPE_ID = os.getenv("CALCOM_EVENT_TYPE_ID")
CALCOM_BASE_URL = os.getenv("CALCOM_BASE_URL", "https://api.cal.com/v2")
CALCOM_DRY_RUN = os.getenv("CALCOM_DRY_RUN", "true").lower() == "true"

if not CALCOM_API_KEY:
    raise EnvironmentError("CALCOM_API_KEY is not set in .env")
if not CALCOM_EVENT_TYPE_ID:
    raise EnvironmentError("CALCOM_EVENT_TYPE_ID is not set in .env")

HEADERS = {
    "Authorization": f"Bearer {CALCOM_API_KEY}",
    "Content-Type": "application/json",
}

# ----------------------------
# HELPERS
# ----------------------------

def _ensure_iso_utc(dt: str) -> str:
    """
    Ensure datetime string is in ISO format with UTC timezone.
    If timezone-naive, assume UTC.
    """
    parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Naive datetime - assume UTC
        from zoneinfo import ZoneInfo
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.isoformat()

def _get_standard_date_range(days_ahead: int = 90) -> tuple:
    """
    Generate standard date range for CalCom API filtering.
    
    Returns:
        (afterStart: str, beforeEnd: str) - ISO format with UTC timezone
        afterStart = today at 00:00:00Z
        beforeEnd = days_ahead from today at 23:59:59Z
    
    Example:
        afterStart, beforeEnd = _get_standard_date_range(90)
        # afterStart = "2026-02-07T00:00:00+00:00"
        # beforeEnd = "2026-05-08T23:59:59+00:00"
    """
    # Today at 00:00:00 UTC
    today = datetime.now(ZoneInfo("UTC")).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 90 days from today at 23:59:59 UTC
    end_date = today + timedelta(days=days_ahead)
    end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=0)
    
    return today.isoformat(), end_date.isoformat()

# ----------------------------
# GET SCHEDULE INFO
# ----------------------------

def get_first_schedule() -> Dict[str, Any]:
    url = f"{CALCOM_BASE_URL}/schedules"
    # Note: schedules endpoint works without the cal-api-version header
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    data = response.json().get("data", [])
    if not data:
        raise RuntimeError("No schedules found for this account.")
    return data[0]

def generate_slots_from_schedule(
    schedule: Dict[str, Any], 
    start_dt: datetime, 
    end_dt: datetime, 
    duration_minutes: int,
    timezone_name: str
) -> Dict[str, List[Dict[str, str]]]:
    """Generate available slots from schedule data"""
    
    slots_by_date = {}
    
    # Get working hours from schedule
    working_hours = schedule.get("workingHours", [])
    if not working_hours:
        logger.warning("No working hours found in schedule")
        return {}
        
    # For simplicity, use the first working hours entry
    work_hours = working_hours[0]
    work_days = work_hours["days"]  # [1,2,3,4,5] for Mon-Fri
    start_minutes = work_hours["startTime"]  # Minutes from midnight
    end_minutes = work_hours["endTime"]    # Minutes from midnight
    
    # Convert minutes to hours
    work_start_hour = start_minutes // 60
    work_start_min = start_minutes % 60
    work_end_hour = end_minutes // 60
    work_end_min = end_minutes % 60
    
    logger.info(f"Working hours: {work_start_hour:02d}:{work_start_min:02d} - {work_end_hour:02d}:{work_end_min:02d} on days {work_days}")
    
    # Generate slots for each day in the range
    current_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    tz = ZoneInfo(timezone_name)
    
    # Convert start and end to target timezone for proper day iteration
    start_dt_tz = start_dt.astimezone(tz)
    end_dt_tz = end_dt.astimezone(tz)
    
    # Get current time in the target timezone to filter past slots
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_tz = now_utc.astimezone(tz)
    # Add buffer of 10 minutes to ensure slot is bookable
    min_booking_time = now_tz + timedelta(minutes=10)
    
    # Start iteration from the start date in target timezone
    current_dt_tz = start_dt_tz.replace(hour=0, minute=0, second=0, microsecond=0)
    
    while current_dt_tz <= end_dt_tz:
        # Check if this day is a working day (1=Monday, 7=Sunday)
        weekday = current_dt_tz.isoweekday()  # 1-7
        
        if weekday in work_days:
            date_key = current_dt_tz.strftime("%Y-%m-%d")
            day_slots = []
            
            # Generate slots for this day in the target timezone
            slot_start = current_dt_tz.replace(hour=work_start_hour, minute=work_start_min)
            work_end = current_dt_tz.replace(hour=work_end_hour, minute=work_end_min)
            
            while slot_start + timedelta(minutes=duration_minutes) <= work_end:
                # slot_start is already in target timezone
                # Only include slots that are in the future (with 10-minute buffer)
                if slot_start >= min_booking_time:
                    slot_iso = slot_start.isoformat()
                    day_slots.append({
                        "start": slot_iso
                    })
                
                # Move to next slot
                slot_start += timedelta(minutes=duration_minutes)
            
            # Only add date if it has available slots
            if day_slots:
                slots_by_date[date_key] = day_slots
                
        current_dt_tz += timedelta(days=1)
    
    logger.info(f"Generated slots for {len(slots_by_date)} days")
    return slots_by_date

# ----------------------------
# GET EXISTING BOOKINGS
# ----------------------------

def get_bookings(start_date: datetime, end_date: datetime, status: str = "upcoming") -> List[Dict[str, Any]]:
    """
    Fetch bookings for the event type within the date range using Cal.com API filtering.
    Cal.com filters on server side for better performance and reduced payload.
    
    ✓ MANDATORY FILTERS:
    - eventTypeId: Specific event type (required)
    - afterStart: Start date/time (required)
    - beforeEnd: End date/time (required)
    - status: Booking status filter (default: "upcoming")
    
    ✗ NEVER fetch without these parameters
    
    Args:
        start_date: Start date to check bookings (inclusive, 00:00:00)
        end_date: End date to check bookings (inclusive, 23:59:59)
        status: Cal.com status filter - "upcoming" (default, future bookings),
                "accepted", "cancelled", "pending", etc.
        
    Returns:
        List of booking objects (pre-filtered by Cal.com API)
    """
    try:
        # Format dates as ISO strings
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        url = f"{CALCOM_BASE_URL}/bookings"
        params = {
            "eventTypeId": CALCOM_EVENT_TYPE_ID,  # ✓ MANDATORY
            "afterStart": start_str,              # ✓ MANDATORY
            "beforeEnd": end_str,                 # ✓ MANDATORY
            "status": status                      # ✓ MANDATORY (default: upcoming)
        }
        
        headers = HEADERS.copy()
        headers["cal-api-version"] = "2024-08-13"
        
        # Log the API call with all mandatory filters
        logger.info(f"[GET_BOOKINGS] ✓ Fetching {status} bookings with mandatory filters:")
        logger.info(f"  - eventTypeId={CALCOM_EVENT_TYPE_ID}")
        logger.info(f"  - afterStart={start_str}")
        logger.info(f"  - beforeEnd={end_str}")
        logger.info(f"  - status={status}")
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.ok:
            data = response.json()
            
            # Extract bookings from response
            if isinstance(data, list):
                bookings = data
            elif isinstance(data, dict) and "data" in data:
                nested = data["data"]
                bookings = nested if isinstance(nested, list) else []
            else:
                bookings = []
            
            logger.info(f"Found {len(bookings)} {status} bookings")
            return bookings
        else:
            logger.warning(f"Failed to fetch bookings: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Error fetching bookings: {e}", exc_info=True)
        return []

def get_bookings_by_email(attendee_email: str, start_date: datetime, end_date: datetime, status: str = "upcoming,unconfirmed") -> List[Dict[str, Any]]:
    """
    Fetch bookings filtered by attendee email using server-side filtering.
    Cal.com filters results by attendeeEmail parameter for optimal performance.
    This is the preferred method when patient email is available.
    
    ✓ MANDATORY FILTERS:
    - eventTypeId: Specific event type (required)
    - afterStart: Start date/time (required)
    - beforeEnd: End date/time (required)
    - status: Booking status filter (required, default: "upcoming,unconfirmed")
    - attendeeEmail: Email to filter by (REQUIRED, enables server-side email filtering)
    
    ✗ NEVER fetch without attendeeEmail - must not retrieve all bookings
    
    Args:
        attendee_email: Attendee email to filter by (server-side filter, REQUIRED)
        start_date: Start date to check bookings (inclusive, 00:00:00)
        end_date: End date to check bookings (inclusive, 23:59:59)
        status: Cal.com status filter - comma-separated like "upcoming,unconfirmed" (default)
                
    Returns:
        List of booking objects matching the email filter (usually 0-1 results)
    """
    try:
        # Validate required parameters
        if not attendee_email:
            logger.error("[GET_BOOKINGS_BY_EMAIL] ✗ ERROR: attendeeEmail is REQUIRED - cannot fetch bookings without email filter")
            return []
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        url = f"{CALCOM_BASE_URL}/bookings"
        params = {
            "eventTypeId": CALCOM_EVENT_TYPE_ID,     # ✓ MANDATORY
            "afterStart": start_str,                 # ✓ MANDATORY
            "beforeEnd": end_str,                    # ✓ MANDATORY
            "status": status,                        # ✓ MANDATORY
            "attendeeEmail": attendee_email          # ✓ MANDATORY - server-side filtering
        }
        
        headers = HEADERS.copy()
        headers["cal-api-version"] = "2024-08-13"
        
        # Log the API call with all mandatory filters
        logger.info(f"[GET_BOOKINGS_BY_EMAIL] ✓ Fetching {status} bookings with mandatory filters:")
        logger.info(f"  - eventTypeId={CALCOM_EVENT_TYPE_ID}")
        logger.info(f"  - afterStart={start_str}")
        logger.info(f"  - beforeEnd={end_str}")
        logger.info(f"  - status={status}")
        logger.info(f"  - attendeeEmail={attendee_email} (server-side filter)")
        
        response = requests.get(url, headers=headers, params=params)
        
        if response.ok:
            data = response.json()
            
            # Extract bookings from response
            if isinstance(data, list):
                bookings = data
            elif isinstance(data, dict) and "data" in data:
                nested = data["data"]
                bookings = nested if isinstance(nested, list) else []
            else:
                bookings = []
            
            logger.info(f"Found {len(bookings)} {status} bookings for {attendee_email}")
            return bookings
        else:
            logger.warning(f"Failed to fetch bookings for {attendee_email}: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Error fetching bookings by email: {e}", exc_info=True)
        return []

# ========================
# HELPER FUNCTIONS FOR INTELLIGENT MATCHING
# ========================

def _normalize_name(name: str) -> set:
    """
    Normalize name for intelligent matching.
    Converts to lowercase and returns set of words.
    
    Args:
        name: Name string to normalize
        
    Returns:
        Set of words in the name
    """
    if not name:
        return set()
    return set(name.lower().split())


def _names_match(user_provided_name: str, stored_name: str) -> bool:
    """
    Check if names match using word-based comparison.
    User-provided name words should be a subset of stored name words.
    
    Example:
        user_provided_name = "Naveen Sharma"
        stored_name = "Naveen Kumar Sharma"
        Result: True (both names match)
    
    Args:
        user_provided_name: Name provided by user (e.g., "Naveen Sharma")
        stored_name: Name from CalCom booking (e.g., "Naveen Kumar Sharma")
        
    Returns:
        True if names match intelligently, False otherwise
    """
    if not user_provided_name or not stored_name:
        return False
    
    user_words = _normalize_name(user_provided_name)
    stored_words = _normalize_name(stored_name)
    
    # User's words should be a subset of stored words
    # e.g., {"naveen", "sharma"} ⊆ {"naveen", "kumar", "sharma"} = True
    match = user_words.issubset(stored_words)
    logger.debug(f"Name matching: user={user_words}, stored={stored_words}, match={match}")
    return match


def _extract_attendee_phone(booking: Dict[str, Any]) -> Optional[str]:
    """
    Extract phone number from booking's custom fields or metadata.
    Cal.com stores phone in bookingFieldsResponses from the booking form.
    
    Args:
        booking: Booking dictionary from Cal.com
        
    Returns:
        Phone number if found, None otherwise
    """
    # Check bookingFieldsResponses (custom fields from booking form)
    booking_fields = booking.get('bookingFieldsResponses', {})
    if isinstance(booking_fields, dict):
        phone = booking_fields.get('phone')
        if phone:
            return str(phone)
    
    # Check attendees' phone field if available
    attendees = booking.get('attendees', [])
    if attendees:
        attendee_phone = attendees[0].get('phone')
        if attendee_phone:
            return str(attendee_phone)
    
    return None


def _normalize_phone(phone: str) -> str:
    """
    Normalize phone number for comparison.
    Removes all non-numeric characters.
    
    Args:
        phone: Phone number to normalize
        
    Returns:
        Normalized phone number (digits only)
    """
    if not phone:
        return ""
    return ''.join(filter(str.isdigit, str(phone)))


def _phones_match(user_phone: str, booking_phone: str) -> bool:
    """
    Check if phone numbers match.
    Handles different formats by normalizing both.
    
    Args:
        user_phone: Phone provided by user
        booking_phone: Phone from CalCom booking
        
    Returns:
        True if phones match, False otherwise
    """
    if not user_phone or not booking_phone:
        return False
    
    user_normalized = _normalize_phone(user_phone)
    booking_normalized = _normalize_phone(booking_phone)
    
    # Match if normalized numbers are the same
    match = user_normalized == booking_normalized
    logger.debug(f"Phone matching: user={user_normalized}, booking={booking_normalized}, match={match}")
    return match


def find_booking_by_patient_info(
    patient_name: str = None,
    patient_email: str = None,
    patient_phone: str = None,
    appointment_time: datetime = None,
    appointment_date: datetime = None,
    search_days: int = 30
) -> Optional[Dict[str, Any]]:
    """
    Find a patient's booking in CalCom using optimized API flow.
    
    ✓ DESIGN FLOW:
    1. Email (primary) → Fetch by email (server-side filter via API)
    2. Name filter → Smart word-matching (Python-side)
    3. Phone filter → Normalized phone matching (Python-side)
    4. Time filter → Optional time matching
    
    ✓ SOURCE OF TRUTH: CalCom API only (never database)
    ✓ API CALL: Uses status=upcoming,unconfirmed&attendeeEmail=naveens142@gmail.com
    
    EXAMPLES:
    - find_booking_by_patient_info(patient_email="user@clinic.com")
    - find_booking_by_patient_info(patient_email="user@clinic.com", patient_name="Naveen", patient_phone="8341756605")
    - find_booking_by_patient_info(patient_email="user@clinic.com", patient_phone="834-175-6605")
    
    Args:
        patient_email: Patient's email (PRIMARY - enables server-side API filtering)
        patient_name: Patient's name (OPTIONAL - filtered with smart word-matching)
        patient_phone: Patient's phone (OPTIONAL - filtered with normalization)
        appointment_time: Specific time to match (optional, e.g., "12:00" or ISO datetime)
        appointment_date: Specific date to search (optional, e.g., "2026-02-09")
        search_days: How many days to search (default: 30)
        
    Returns:
        Booking dict if found, or None if not found
    """
    try:
        # ✓ Use standard date range: today to 90 days ahead
        # afterStart = today at 00:00:00Z, beforeEnd = 90 days from today at 23:59:59Z
        if appointment_date:
            # If specific date provided, search only that date
            if isinstance(appointment_date, str):
                date_obj = datetime.fromisoformat(appointment_date)
            else:
                date_obj = appointment_date
            start_dt = date_obj.replace(hour=0, minute=0, second=0, tzinfo=ZoneInfo("UTC"))
            end_dt = date_obj.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo("UTC"))
        else:
            # Default: use standard 90-day window (today to 90 days ahead)
            start_dt, _ = _get_standard_date_range(90)
            start_dt = datetime.fromisoformat(start_dt)
            _, end_dt = _get_standard_date_range(90)
            end_dt = datetime.fromisoformat(end_dt)
        
        # Require at least email or name for lookup
        if not patient_email and not patient_name:
            logger.warning("[LOOKUP] No patient email or name provided for booking lookup")
            return None
        
        # ========== STEP 1: Fetch ACTIVE bookings from CalCom ==========
        # Primary: Use email for server-side filtering (most efficient)
        # Filter: status=upcoming,unconfirmed&attendeeEmail=naveens142@gmail.com
        # Date Range: afterStart=today at 00:00:00Z, beforeEnd=90 days ahead at 23:59:59Z
        
        bookings = []
        
        if patient_email:
            logger.info(f"[LOOKUP] ★ Fetching ACTIVE bookings by email: {patient_email}")
            logger.info(f"[LOOKUP]   API Filter: status=upcoming,unconfirmed&attendeeEmail={patient_email}")
            bookings = get_bookings_by_email(
                patient_email, 
                start_dt, 
                end_dt, 
                status="upcoming,unconfirmed"
            )
            logger.info(f"[LOOKUP] Found {len(bookings)} ACTIVE bookings for email: {patient_email}")
        else:
            # Fallback: fetch all active and filter by name (less efficient)
            logger.info(f"[LOOKUP] ⚠ No email provided, fetching all ACTIVE bookings (will filter by name in Python)")
            bookings = get_bookings(start_dt, end_dt, status="upcoming,unconfirmed")
            logger.info(f"[LOOKUP] Found {len(bookings)} total ACTIVE bookings to search")
        
        if not bookings:
            logger.info(f"[LOOKUP] No ACTIVE bookings found in CalCom")
            return None
        
        # ========== STEP 2: Filter results by name & phone (Python-side) ==========
        matching_bookings = []
        
        for booking in bookings:
            attendees = booking.get('attendees', [])
            if not attendees:
                continue
            
            attendee = attendees[0]
            attendee_name = attendee.get('name', '')
            attendee_email = attendee.get('email', '').lower()
            
            # --- Name Filter (optional, smart word-matching) ---
            if patient_name:
                if not _names_match(patient_name, attendee_name):
                    logger.debug(f"[LOOKUP] ✗ Name mismatch: user='{patient_name}' vs booking='{attendee_name}'")
                    continue
                logger.debug(f"[LOOKUP] ✓ Name match: '{patient_name}' matches '{attendee_name}'")
            
            # --- Phone Filter (optional, normalized matching) ---
            if patient_phone:
                booking_phone = _extract_attendee_phone(booking)
                if not booking_phone or not _phones_match(patient_phone, booking_phone):
                    logger.debug(f"[LOOKUP] ✗ Phone mismatch: user='{patient_phone}' vs booking='{booking_phone}'")
                    continue
                logger.debug(f"[LOOKUP] ✓ Phone match: '{patient_phone}' matches '{booking_phone}'")
            
            # --- Time Filter (optional) ---
            if appointment_time:
                if not _matches_appointment_time(booking, appointment_time):
                    logger.debug(f"[LOOKUP] ✗ Time mismatch for {attendee_email}")
                    continue
                logger.debug(f"[LOOKUP] ✓ Time match for {attendee_email}")
            
            # ✓ All filters passed - this is a match!
            matching_bookings.append(booking)
            logger.info(f"[LOOKUP] ✓ MATCH: {attendee_name} ({attendee_email}) at {booking.get('start')}")
        
        if not matching_bookings:
            logger.info(f"[LOOKUP] No ACTIVE bookings matched the filters (name/phone/time)")
            return None
        
        # Return the most recent booking if multiple matches
        if len(matching_bookings) > 1:
            logger.info(f"[LOOKUP] Found {len(matching_bookings)} matching bookings, returning most recent")
            booking = sorted(matching_bookings, key=lambda b: b.get('start', ''), reverse=True)[0]
        else:
            booking = matching_bookings[0]
        
        logger.info(f"[LOOKUP] ★ SUCCESS: Found booking UID={booking.get('uid')}, Start={booking.get('start')}")
        return booking
            
    except Exception as e:
        logger.error(f"[LOOKUP] Error finding booking by patient info: {e}", exc_info=True)
        return None


def find_all_bookings_by_patient_info(
    patient_name: str = None,
    patient_email: str = None,
    patient_phone: str = None,
    appointment_date: datetime = None,
    search_days: int = 30
) -> Optional[List[Dict[str, Any]]]:
    """
    Find ALL of a patient's bookings in CalCom.
    
    CRITICAL: Returns ALL matching appointments, not just the first one.
    This is needed when a patient has multiple appointments on the same day
    with the same email but different names (family members).
    
    ✓ DESIGN FLOW:
    1. Email (primary) → Fetch all by email (server-side filter via API)
    2. Name filter → Filter each booking by name (Python-side)
    3. Phone filter → Normalized phone matching (Python-side)
    
    EXAMPLES:
    - find_all_bookings_by_patient_info(patient_email="family@example.com")
      → Returns [booking1, booking2, booking3] for all family members
    - find_all_bookings_by_patient_info(patient_email="family@example.com", patient_name="Naveen")
      → Returns [booking1, booking2] only for Naveen
    
    Args:
        patient_email: Patient's email (PRIMARY - enables server-side API filtering)
        patient_name: Patient's name (OPTIONAL - filter results by name)
        patient_phone: Patient's phone (OPTIONAL - filter results by phone)
        appointment_date: Specific date to search (optional)
        search_days: How many days to search (default: 30)
        
    Returns:
        List of booking dicts if found, empty list if none found
    """
    try:
        if appointment_date:
            if isinstance(appointment_date, str):
                date_obj = datetime.fromisoformat(appointment_date)
            else:
                date_obj = appointment_date
            start_dt = date_obj.replace(hour=0, minute=0, second=0, tzinfo=ZoneInfo("UTC"))
            end_dt = date_obj.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo("UTC"))
        else:
            start_dt, _ = _get_standard_date_range(90)
            start_dt = datetime.fromisoformat(start_dt)
            _, end_dt = _get_standard_date_range(90)
            end_dt = datetime.fromisoformat(end_dt)
        
        if not patient_email and not patient_name:
            logger.warning("[LOOKUP_ALL] No patient email or name provided")
            return []
        
        bookings = []
        
        if patient_email:
            logger.info(f"[LOOKUP_ALL] ★ Fetching ALL ACTIVE bookings by email: {patient_email}")
            bookings = get_bookings_by_email(patient_email, start_dt, end_dt, status="upcoming,unconfirmed")
            logger.info(f"[LOOKUP_ALL] Found {len(bookings)} bookings for email: {patient_email}")
        else:
            logger.info(f"[LOOKUP_ALL] Fetching all ACTIVE bookings")
            bookings = get_bookings(start_dt, end_dt, status="upcoming,unconfirmed")
        
        if not bookings:
            return []
        
        matching_bookings = []
        
        for booking in bookings:
            attendees = booking.get('attendees', [])
            if not attendees:
                continue
            
            attendee = attendees[0]
            attendee_name = attendee.get('name', '')
            
            if patient_name and not _names_match(patient_name, attendee_name):
                continue
            
            if patient_phone:
                booking_phone = _extract_attendee_phone(booking)
                if not booking_phone or not _phones_match(patient_phone, booking_phone):
                    continue
            
            matching_bookings.append(booking)
            logger.info(f"[LOOKUP_ALL] ✓ MATCH: {attendee_name} at {booking.get('start')}")
        
        if not matching_bookings:
            return []
        
        matching_bookings.sort(key=lambda b: b.get('start', ''))
        logger.info(f"[LOOKUP_ALL] ★ SUCCESS: Found {len(matching_bookings)} matching bookings")
        return matching_bookings
            
    except Exception as e:
        logger.error(f"[LOOKUP_ALL] Error finding all bookings: {e}", exc_info=True)
        return []


def _matches_appointment_time(booking: Dict[str, Any], appointment_time: str, timezone_name: str = "Asia/Kolkata") -> bool:
    """
    Helper function to check if booking time matches requested time.
    Handles timezone conversions properly.
    
    Args:
        booking: Booking dictionary from Cal.com
        appointment_time: Time string (e.g., "12:00", "2:30 PM", "10:00 AM", full ISO datetime)
        timezone_name: User's timezone for conversion (default: Asia/Kolkata)
        
    Returns:
        True if times match, False otherwise
    """
    try:
        # Get booking start time (stored in UTC in Cal.com)
        booking_time_str = booking.get('start') or booking.get('startTime', '')
        if not booking_time_str:
            return False
            
        # Parse booking time and convert to user timezone
        booking_dt_utc = datetime.fromisoformat(booking_time_str.replace('Z', '+00:00'))
        user_tz = ZoneInfo(timezone_name)
        booking_dt_local = booking_dt_utc.astimezone(user_tz)
        
        # Handle full ISO datetime strings (e.g., "2026-02-09T10:00:00+05:30")
        if isinstance(appointment_time, str) and 'T' in appointment_time:
            try:
                # Try to parse as ISO datetime
                requested_dt = datetime.fromisoformat(appointment_time.replace('Z', '+00:00'))
                
                # If timezone-naive, assume user's timezone
                if requested_dt.tzinfo is None:
                    requested_dt = requested_dt.replace(tzinfo=user_tz)
                else:
                    # Convert to user's timezone for comparison
                    requested_dt = requested_dt.astimezone(user_tz)
                
                # Compare date and hour (user timezone)
                return (booking_dt_local.year == requested_dt.year and 
                        booking_dt_local.month == requested_dt.month and 
                        booking_dt_local.day == requested_dt.day and 
                        booking_dt_local.hour == requested_dt.hour)
            except ValueError:
                pass  # Fall through to time-only parsing
        
        # Handle time-only strings (e.g., "10:00", "2:30 PM", "10:30 AM")
        if isinstance(appointment_time, str) and ':' in appointment_time:
            # Extract time components
            time_str = appointment_time.strip().upper()
            
            # Parse hour:minute format
            time_parts = time_str.split(':')
            if len(time_parts) < 2:
                return False
                
            try:
                search_hour = int(time_parts[0])
                search_minute = int(time_parts[1].split()[0])  # Handle "30 PM" format
                
                # Convert 12-hour format to 24-hour if needed
                if 'PM' in time_str and search_hour != 12:
                    search_hour += 12
                elif 'AM' in time_str and search_hour == 12:
                    search_hour = 0
                
                # Compare in user's timezone
                # Check hour and minute (allow some tolerance for seconds)
                return (booking_dt_local.hour == search_hour and 
                        booking_dt_local.minute == search_minute)
                        
            except (ValueError, IndexError):
                logger.debug(f"Could not parse time string: {appointment_time}")
                return False
        elif isinstance(appointment_time, datetime):
            # If datetime object provided, compare in user's timezone
            if appointment_time.tzinfo is None:
                requested_dt = appointment_time.replace(tzinfo=user_tz)
            else:
                requested_dt = appointment_time.astimezone(user_tz)
            
            return (booking_dt_local.hour == requested_dt.hour and 
                    booking_dt_local.minute == requested_dt.minute)
    except Exception as e:
        logger.warning(f"Error matching appointment time: {e}")
    
    return False
# GET AVAILABILITY
# ----------------------------

def get_availability(
    start: str,
    end: str,
    timezone_name: str = "Asia/Kolkata",
    duration_minutes: int = None
) -> Dict[str, Any]:
    """
    Get availability using hybrid approach.
    Constructs slots from schedule working hours and filters out already booked slots.
    
    ✓ Date Range Enforcement: 
    - start must be today or later
    - end must be within 90 days from today
    - Uses status=upcoming with CalCom API filtering
    """
    start_iso = _ensure_iso_utc(start)
    end_iso = _ensure_iso_utc(end)
    
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    
    # Enforce date range constraints
    today = datetime.now(ZoneInfo("UTC")).replace(hour=0, minute=0, second=0, microsecond=0)
    max_date = today + timedelta(days=90)
    
    if start_dt.date() < today.date():
        logger.warning(f"[AVAILABILITY] Start date {start_dt.date()} is in the past, moving to today")
        start_dt = today
    
    if end_dt.date() > max_date.date():
        logger.warning(f"[AVAILABILITY] End date {end_dt.date()} exceeds 90-day limit, capping at {max_date.date()}")
        end_dt = max_date.replace(hour=23, minute=59, second=59)

    logger.info(f"Generating availability from schedule for eventType={CALCOM_EVENT_TYPE_ID} "
                f"from {start_dt.isoformat()} to {end_dt.isoformat()} [{timezone_name}]")
    logger.info(f"✓ CalCom API Filter: status=upcoming&afterStart={start_dt.isoformat()}&beforeEnd={end_dt.isoformat()}")

    # Get schedule info and generate slots
    schedule = get_first_schedule()
    logger.info(f"Using schedule: {schedule['name']}")
    
    # Generate time slots based on schedule working hours
    if duration_minutes is None:
        duration_minutes = 30  # Default duration
        
    all_slots = generate_slots_from_schedule(schedule, start_dt, end_dt, duration_minutes, timezone_name)
    
    # Fetch upcoming bookings only (Cal.com filters server-side)
    # Only upcoming bookings block availability
    existing_bookings = get_bookings(start_dt, end_dt, status="upcoming")
    
    # Create set of blocked time slots (including duration)
    blocked_times = set()
    tz = ZoneInfo(timezone_name)
    
    for booking in existing_bookings:
        # Parse booking start time and duration
        booking_start = booking.get("start") or booking.get("startTime")
        booking_duration = booking.get("duration", 15)  # Duration in minutes, default 15
        
        if booking_start:
            try:
                # Convert booking time to target timezone
                booking_dt = datetime.fromisoformat(booking_start.replace("Z", "+00:00"))
                booking_dt_tz = booking_dt.astimezone(tz)
                booking_end_dt_tz = booking_dt_tz + timedelta(minutes=booking_duration)
                
                # Add the exact booking window to blocked times
                # We'll check overlaps during slot filtering
                blocked_times.add((booking_dt_tz, booking_end_dt_tz))
                    
                logger.debug(f"Booking: {booking_dt_tz.isoformat()} to {booking_end_dt_tz.isoformat()}")
            except Exception as e:
                logger.warning(f"Failed to parse booking time {booking_start}: {e}")
    logger.info(f"Found {len(existing_bookings)} bookings")
    
    # Filter out booked slots
    available_slots = {}
    total_slots = 0
    available_count = 0
    
    for date_key, slots in all_slots.items():
        available_day_slots = []
        for slot in slots:
            total_slots += 1
            slot_time = slot["start"]
            slot_dt = datetime.fromisoformat(slot_time)
            slot_end_dt = slot_dt + timedelta(minutes=duration_minutes)
            
            # Check if this slot overlaps with any booking
            is_blocked = False
            for booking_start, booking_end in blocked_times:
                # Slot overlaps with booking if:
                # - Slot starts before booking ends AND slot ends after booking starts
                if slot_dt < booking_end and slot_end_dt > booking_start:
                    is_blocked = True
                    break
            
            if not is_blocked:
                available_day_slots.append(slot)
                available_count += 1
        
        # Only include dates that have available slots
        if available_day_slots:
            available_slots[date_key] = available_day_slots
    
    logger.info(f"Filtered: {total_slots} total slots, {available_count} available, {total_slots - available_count} blocked")
    
    # Return in the expected format
    return {
        "status": "success",
        "data": available_slots
    }

# ----------------------------
# BOOK APPOINTMENT
# ----------------------------

def book_appointment(
    name: str,
    email: str,
    start_time: str,
    timezone_name: str = "Asia/Kolkata",
    duration_minutes: int = 15
) -> Dict[str, Any]:
    """
    Book an appointment using v2 API.
    """
    # Convert start_time to UTC format as required by API v2
    # The start_time comes in format like "2026-02-06T09:00:00.000+05:30"
    # We need to convert it to UTC format
    try:
        # Parse the datetime string with timezone info into an aware datetime
        parsed_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        # Convert to UTC timezone-aware ISO string (Cal.com expects Z suffix)
        utc_dt = parsed_dt.astimezone(timezone.utc)
        utc_iso_format = utc_dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        logger.info(f"Converted {start_time} to UTC: {utc_iso_format}")
    except Exception as e:
        logger.error(f"Error converting start_time to UTC: {e}")
        # Fallback: use provided value but ensure it ends with Z if it's naive
        utc_iso_format = start_time
        if isinstance(utc_iso_format, str) and not (utc_iso_format.endswith('Z') or '+' in utc_iso_format):
            utc_iso_format += 'Z'

    payload = {
        "eventTypeId": int(CALCOM_EVENT_TYPE_ID),
        "start": utc_iso_format,
        "attendee": {
            "name": name,
            "email": email,
            "timeZone": timezone_name
        }
    }

    print("Booking payload:", payload)

    if CALCOM_DRY_RUN:
        logger.warning("CALCOM_DRY_RUN enabled — booking skipped")
        return {"status": "DRY_RUN", "payload": payload}

    url = f"{CALCOM_BASE_URL}/bookings"
    
    # Add API version header for booking
    booking_headers = HEADERS.copy()
    booking_headers["cal-api-version"] = "2024-08-13"
    
    response = requests.post(url, headers=booking_headers, json=payload)
    
    if not response.ok:
        logger.error(f"Booking failed with status {response.status_code}")
        logger.error(f"Response text: {response.text}")
        
        # Parse error message for user-friendly response
        error_message = "Booking failed"
        try:
            error_json = response.json()
            logger.error(f"Error JSON: {error_json}")
            
            # Extract the actual error message
            if "error" in error_json and "message" in error_json["error"]:
                error_message = error_json["error"]["message"]
            elif "message" in error_json:
                error_message = error_json["message"]
        except:
            error_message = response.text
        
        # Return error as a dict instead of raising exception
        return {
            "status": "error",
            "error": error_message,
            "details": f"Cal.com API returned status {response.status_code}"
        }
    
    return response.json()

# ----------------------------
# CANCEL APPOINTMENT
# ----------------------------

def cancel_appointment(
    booking_uid: str,
    cancellation_reason: str = "Cancelled by user"
) -> Dict[str, Any]:
    """
    Cancel an existing appointment using v2 API.
    
    Args:
        booking_uid: The unique ID of the booking to cancel (e.g., "opmpySGx5cN1gVEZMXH1LU")
        cancellation_reason: Optional reason for cancellation
        
    Returns:
        Dict containing cancellation response with explicit success/error status
    """
    if CALCOM_DRY_RUN:
        logger.warning("CALCOM_DRY_RUN enabled — cancellation skipped")
        return {"status": "DRY_RUN", "booking_uid": booking_uid, "reason": cancellation_reason}

    url = f"{CALCOM_BASE_URL}/bookings/{booking_uid}/cancel"
    
    payload = {
        "cancellationReason": cancellation_reason
    }
    
    # Add API version header for cancellation
    cancel_headers = HEADERS.copy()
    cancel_headers["cal-api-version"] = "2024-08-13"
    
    logger.info(f"[CANCEL_APPOINTMENT] Cancelling booking {booking_uid}")
    
    response = requests.post(url, headers=cancel_headers, json=payload)
    
    if not response.ok:
        logger.error(f"[CANCEL_APPOINTMENT] Cancellation failed with status {response.status_code}")
        logger.error(f"[CANCEL_APPOINTMENT] Response text: {response.text}")
        
        # Parse error message for user-friendly response
        error_message = "Cancellation failed"
        try:
            error_json = response.json()
            logger.error(f"[CANCEL_APPOINTMENT] Error JSON: {error_json}")
            
            # Extract the actual error message
            if "error" in error_json and "message" in error_json["error"]:
                error_message = error_json["error"]["message"]
            elif "message" in error_json:
                error_message = error_json["message"]
        except:
            error_message = response.text
        
        # Return error as a dict with explicit error status
        return {
            "status": "error",
            "error": error_message,
            "details": f"Cal.com API returned status {response.status_code}"
        }
    
    # Successfully cancelled - add explicit success status
    response_data = response.json()
    logger.info(f"[CANCEL_APPOINTMENT] ✓ Successfully cancelled booking {booking_uid}")
    logger.info(f"[CANCEL_APPOINTMENT] Response: {response_data}")
    
    # Ensure response has a clear success indicator
    if isinstance(response_data, dict):
        # Check if response indicates cancelled status
        if response_data.get("data", {}).get("status") == "cancelled":
            response_data["status"] = "success"
        elif "status" not in response_data:
            response_data["status"] = "success"
    
    return response_data
