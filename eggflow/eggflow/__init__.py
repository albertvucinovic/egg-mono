import sqlite3
import json
import hashlib
import pickle
import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Union

# --- Imports with Graceful Fallback ---
try:
    from eggthreads import db as egg_db
    from eggthreads import api as egg_api
    from eggthreads import runner as egg_runner
    from eggthreads.tools import create_default_tools
    EGGTHREADS_AVAILABLE = True
except ImportError:
    EGGTHREADS_AVAILABLE = False
    print("Warning: eggthreads not found. Running in mock mode.")

# --- 1. Result Monad ---

@dataclass
class Result:
    value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    @property
    def is_success(self) -> bool: return self.error is None
    
    def unwrap(self):
        if self.error: raise Exception(self.error)
        return self.value

# --- 2. Task Specifications ---

@dataclass
class TaskSpec:
    def get_cache_key(self) -> str:
        """
        Default cache key generation based on the class name and 
        serialized dictionary of fields.
        """
        data = self.__dict__.copy()
        s = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(f"{self.__class__.__name__}:{s}".encode()).hexdigest()

    async def run(self) -> Any:
        raise NotImplementedError

@dataclass
class CreateThread(TaskSpec):
    """
    Starts a brand new thread with an initial user prompt.
    Returns the assistant's response.
    """
    prompt: str
    model_key: Optional[str] = None
    system_prompt: Optional[str] = None
    seed: int = 0 # Use to vary outputs for the same prompt

    async def run(self): pass # Handled by Executor

@dataclass
class ContinueThread(TaskSpec):
    """
    Continues an existing thread by appending a message.
    Returns the assistant's response.
    """
    thread_id: str
    content: str
    role: str = "user"
    
    async def run(self): pass # Handled by Executor

@dataclass
class ForkThread(TaskSpec):
    """
    Creates an independent copy of an existing thread (and its history).
    Useful for branching strategies (Tree of Thoughts, etc.).
    Returns the new thread_id.
    """
    source_thread_id: str
    
    async def run(self): pass # Handled by Executor

# --- 3. Persistence Layer ---

class JobStore:
    def __init__(self, db_path: str = "eggflow.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                cache_key TEXT PRIMARY KEY,
                spec_blob BLOB,
                status TEXT,
                result_blob BLOB,
                external_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def get(self, key): 
        return self.conn.execute("SELECT * FROM jobs WHERE cache_key=?", (key,)).fetchone()

    def create(self, key, spec):
        try:
            self.conn.execute("INSERT INTO jobs (cache_key, spec_blob, status) VALUES (?, ?, ?)",
                              (key, pickle.dumps(spec), "PENDING"))
            self.conn.commit()
        except sqlite3.IntegrityError: pass

    def update(self, key, status, result=None, external_id=None):
        if result:
            self.conn.execute("UPDATE jobs SET status=?, result_blob=?, updated_at=CURRENT_TIMESTAMP WHERE cache_key=?",
                              (status, pickle.dumps(result), key))
        elif external_id:
            self.conn.execute("UPDATE jobs SET status=?, external_id=?, updated_at=CURRENT_TIMESTAMP WHERE cache_key=?",
                              (status, external_id, key))
        else:
            self.conn.execute("UPDATE jobs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE cache_key=?", (status, key))
        self.conn.commit()

# --- 4. Executor Engine ---

class EggFlowExecutor:
    def __init__(self, store: JobStore, egg_db_path: str = "eggthreads.db"):
        self.store = store
        self.egg_db = egg_db.ThreadsDB(egg_db_path) if EGGTHREADS_AVAILABLE else None
        # Ensure DB tables exist if we have the DB object
        if self.egg_db:
            with self.egg_db.conn: pass 

    async def run(self, spec: Union[TaskSpec, List[TaskSpec]]) -> Union[Result, List[Result]]:
        """
        Main entry point. Handles single specs or lists for parallel execution.
        """
        if isinstance(spec, list):
            return await asyncio.gather(*(self._execute_task(s) for s in spec))
        return await self._execute_task(spec)

    async def _execute_task(self, spec: TaskSpec) -> Result:
        key = spec.get_cache_key()
        row = self.store.get(key)
        
        # 1. Cache Hit
        if row and row['status'] == "COMPLETED":
            try:
                return pickle.loads(row['result_blob'])
            except Exception:
                pass # Corrupt or unpickling error, rerun
        
        # 2. Resume / New
        if not row: 
            self.store.create(key, spec)
        else:
            # If it was running but process died, we treat as a resume/rerun
            self.store.update(key, "RUNNING")

        try:
            if isinstance(spec, CreateThread):
                return await self._handle_create_thread(spec, key, row)
            elif isinstance(spec, ContinueThread):
                return await self._handle_continue_thread(spec, key)
            elif isinstance(spec, ForkThread):
                return await self._handle_fork_thread(spec, key)
            else:
                return await self._handle_generic(spec, key)
        except Exception as e:
            err = Result(error=str(e))
            self.store.update(key, "FAILED", result=err)
            return err

    async def _handle_generic(self, spec: TaskSpec, key: str) -> Result:
        # Standard generator driving logic for DAGs
        gen = spec.run()
        final_val = None
        
        if inspect.isgenerator(gen):
            try:
                # Prime
                to_yield = next(gen)
                while True:
                    # Recursive execution of yielded task(s)
                    res = await self.run(to_yield)
                    # Send result back into generator
                    to_yield = gen.send(res)
            except StopIteration as e:
                final_val = e.value
        elif inspect.iscoroutine(gen):
            final_val = await gen
        else:
            final_val = gen

        res = Result(value=final_val)
        self.store.update(key, "COMPLETED", result=res)
        return res

    # --- EggThread Integration Logic ---

    async def _run_scheduler(self, tid):
        """Runs the thread until it is idle (no running tools, no streaming text)."""
        if not EGGTHREADS_AVAILABLE: return
        
        # 1. Start the scheduler/runner
        scheduler = egg_runner.SubtreeScheduler(self.egg_db, tid, tools=create_default_tools())
        task = asyncio.create_task(scheduler.run_forever())
        
        # 2. Wait for the thread (and subtree) to settle
        try:
            await egg_api.wait_subtree_idle(self.egg_db, tid)
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

    async def _get_last_message(self, tid):
        if not EGGTHREADS_AVAILABLE: return "Mock Response"
        snap = json.loads(egg_api.create_snapshot(self.egg_db, tid))
        msgs = snap.get("messages", [])
        return msgs[-1].get("content", "") if msgs else ""

    async def _handle_create_thread(self, spec: CreateThread, key: str, row) -> Result:
        if row and row['external_id']:
            tid = row['external_id'] # Resume existing thread
        else:
            if not EGGTHREADS_AVAILABLE: tid = f"mock_thread_{key[:8]}"
            else:
                tid = egg_api.create_root_thread(self.egg_db, initial_model_key=spec.model_key)
                egg_api.append_message(self.egg_db, tid, "user", spec.prompt)
            self.store.update(key, "RUNNING", external_id=tid)

        await self._run_scheduler(tid)
        content = await self._get_last_message(tid)
        
        res = Result(value=content, metadata={"thread_id": tid})
        self.store.update(key, "COMPLETED", result=res)
        return res

    async def _handle_continue_thread(self, spec: ContinueThread, key: str) -> Result:
        if not EGGTHREADS_AVAILABLE:
            res = Result(value=f"Mock Reply to: {spec.content}", metadata={"thread_id": spec.thread_id})
            self.store.update(key, "COMPLETED", result=res)
            return res

        tid = spec.thread_id
        
        # Idempotency Check: 
        # Has the message already been appended?
        snap = json.loads(egg_api.create_snapshot(self.egg_db, tid))
        msgs = snap.get("messages", [])
        last_msg = msgs[-1] if msgs else None
        
        # If the last message is NOT what we want to send, append it.
        # Note: This is a simple check. Robust logic might check IDs or content more strictly.
        is_already_appended = False
        if last_msg and last_msg.get('role') == spec.role and last_msg.get('content') == spec.content:
            is_already_appended = True
            
        if not is_already_appended:
            egg_api.append_message(self.egg_db, tid, spec.role, spec.content)
        
        await self._run_scheduler(tid)
        content = await self._get_last_message(tid)
        
        res = Result(value=content, metadata={"thread_id": tid})
        self.store.update(key, "COMPLETED", result=res)
        return res

    async def _handle_fork_thread(self, spec: ForkThread, key: str) -> Result:
        if not EGGTHREADS_AVAILABLE:
            new_id = f"{spec.source_thread_id}_fork_{key[:4]}"
            res = Result(value=new_id, metadata={"thread_id": new_id})
            self.store.update(key, "COMPLETED", result=res)
            return res

        # 1. Wait for source to be idle so we get a clean snapshot
        await egg_api.wait_subtree_idle(self.egg_db, spec.source_thread_id)
        
        # 2. Duplicate
        new_tid = egg_api.duplicate_thread(self.egg_db, spec.source_thread_id)
        
        res = Result(value=new_tid, metadata={"thread_id": new_tid})
        self.store.update(key, "COMPLETED", result=res)
        return res
