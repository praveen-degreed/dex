"""Long-lived objects shared by the flows. Tests replace these attributes with fakes or temp-dir instances."""
from . import config
from .policy import Policy
from .qa import SessionStore

policy = Policy(config.DATA, config.OWNER)
sessions = SessionStore(config.SESSIONS_FILE)
