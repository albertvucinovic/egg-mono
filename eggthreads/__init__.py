# Aggregator package for local development without installation.
# Re-export the real package located in eggthreads/eggthreads
from .eggthreads import *  # type: ignore
from .eggthreads.event_watcher import EventWatcher  # type: ignore