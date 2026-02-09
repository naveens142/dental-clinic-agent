"""
Dental Clinic Agent Source Code Package

Organized structure for LiveKit Cloud deployment:
- config: Configuration and prompts
- models: Data models (conversation memory)
- services: Business logic (database, cal.com)
- tools: Agent function tools
"""

__version__ = "1.0.0"
__author__ = "ToothFairy Dental Clinic"

from .config import PromptConfig
from .models import ConversationMemo, get_memo, initialize_memo, clear_memo

__all__ = [
    'PromptConfig',
    'ConversationMemo',
    'get_memo',
    'initialize_memo',
    'clear_memo'
]
