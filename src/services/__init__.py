"""
Services package - Core business logic services
"""

from .database import DatabaseService

# Singleton getter function
def get_db() -> DatabaseService:
    """Get or create the database service singleton."""
    try:
        from .database import _db as _db_instance
        if _db_instance is None:
            return DatabaseService()
        return _db_instance
    except:
        return DatabaseService()

__all__ = [
    'DatabaseService',
    'get_db'
]

