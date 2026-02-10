"""
Conversation Memory Model

Simple Memory (Memo) System for Agent Conversation Context.
Stores only essential information needed across conversation turns.
"""

from contextvars import ContextVar
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Context variable to store conversation memory
_conversation_memo: ContextVar[Optional[Dict[str, Any]]] = ContextVar('conversation_memo', default=None)


class ConversationMemo:
    """Simple conversation memory to avoid asking same questions twice."""
    
    def __init__(self):
        """Initialize empty memo."""
        self.data: Dict[str, Any] = {
            'patient_email': None,
            'patient_name': None,
            'patient_phone': None,
            'preferred_doctor': None,
            'appointment_reason': None,
            'appointments': [],
            'current_appointment': None,
            'action': None,
            'session_start': datetime.now().isoformat(),
            'user_statements': []
        }
    
    def update_patient_email(self, email: str) -> None:
        """Store patient email to avoid asking twice."""
        if email:
            self.data['patient_email'] = email
            logger.debug(f"Memo: Stored patient email")
    
    def update_patient_name(self, name: str) -> None:
        """Store patient name."""
        if name:
            self.data['patient_name'] = name
            logger.debug(f"Memo: Stored patient name: {name}")
    
    def update_patient_phone(self, phone: str) -> None:
        """Store patient phone."""
        if phone:
            self.data['patient_phone'] = phone
            logger.debug(f"Memo: Stored patient phone")
    
    def set_appointments(self, appointments: List[Dict[str, Any]]) -> None:
        """Store all appointments found for patient."""
        if appointments:
            self.data['appointments'] = appointments
            logger.debug(f"Memo: Stored {len(appointments)} appointment(s)")
    
    def set_current_appointment(self, appointment: Dict[str, Any]) -> None:
        """Store current appointment being discussed."""
        if appointment:
            self.data['current_appointment'] = appointment
            logger.debug(f"Memo: Set current appointment")
    
    def set_action(self, action: str) -> None:
        """Set what user wants to do: 'book', 'reschedule', 'cancel', etc."""
        if action:
            self.data['action'] = action
            logger.debug(f"Memo: Set action to {action}")
    
    def add_user_statement(self, statement: str) -> None:
        """Store important user statements for context."""
        if statement:
            self.data['user_statements'].append({
                'text': statement,
                'timestamp': datetime.now().isoformat()
            })
            logger.debug(f"Memo: Added user statement")
    
    def set_preferred_doctor(self, doctor: str) -> None:
        """Store preferred doctor for appointment."""
        if doctor:
            self.data['preferred_doctor'] = doctor
            logger.debug(f"Memo: Set preferred doctor: {doctor}")
    
    def set_appointment_reason(self, reason: str) -> None:
        """Store appointment reason or service type."""
        if reason:
            self.data['appointment_reason'] = reason
            logger.debug(f"Memo: Set appointment reason: {reason}")
    
    def needs_appointment_reason(self) -> bool:
        """Check if we still need to ask for appointment reason."""
        return not self.data.get('appointment_reason')
    
    def needs_doctor_selection(self) -> bool:
        """Check if we need to ask for doctor preference (only if multiple doctors)."""
        from ..config.prompts import should_ask_for_doctor
        if not should_ask_for_doctor():
            return False  # Single doctor clinic - no selection needed
        return not self.data.get('preferred_doctor')
    
    def get_preferred_doctor(self) -> Optional[str]:
        """Retrieve stored preferred doctor."""
        return self.data['preferred_doctor']
    
    def get_appointment_reason(self) -> Optional[str]:
        """Retrieve stored appointment reason."""
        return self.data['appointment_reason']
    
    def get_patient_email(self) -> Optional[str]:
        """Retrieve stored patient email."""
        return self.data['patient_email']
    
    def get_patient_name(self) -> Optional[str]:
        """Retrieve stored patient name."""
        return self.data['patient_name']
    
    def get_patient_phone(self) -> Optional[str]:
        """Retrieve stored patient phone."""
        return self.data['patient_phone']
    
    def get_appointments(self) -> List[Dict[str, Any]]:
        """Get all stored appointments."""
        return self.data['appointments']
    
    def get_current_appointment(self) -> Optional[Dict[str, Any]]:
        """Get current appointment being discussed."""
        return self.data['current_appointment']
    
    def get_action(self) -> Optional[str]:
        """Get current action."""
        return self.data['action']
    
    def has_email(self) -> bool:
        """Check if we already have patient email."""
        return self.data['patient_email'] is not None
    
    def has_appointments(self) -> bool:
        """Check if we already fetched appointments."""
        return len(self.data['appointments']) > 0
    
    def clear(self) -> None:
        """Clear all memo data (for new conversation or session end)."""
        self.__init__()
        logger.debug("Memo: Cleared all data")
    
    def to_dict(self) -> Dict[str, Any]:
        """Get memo as dictionary for logging/debugging."""
        return self.data.copy()
    
    def get_summary(self) -> str:
        """Get human-readable summary of memo content."""
        summary = []
        
        if self.data['patient_email']:
            summary.append(f"Email: {self.data['patient_email']}")
        
        if self.data['patient_name']:
            summary.append(f"Name: {self.data['patient_name']}")
        
        if self.data['patient_phone']:
            summary.append(f"Phone: {self.data['patient_phone']}")
        
        if self.data['preferred_doctor']:
            summary.append(f"Doctor: {self.data['preferred_doctor']}")
        
        if self.data['appointment_reason']:
            summary.append(f"Reason: {self.data['appointment_reason']}")
        
        if self.data['appointments']:
            summary.append(f"Appointments: {len(self.data['appointments'])}")
        
        if self.data['action']:
            summary.append(f"Action: {self.data['action']}")
        
        return " | ".join(summary) if summary else "Empty memo"


def get_memo() -> ConversationMemo:
    """Get current conversation memo, creating if needed."""
    memo = _conversation_memo.get()
    if memo is None:
        memo = ConversationMemo()
        _conversation_memo.set(memo)
    return memo


def initialize_memo() -> ConversationMemo:
    """Initialize new memo for session start."""
    memo = ConversationMemo()
    _conversation_memo.set(memo)
    logger.info("Initialized new conversation memo")
    return memo


def clear_memo() -> None:
    """Clear memo (for session end or reset)."""
    global _conversation_memo
    _conversation_memo.set(None)
    logger.info("Memo cleared - conversation context reset")


def get_memo_context_for_prompt() -> str:
    """
    Generate string for injection into system prompt to remind agent 
    of remembered information. This helps agent avoid asking duplicate questions.
    """
    memo = get_memo()
    
    context_parts = []
    
    # Patient contact information
    if memo.has_email():
        context_parts.append(
            f"✓ Patient's email: {memo.get_patient_email()} (DON'T ask again)"
        )
    
    if memo.get_patient_name():
        context_parts.append(
            f"✓ Patient's name: {memo.get_patient_name()} (DON'T ask again)"
        )
    
    if memo.get_patient_phone():
        context_parts.append(
            f"✓ Patient's phone: {memo.get_patient_phone()} (DON'T ask again)"
        )
    
    # Appointment preferences
    if memo.get_appointment_reason():
        context_parts.append(
            f"✓ Appointment reason: {memo.get_appointment_reason()} (DON'T ask again)"
        )
    
    if memo.get_preferred_doctor():
        context_parts.append(
            f"✓ Preferred doctor: {memo.get_preferred_doctor()} (DON'T ask again)"
        )
    
    # Appointment status
    if memo.has_appointments():
        apts = memo.get_appointments()
        context_parts.append(
            f"✓ Found {len(apts)} existing appointment(s) (already shown to patient)"
        )
    
    if memo.get_current_appointment():
        context_parts.append(
            f"✓ Current appointment selected (details stored)"
        )
    
    if memo.get_action():
        context_parts.append(
            f"✓ Current task: {memo.get_action()}"
        )
    
    if context_parts:
        header = "IMPORTANT - Information Already Collected (DO NOT ask for these again):"
        return f"{header}\n" + "\n".join(context_parts)
    
    return ""
