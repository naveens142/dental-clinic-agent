# database_service.py
"""
Database Service Module
Handles all database operations for the dental clinic agent.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import mysql.connector
from mysql.connector import Error, pooling
from dotenv import load_dotenv
import os
import hashlib

load_dotenv()

logger = logging.getLogger(__name__)

class DatabaseService:
    """Service for managing all database operations with connection pooling."""
    
    _pool = None  # Class variable for connection pool
    
    def __init__(self):
        """Initialize database with connection pool."""
        self.config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'root'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'dental_clinic_agent'),
            'port': int(os.getenv('DB_PORT', '3306'))
        }
        self._validate_config()
        self._init_pool()
    
    def _validate_config(self):
        """Validate database configuration."""
        required = ['host', 'user', 'database']
        missing = [k for k in required if not self.config.get(k)]
        if missing:
            raise EnvironmentError(f"Missing DB config: {', '.join(missing)}")
    
    def _init_pool(self):
        """Initialize connection pool if not already done."""
        if DatabaseService._pool is None:
            try:
                DatabaseService._pool = pooling.MySQLConnectionPool(
                    pool_name="dental_clinic_pool",
                    pool_size=5,
                    pool_reset_session=True,
                    **self.config,
                    autocommit=False,
                    use_unicode=True,
                    charset='utf8mb4'
                )
                logger.info("Connection pool initialized successfully")
            except Error as e:
                logger.error(f"Failed to create connection pool: {e}")
                raise
    
    def get_connection(self):
        """Get a database connection from the pool."""
        try:
            if DatabaseService._pool is None:
                self._init_pool()
            return DatabaseService._pool.get_connection()
        except Error as e:
            logger.error(f"Database connection error: {e}")
            raise
            raise
    
    # ========== SESSION MANAGEMENT ==========
    
    def create_session(self, room_name: str) -> str:
        """
        Create a new session record.
        
        Args:
            room_name: LiveKit room name
            
        Returns:
            session_id
        """
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            INSERT INTO sessions (session_id, room_name, start_time, status)
            VALUES (%s, %s, %s, %s)
            """
            cursor.execute(query, (session_id, room_name, datetime.now(), 'active'))
            conn.commit()
            
            logger.info(f"Created session: {session_id} for room: {room_name}")
            return session_id
            
        except Error as e:
            logger.error(f"Error creating session: {e}")
            raise
        finally:
            cursor.close()
            conn.close()
    
    def end_session(self, session_id: str, duration_seconds: int = None):
        """End an active session."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            UPDATE sessions 
            SET end_time = %s, status = %s, duration_seconds = %s
            WHERE session_id = %s
            """
            cursor.execute(query, (datetime.now(), 'completed', duration_seconds, session_id))
            conn.commit()
            
            logger.info(f"Ended session: {session_id}")
            
        except Error as e:
            logger.error(f"Error ending session: {e}")
        finally:
            cursor.close()
            conn.close()
    
    # ========== USER LOGIN & AUTHENTICATION ==========
    
    def login(self, email: str, password: str):
        """Login user by email and hashed password."""
        conn = self.get_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Hash the password
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        
        # Query user_auth table
        cursor.execute("SELECT id, email FROM user_auth WHERE email = %s AND password = %s", (email, hashed_password))
        user = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        return user
    
    # ========== MESSAGE LOGGING & CONVERSATION ==========
    
    def get_or_create_user(self, phone: str, name: str = None, email: str = None) -> int:
        """
        Get existing user or create new one.
        Uses normalized phone lookup to avoid duplicates from formatting differences.
        
        Args:
            phone: User's phone number (unique identifier)
            name: User's name
            email: User's email
            
        Returns:
            user_id
        """
        try:
            logger.info(f"[GET_OR_CREATE_USER] Starting - phone={phone}, name={name}, email={email}")
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Normalize phone for lookup: digits only
            normalized_phone = ''.join(filter(str.isdigit, phone)) if phone else ""
            logger.info(f"[GET_OR_CREATE_USER] Normalized phone: {normalized_phone}")
            
            # Check if user exists using normalized phone
            logger.info(f"[GET_OR_CREATE_USER] Searching for existing user by normalized phone")
            cursor.execute("""
                SELECT user_id FROM users 
                WHERE REPLACE(REPLACE(REPLACE(phone, '-', ''), ' ', ''), '+', '') = %s
            """, (normalized_phone,))
            result = cursor.fetchone()
            
            if result:
                user_id = result[0]
                logger.info(f"[GET_OR_CREATE_USER] ✓ Found existing user {user_id}, updating last_contact_date")
                # Update last_contact_date
                cursor.execute(
                    "UPDATE users SET last_contact_date = %s WHERE user_id = %s",
                    (datetime.now(), user_id)
                )
                logger.info(f"[GET_OR_CREATE_USER] UPDATE successful, affected rows: {cursor.rowcount}")
                conn.commit()
                logger.info(f"[GET_OR_CREATE_USER] ✓ Transaction committed. Returning existing user_id: {user_id}")
                return user_id
            
            # Create new user
            logger.info(f"[GET_OR_CREATE_USER] User not found, creating new user")
            query = """
            INSERT INTO users (phone, name, email, first_contact_date, last_contact_date)
            VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(query, (phone, name, email, datetime.now(), datetime.now()))
            logger.info(f"[GET_OR_CREATE_USER] INSERT successful, affected rows: {cursor.rowcount}")
            
            conn.commit()
            user_id = cursor.lastrowid
            logger.info(f"[GET_OR_CREATE_USER] ✓ Transaction committed. Created new user {user_id} with phone={phone}, name={name}, email={email}")
            return user_id
            
        except Error as e:
            logger.error(f"[GET_OR_CREATE_USER] ✗ Error managing user: {e}", exc_info=True)
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            logger.info(f"[GET_OR_CREATE_USER] Connection closed")
    
    def update_user(self, user_id: int, **kwargs):
        """Update user information."""
        allowed_fields = ['name', 'email', 'notes']
        fields = {k: v for k, v in kwargs.items() if k in allowed_fields}
        
        if not fields:
            return
        
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            set_clause = ", ".join([f"{k} = %s" for k in fields.keys()])
            values = list(fields.values()) + [user_id]
            
            query = f"UPDATE users SET {set_clause} WHERE user_id = %s"
            cursor.execute(query, values)
            conn.commit()
            
            logger.info(f"Updated user {user_id}: {fields}")
            
        except Error as e:
            logger.error(f"Error updating user: {e}")
        finally:
            cursor.close()
            conn.close()
    
    def upsert_user_contact(self, name: str, phone: str, email: str, calcom_uid: str = None) -> int:
        """
        Insert new user or update last_contact_date for existing user.
        NEVER overwrites existing phone/email (preserves original contact info).
        
        Args:
            name: Patient's full name
            phone: Patient's phone number
            email: Patient's email address
            calcom_uid: Optional CalCom booking UID (not stored, for reference only)
            
        Returns:
            user_id (newly created or existing)
        """
        try:
            logger.info(f"[UPSERT_USER] Starting - name={name}, phone={phone}, email={email}")
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Normalize phone for lookup: digits only
            normalized_phone = ''.join(filter(str.isdigit, phone)) if phone else ""
            logger.info(f"[UPSERT_USER] Normalized phone: {normalized_phone}")
            
            # Try to find existing user by normalized phone
            logger.info(f"[UPSERT_USER] Searching for existing user by normalized phone")
            cursor.execute("""
                SELECT user_id FROM users 
                WHERE REPLACE(REPLACE(REPLACE(phone, '-', ''), ' ', ''), '+', '') = %s
            """, (normalized_phone,))
            result = cursor.fetchone()
            
            if result:
                # User exists - only update last_contact_date, NEVER touch phone/email
                user_id = result[0]
                logger.info(f"[UPSERT_USER] ✓ Found existing user {user_id}, updating last_contact_date")
                cursor.execute(
                    "UPDATE users SET last_contact_date = %s WHERE user_id = %s",
                    (datetime.now(), user_id)
                )
                logger.info(f"[UPSERT_USER] UPDATE successful, affected rows: {cursor.rowcount}")
                conn.commit()
                logger.info(f"[UPSERT_USER] ✓ Transaction committed for existing user {user_id}")
                return user_id
            
            # User does NOT exist - create new user
            logger.info(f"[UPSERT_USER] User not found, creating new user")
            query = """
            INSERT INTO users (phone, name, email, first_contact_date, last_contact_date)
            VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                phone, 
                name.strip() if name else None, 
                email.strip() if email else None, 
                datetime.now(), 
                datetime.now()
            ))
            logger.info(f"[UPSERT_USER] INSERT successful, affected rows: {cursor.rowcount}")
            
            conn.commit()
            user_id = cursor.lastrowid
            logger.info(f"[UPSERT_USER] ✓ Transaction committed. Created new user {user_id} with phone={phone}, name={name}, email={email}")
            return user_id
            
        except Error as e:
            logger.error(f"[UPSERT_USER] ✗ Error upserting user contact: {e}", exc_info=True)
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            logger.info(f"[UPSERT_USER] Connection closed")
    
    def mark_calcom_sync(self, booking_id: str, calcom_uid: str, sync_status: str = "synced"):
        """
        Mark a booking as synced with CalCom and record sync timestamp.
        Enables reconciliation and audit trail between CalCom and DB.
        
        Args:
            booking_id: Database booking ID (internal)
            calcom_uid: CalCom booking UID (unique identifier from API)
            sync_status: Status of sync ('synced', 'cancelled', 'rescheduled', 'pending', 'failed')
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            UPDATE bookings 
            SET calcom_uid = %s, calcom_synced = %s, sync_status = %s
            WHERE booking_id = %s
            """
            cursor.execute(query, (calcom_uid, datetime.now(), sync_status, booking_id))
            conn.commit()
            
            logger.info(f"[SYNC] Marked booking {booking_id} (CalCom UID: {calcom_uid}) as {sync_status}")
            
        except Error as e:
            logger.error(f"[SYNC] Error marking booking sync: {e}")
        finally:
            cursor.close()
            conn.close()
    
    # ========== CONVERSATION LOGGING ==========
    
    def log_message(
        self, 
        session_id: str, 
        speaker: str,  # 'agent' or 'user'
        message_text: str,
        user_id: int = None,
        audio_duration: float = None
    ):
        """
        Log a conversation message.
        
        Args:
            session_id: Session ID
            speaker: 'agent' or 'user'
            message_text: The message content
            user_id: Associated user ID (optional)
            audio_duration: Duration of audio in seconds (optional)
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            INSERT INTO conversation_logs 
            (session_id, user_id, speaker, message_text, timestamp, audio_duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                session_id, user_id, speaker, message_text, datetime.now(), audio_duration
            ))
            conn.commit()
            
        except Error as e:
            logger.error(f"Error logging message: {e}")
        finally:
            cursor.close()
            conn.close()
    
    def link_conversation_logs_to_user(self, session_id: str, user_id: int):
        """
        Retroactively link all conversation logs for a session to a user.
        Called when a user is created during booking to associate all past
        conversation in that session with the user.
        
        Args:
            session_id: Session ID
            user_id: User ID to link to all logs in this session
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            UPDATE conversation_logs 
            SET user_id = %s 
            WHERE session_id = %s AND user_id IS NULL
            """
            cursor.execute(query, (user_id, session_id))
            updated_rows = cursor.rowcount
            conn.commit()
            
            if updated_rows > 0:
                logger.info(f"Linked {updated_rows} conversation logs to user {user_id} for session {session_id}")
            
        except Error as e:
            logger.error(f"Error linking conversation logs to user: {e}")
        finally:
            cursor.close()
            conn.close()
    
    def get_session_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve full conversation for a session."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
            SELECT message_id, speaker, message_text, timestamp, audio_duration_seconds
            FROM conversation_logs
            WHERE session_id = %s
            ORDER BY timestamp ASC
            """
            cursor.execute(query, (session_id,))
            messages = cursor.fetchall()
            return messages
            
        except Error as e:
            logger.error(f"Error retrieving conversation: {e}")
            return []
        finally:
            cursor.close()
            conn.close()
    
    # ========== BOOKING MANAGEMENT ==========
    
    def create_booking(
        self,
        session_id: str,
        user_id: int,
        appointment_start: datetime,
        appointment_end: datetime,
        service_type: str = None,
        notes: str = None,
        calcom_uid: str = None
    ) -> str:
        """
        Create a new booking record.
        
        Args:
            calcom_uid: Cal.com booking UID (important for cancellation/rescheduling)
        
        Returns:
            booking_id
        """
        booking_id = f"book_{uuid.uuid4().hex[:12]}"
        
        try:
            logger.info(f"[CREATE_BOOKING] Starting - session_id={session_id}, user_id={user_id}")
            conn = self.get_connection()
            cursor = conn.cursor()
            
            logger.info(f"[CREATE_BOOKING] Executing INSERT into bookings table")
            query = """
            INSERT INTO bookings 
            (booking_id, session_id, user_id, appointment_start_time, 
             appointment_end_time, service_type, status, notes, calcom_uid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                booking_id, session_id, user_id, appointment_start, 
                appointment_end, service_type, 'confirmed', notes, calcom_uid
            ))
            logger.info(f"[CREATE_BOOKING] INSERT successful, affected rows: {cursor.rowcount}")
            
            # Update user's total bookings
            logger.info(f"[CREATE_BOOKING] Updating users.total_bookings for user_id={user_id}")
            cursor.execute(
                "UPDATE users SET total_bookings = total_bookings + 1 WHERE user_id = %s",
                (user_id,)
            )
            logger.info(f"[CREATE_BOOKING] UPDATE successful, affected rows: {cursor.rowcount}")
            
            logger.info(f"[CREATE_BOOKING] Committing transaction")
            conn.commit()
            logger.info(f"[CREATE_BOOKING] ✓ Transaction committed successfully. Created booking: {booking_id}")
            return booking_id
            
        except Error as e:
            logger.error(f"[CREATE_BOOKING] ✗ Error creating booking: {e}", exc_info=True)
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            logger.info(f"[CREATE_BOOKING] Connection closed")
    
    def get_booking(self, booking_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve booking details."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = "SELECT * FROM bookings WHERE booking_id = %s"
            cursor.execute(query, (booking_id,))
            booking = cursor.fetchone()
            return booking
            
        except Error as e:
            logger.error(f"Error retrieving booking: {e}")
            return None
        finally:
            cursor.close()
            conn.close()
    
    def update_booking_status(
        self, 
        booking_id: str, 
        new_status: str,
        notes: str = None
    ):
        """
        Update booking status.
        
        Args:
            booking_id: Booking ID
            new_status: 'confirmed', 'cancelled', 'completed', 'rescheduled', 'no-show'
            notes: Optional change notes
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
            UPDATE bookings 
            SET status = %s, notes = %s
            WHERE booking_id = %s
            """
            cursor.execute(query, (new_status, notes, booking_id))
            conn.commit()
            
            logger.info(f"Updated booking {booking_id} status to {new_status}")
            
        except Error as e:
            logger.error(f"Error updating booking: {e}")
        finally:
            cursor.close()
            conn.close()
    
    def reschedule_booking(
        self,
        booking_id: str,
        new_appointment_time: datetime,
        reason: str = None
    ):
        """
        Reschedule a booking and log the change.
        
        Args:
            booking_id: Booking ID
            new_appointment_time: New appointment start time
            reason: Reason for rescheduling
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get current booking details
            cursor.execute(
                "SELECT appointment_start_time FROM bookings WHERE booking_id = %s",
                (booking_id,)
            )
            result = cursor.fetchone()
            old_time = result[0] if result else None
            
            # Calculate new end time (30 min default)
            new_end_time = new_appointment_time + __import__('datetime').timedelta(minutes=30)
            
            # Update booking
            query = """
            UPDATE bookings 
            SET appointment_start_time = %s, appointment_end_time = %s, status = %s
            WHERE booking_id = %s
            """
            cursor.execute(query, (
                new_appointment_time, new_end_time, 'rescheduled', booking_id
            ))
            
            # Log history
            history_query = """
            INSERT INTO booking_history 
            (booking_id, old_appointment_time, new_appointment_time, change_reason, changed_by, changed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(history_query, (
                booking_id, old_time, new_appointment_time, reason, 'system', datetime.now()
            ))
            
            conn.commit()
            logger.info(f"Rescheduled booking {booking_id} from {old_time} to {new_appointment_time}")
            
        except Error as e:
            logger.error(f"Error rescheduling booking: {e}")
        finally:
            cursor.close()
            conn.close()
    
    def cancel_booking(self, booking_id: str, reason: str = None):
        """Cancel a booking."""
        self.update_booking_status(booking_id, 'cancelled', reason)
    
    def get_user_bookings(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Get user's appointment history."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
            SELECT b.*, u.name as patient_name, u.email, u.phone 
            FROM bookings b
            JOIN users u ON b.user_id = u.user_id
            WHERE b.user_id = %s 
            ORDER BY b.appointment_start_time DESC 
            LIMIT %s
            """
            cursor.execute(query, (user_id, limit))
            bookings = cursor.fetchall()
            return bookings
            
        except Error as e:
            logger.error(f"Error retrieving user bookings: {e}")
            return []
        finally:
            cursor.close()
            conn.close()
    
    def find_bookings_by_phone(self, phone: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Find all bookings for a patient by phone number.
        Handles various phone formats (with/without dashes, spaces, country codes).
        
        Args:
            phone: Patient's phone number
            limit: Maximum number of bookings to return
            
        Returns:
            List of booking details
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Normalize phone number - keep only digits
            normalized_phone = ''.join(filter(str.isdigit, phone)) if phone else ""
            
            # Try exact match first
            query = """
                SELECT b.*, u.name as patient_name, u.email, u.phone
                FROM bookings b
                JOIN users u ON b.user_id = u.user_id
                WHERE u.phone = %s
                AND b.status IN ('confirmed', 'completed')
                ORDER BY b.appointment_start_time DESC
                LIMIT %s
            """
            cursor.execute(query, (phone, limit))
            bookings = cursor.fetchall()
            
            # If not found and phone has digits, try matching just the digits
            if not bookings and normalized_phone:
                # Try to match the last N digits
                digits_only_query = """
                    SELECT b.*, u.name as patient_name, u.email, u.phone
                    FROM bookings b
                    JOIN users u ON b.user_id = u.user_id
                    WHERE REPLACE(REPLACE(REPLACE(u.phone, '-', ''), ' ', ''), '+', '') LIKE %s
                    AND b.status IN ('confirmed', 'completed')
                    ORDER BY b.appointment_start_time DESC
                    LIMIT %s
                """
                cursor.execute(digits_only_query, (f"%{normalized_phone}", limit))
                bookings = cursor.fetchall()
            
            return bookings
            
        except Error as e:
            logger.error(f"Error finding bookings by phone: {e}")
            return []
        finally:
            cursor.close()
            conn.close()
    
    def find_user_by_phone_fuzzy(self, phone: str) -> Optional[Dict[str, Any]]:
        """
        Find a user by phone number with fuzzy matching.
        Useful for providing suggestions when exact match fails.
        
        Args:
            phone: Patient's phone number
            
        Returns:
            User details if found, None otherwise
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Normalize phone
            normalized_phone = ''.join(filter(str.isdigit, phone)) if phone else ""
            
            # Try exact match first
            query = "SELECT user_id, name, phone, email FROM users WHERE phone = %s"
            cursor.execute(query, (phone,))
            user = cursor.fetchone()
            
            if user:
                return user
            
            # Try fuzzy match with digits
            if normalized_phone:
                fuzzy_query = """
                    SELECT user_id, name, phone, email FROM users
                    WHERE REPLACE(REPLACE(REPLACE(phone, '-', ''), ' ', ''), '+', '') LIKE %s
                    LIMIT 1
                """
                cursor.execute(fuzzy_query, (f"%{normalized_phone[-10:]}", ))  # Last 10 digits
                user = cursor.fetchone()
                return user
            
            return None
            
        except Error as e:
            logger.error(f"Error finding user by phone (fuzzy): {e}")
            return None
        finally:
            cursor.close()
            conn.close()
    
    def find_bookings_by_email(self, email: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Find all bookings for a patient by email.
        
        Args:
            email: Patient's email address
            limit: Maximum number of bookings to return
            
        Returns:
            List of booking details
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT b.*, u.name as patient_name, u.email, u.phone
                FROM bookings b
                JOIN users u ON b.user_id = u.user_id
                WHERE u.email = %s
                AND b.status IN ('confirmed', 'completed')
                ORDER BY b.appointment_start_time DESC
                LIMIT %s
            """
            cursor.execute(query, (email, limit))
            bookings = cursor.fetchall()
            return bookings
            
        except Error as e:
            logger.error(f"Error finding bookings by email: {e}")
            return []
        finally:
            cursor.close()
            conn.close()
    
    def find_bookings_by_name(self, patient_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Find all bookings for a patient by name.
        
        Args:
            patient_name: Patient's full name
            limit: Maximum number of bookings to return
            
        Returns:
            List of booking details
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT b.*, u.name as patient_name, u.email, u.phone
                FROM bookings b
                JOIN users u ON b.user_id = u.user_id
                WHERE LOWER(u.name) LIKE LOWER(%s)
                AND b.status IN ('confirmed', 'completed')
                ORDER BY b.appointment_start_time DESC
                LIMIT %s
            """
            cursor.execute(query, (f"%{patient_name}%", limit))
            bookings = cursor.fetchall()
            return bookings
            
        except Error as e:
            logger.error(f"Error finding bookings by name: {e}")
            return []
        finally:
            cursor.close()
            conn.close()
    
    def find_booking_by_name_and_time(self, patient_name: str, appointment_time: str) -> Optional[Dict[str, Any]]:
        """
        Find a booking by patient name and appointment time.
        
        Args:
            patient_name: Patient's name
            appointment_time: Appointment time in ISO format or readable format
            
        Returns:
            Booking details if found, None otherwise
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Parse the appointment time to handle various formats
            try:
                appointment_dt = datetime.fromisoformat(appointment_time.replace("Z", "+00:00"))
            except:
                # Try alternative format
                try:
                    from zoneinfo import ZoneInfo
                    appointment_dt = datetime.strptime(appointment_time, "%Y-%m-%d %H:%M")
                    appointment_dt = appointment_dt.replace(tzinfo=ZoneInfo("Asia/Calcutta"))
                except:
                    logger.warning(f"Could not parse appointment time: {appointment_time}")
                    return None
            
            # Query bookings matching the patient name and appointment time
            # Allow a 5-minute window for time matching
            time_start = appointment_dt - timedelta(minutes=5)
            time_end = appointment_dt + timedelta(minutes=5)
            
            query = """
                SELECT b.*, u.name as patient_name, u.email, u.phone 
                FROM bookings b
                JOIN users u ON b.user_id = u.user_id
                WHERE LOWER(u.name) = LOWER(%s)
                AND b.appointment_start_time BETWEEN %s AND %s
                AND b.status != 'cancelled'
                ORDER BY b.booking_timestamp DESC
                LIMIT 1
            """
            
            cursor.execute(query, (patient_name, time_start, time_end))
            result = cursor.fetchone()
            
            if result:
                logger.info(f"Found booking for {patient_name} at {appointment_time}")
                return result
            else:
                logger.info(f"No booking found for {patient_name} at {appointment_time}")
                return None
                
        except Error as e:
            logger.error(f"Error finding booking by name and time: {e}")
            return None
        finally:
            cursor.close()
            conn.close()
    
    # ========== ANALYTICS ==========
    
    def log_booking_to_history(self, booking_id: str, appointment_start_time: datetime = None, reason: str = "initial_booking"):
        """
        Create a booking history record for audit trail.
        booking_history tracks changes to appointments.
        Uses: history_id (auto), booking_id, action (reason), appointment_start_time, notes, changed_at
        """
        conn = None
        cursor = None
        try:
            logger.info(f"[LOG_HISTORY] Starting for booking_id={booking_id}, reason={reason}")
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # If appointment_start_time not provided, fetch it from bookings table
            if not appointment_start_time:
                logger.info(f"[LOG_HISTORY] Fetching appointment_start_time from bookings table")
                cursor.execute(
                    "SELECT appointment_start_time FROM bookings WHERE booking_id = %s",
                    (booking_id,)
                )
                result = cursor.fetchone()
                if result:
                    appointment_start_time = result[0]
                    logger.info(f"[LOG_HISTORY] ✓ Retrieved appointment_start_time: {appointment_start_time}")
                else:
                    logger.warning(f"[LOG_HISTORY] ✗ Could not find appointment time for booking {booking_id}")
                    return
            
            # Insert into booking_history using correct column names
            logger.info(f"[LOG_HISTORY] Inserting into booking_history table")
            query = """
            INSERT INTO booking_history 
            (booking_id, action, appointment_start_time, changed_at, notes)
            VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(query, (
                booking_id,
                reason,  # action column stores the reason/action type
                appointment_start_time,
                datetime.now(),
                f"Booking {reason} via AI agent"
            ))
            logger.info(f"[LOG_HISTORY] INSERT successful, affected rows: {cursor.rowcount}")
            
            conn.commit()
            logger.info(f"[LOG_HISTORY] ✓ Transaction committed. Logged booking {booking_id} to history with reason: {reason}")
            
        except Error as e:
            logger.error(f"[LOG_HISTORY] ✗ Error logging booking to history: {e}", exc_info=True)
            if conn:
                conn.rollback()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            logger.info(f"[LOG_HISTORY] Connection closed")
            cursor.close()
            conn.close()
    
    def update_session_analytics(self):
        """
        Update daily session analytics aggregated from sessions and bookings tables.
        Called after sessions end to update cumulative stats for the day.
        """
        conn = None
        cursor = None
        try:
            logger.info(f"[ANALYTICS] Starting session analytics update")
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            today = datetime.now().date()
            logger.info(f"[ANALYTICS] Updating for date: {today}")
            
            # Get session stats for today
            logger.info(f"[ANALYTICS] Fetching session stats")
            cursor.execute("""
                SELECT 
                    COUNT(DISTINCT session_id) as total_sessions,
                    AVG(duration_seconds) as avg_duration
                FROM sessions 
                WHERE DATE(start_time) = %s AND status = 'completed'
            """, (today,))
            session_result = cursor.fetchone()
            logger.info(f"[ANALYTICS] Session result: {session_result}")
            
            # Get unique users from bookings for today
            logger.info(f"[ANALYTICS] Fetching unique users from bookings")
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) as total_users
                FROM bookings
                WHERE DATE(created_at) = %s
            """, (today,))
            users_result = cursor.fetchone()
            logger.info(f"[ANALYTICS] Users result: {users_result}")
            
            # Get booking stats for today
            logger.info(f"[ANALYTICS] Fetching booking stats")
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_bookings,
                    SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) as successful_bookings,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled_bookings
                FROM bookings
                WHERE DATE(created_at) = %s
            """, (today,))
            booking_result = cursor.fetchone()
            logger.info(f"[ANALYTICS] Booking result: {booking_result}")
            
            # Check if record exists for today
            logger.info(f"[ANALYTICS] Checking for existing analytics record")
            cursor.execute(
                "SELECT analytics_id FROM session_analytics WHERE date = %s",
                (today,)
            )
            existing = cursor.fetchone()
            logger.info(f"[ANALYTICS] Existing record: {existing}")
            
            total_sessions = session_result['total_sessions'] or 0
            total_users = users_result['total_users'] or 0
            avg_duration = session_result['avg_duration'] or 0
            total_bookings = booking_result['total_bookings'] or 0
            successful_bookings = booking_result['successful_bookings'] or 0
            cancelled_bookings = booking_result['cancelled_bookings'] or 0
            
            logger.info(f"[ANALYTICS] Calculated stats: sessions={total_sessions}, users={total_users}, bookings={total_bookings}")
            
            if existing:
                # Update existing record
                logger.info(f"[ANALYTICS] Updating existing record")
                query = """
                UPDATE session_analytics 
                SET total_sessions = %s, total_users = %s, total_bookings = %s,
                    successful_bookings = %s, cancelled_bookings = %s,
                    avg_session_duration_seconds = %s
                WHERE date = %s
                """
                cursor.execute(query, (
                    total_sessions, total_users, total_bookings,
                    successful_bookings, cancelled_bookings, avg_duration, today
                ))
                logger.info(f"[ANALYTICS] UPDATE successful, affected rows: {cursor.rowcount}")
            else:
                # Insert new record
                logger.info(f"[ANALYTICS] Inserting new record")
                query = """
                INSERT INTO session_analytics 
                (date, total_sessions, total_users, total_bookings, successful_bookings, 
                 cancelled_bookings, avg_session_duration_seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(query, (
                    today, total_sessions, total_users, total_bookings,
                    successful_bookings, cancelled_bookings, avg_duration
                ))
                logger.info(f"[ANALYTICS] INSERT successful, affected rows: {cursor.rowcount}")
            
            conn.commit()
            logger.info(f"[ANALYTICS] ✓ Transaction committed. Updated analytics for {today}: "
                       f"sessions={total_sessions}, users={total_users}, bookings={total_bookings}")
            
        except Error as e:
            logger.error(f"[ANALYTICS] ✗ Error updating session analytics: {e}", exc_info=True)
            if conn:
                conn.rollback()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            logger.info(f"[ANALYTICS] Connection closed")
            cursor.close()
            conn.close()
    
    def get_session_stats(self, date: datetime = None) -> Dict[str, Any]:
        """Get statistics for a specific date."""
        date = date or datetime.now().date()
        
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
            SELECT 
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(DISTINCT user_id) as total_users,
                AVG(duration_seconds) as avg_duration
            FROM sessions 
            WHERE DATE(start_time) = %s AND status = 'completed'
            """
            cursor.execute(query, (date,))
            result = cursor.fetchone()
            
            # Booking stats
            booking_query = """
            SELECT 
                COUNT(*) as total_bookings,
                SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled
            FROM bookings
            WHERE DATE(booking_timestamp) = %s
            """
            cursor.execute(booking_query, (date,))
            booking_result = cursor.fetchone()
            
            return {
                **result,
                **booking_result
            }
            
        except Error as e:
            logger.error(f"Error retrieving stats: {e}")
            return {}
        finally:
            cursor.close()
            conn.close()


# Singleton instance for easy access
_db = None

# Singleton getter function
def get_db() -> DatabaseService:
    """Get or create the database service singleton."""
    global _db
    if _db is None:
        _db = DatabaseService()
    return _db
