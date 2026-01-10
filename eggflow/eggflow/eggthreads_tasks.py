import os
import json
import asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Any, Dict
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
        return f"Mock Reply to prompt"
    db = get_egg_db()
    snap = json.loads(egg_api.create_snapshot(db, tid))
    msgs = snap.get("messages", [])
    return msgs[-1].get("content", "") if msgs else ""

# --- Tasks ---

@dataclass
class ThreadResult:
    """Standard return type for Thread tasks to pass ID and Content back."""
    thread_id: str
    content: str
    artifacts: Dict[str, str] = field(default_factory=dict)

@dataclass
class CreateThread(Task):
    prompt: str
    model_key: Optional[str] = None
    system_prompt: Optional[str] = None
    seed: int = 0
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> ThreadResult:
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            tid = f"mock_{abs(hash(self.prompt))}"
            artifacts = await _collect_artifacts(tid, self.output_files)
            return ThreadResult(thread_id=tid, content=f"Mock Reply to '{self.prompt}'", artifacts=artifacts)
            
        db = get_egg_db()
        tid = egg_api.create_root_thread(db, initial_model_key=self.model_key)
        egg_api.append_message(db, tid, "user", self.prompt)
        await _run_scheduler_until_idle(tid)
        
        content = await _get_last_content(tid)
        artifacts = await _collect_artifacts(tid, self.output_files)
        return ThreadResult(thread_id=tid, content=content, artifacts=artifacts)

@dataclass
class ContinueThread(Task):
    thread_id: str
    content: str
    role: str = "user"
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> ThreadResult:
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            artifacts = await _collect_artifacts(self.thread_id, self.output_files)
            return ThreadResult(thread_id=self.thread_id, content=f"Mock Reply to '{self.content}'", artifacts=artifacts)

        db = get_egg_db()
        snap = json.loads(egg_api.create_snapshot(db, self.thread_id))
        msgs = snap.get("messages", [])
        last = msgs[-1] if msgs else None
        
        if not (last and last.get('role') == self.role and last.get('content') == self.content):
            egg_api.append_message(db, self.thread_id, self.role, self.content)
            
        await _run_scheduler_until_idle(self.thread_id)
        content = await _get_last_content(self.thread_id)
        artifacts = await _collect_artifacts(self.thread_id, self.output_files)
        return ThreadResult(thread_id=self.thread_id, content=content, artifacts=artifacts)

@dataclass
class ForkThread(Task):
    source_thread_id: str
    
    async def run(self) -> str:
        # Returns just the new Thread ID string
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            return f"{self.source_thread_id}_fork"
            
        db = get_egg_db()
        await egg_api.wait_subtree_idle(db, self.source_thread_id)
        return egg_api.duplicate_thread(db, self.source_thread_id)
