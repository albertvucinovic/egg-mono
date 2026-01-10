import os
import json
import asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Any
from .core import Task, Result

# --- Configuration ---
class Config:
    EGG_DB_PATH: str = "eggthreads.db"
    MOCK_MODE: bool = True

    @classmethod
    def load(cls):
        cls.MOCK_MODE = os.environ.get("EGGFLOW_REAL_LLM", "false").lower() != "true"
        cls.EGG_DB_PATH = os.environ.get("EGG_DB_PATH", "eggthreads.db")

Config.load()

# --- EggThreads Integration Helpers ---
# We use a singleton or global access because Task.run() signature is fixed.

try:
    from eggthreads import db as egg_db_module
    from eggthreads import api as egg_api
    from eggthreads import runner as egg_runner
    from eggthreads.tools import create_default_tools
    EGGTHREADS_INSTALLED = True
except ImportError:
    EGGTHREADS_INSTALLED = False

def get_egg_db():
    if not EGGTHREADS_INSTALLED: return None
    return egg_db_module.ThreadsDB(Config.EGG_DB_PATH)

async def _run_scheduler_until_idle(tid: str):
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE: return
    db = get_egg_db()
    # We create scheduler
    scheduler = egg_runner.SubtreeScheduler(db, tid, tools=create_default_tools())
    task = asyncio.create_task(scheduler.run_forever())
    try:
        await egg_api.wait_subtree_idle(db, tid)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

async def _collect_artifacts(tid: str, files: List[str]):
    if not files: return {}
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE:
        return {f: f"Mock content of {f}" for f in files}
    
    db = get_egg_db()
    wd = egg_api.get_thread_working_directory(db, tid)
    out = {}
    for f in files:
        p = wd / f
        if p.exists():
            out[f] = p.read_text(errors='replace')
    return out

async def _get_last_content(tid: str):
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE:
        return f"Mock Response for {tid}"
    db = get_egg_db()
    snap = json.loads(egg_api.create_snapshot(db, tid))
    msgs = snap.get("messages", [])
    return msgs[-1].get("content", "") if msgs else ""

# --- Tasks ---

@dataclass
class CreateThread(Task):
    prompt: str
    model_key: Optional[str] = None
    system_prompt: Optional[str] = None
    seed: int = 0
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> Any:
        # Mock Mode
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            print(f"[Mock] CreateThread: {self.prompt[:30]}...")
            return f"Mock Reply to '{self.prompt}'"
            
        # Real Mode
        db = get_egg_db()
        tid = egg_api.create_root_thread(db, initial_model_key=self.model_key)
        if self.system_prompt:
             # eggthreads doesn't have explicit system prompt arg in create_root_thread easily accessible 
             # without modifying events manually, but we can assume normal flow or inject as first msg?
             # For now, just user prompt.
             pass
        egg_api.append_message(db, tid, "user", self.prompt)
        
        await _run_scheduler_until_idle(tid)
        return await _get_last_content(tid)

@dataclass
class ContinueThread(Task):
    thread_id: str
    content: str
    role: str = "user"
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> Any:
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            print(f"[Mock] ContinueThread {self.thread_id}: {self.content[:30]}...")
            return f"Mock Reply to '{self.content}'"

        db = get_egg_db()
        # Idempotency check could go here if we had access to previous result, 
        # but Task.run is stateless. 
        # However, checking the DB for the last message is safe.
        snap = json.loads(egg_api.create_snapshot(db, self.thread_id))
        msgs = snap.get("messages", [])
        last = msgs[-1] if msgs else None
        
        should_append = True
        if last and last.get('role') == self.role and last.get('content') == self.content:
            should_append = False
        
        if should_append:
            egg_api.append_message(db, self.thread_id, self.role, self.content)
            
        await _run_scheduler_until_idle(self.thread_id)
        return await _get_last_content(self.thread_id)

@dataclass
class ForkThread(Task):
    source_thread_id: str
    
    async def run(self) -> Any:
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            return f"{self.source_thread_id}_fork"
            
        db = get_egg_db()
        await egg_api.wait_subtree_idle(db, self.source_thread_id)
        return egg_api.duplicate_thread(db, self.source_thread_id)

