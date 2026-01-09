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
    from eggthreads import (
        ThreadsDB,
        SubtreeScheduler,
        create_root_thread,
        append_message,
        create_snapshot,
        wait_subtree_idle,
        duplicate_thread,
        get_thread_working_directory,
        create_default_tools,
        create_llm_client,
        approve_tool_calls_for_thread,
    )
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
    
    @property
    def artifacts(self) -> Dict[str, str]:
        """Convenience accessor for extracted files."""
        return self.metadata.get('artifacts', {})

# --- 2. Task Specifications ---

@dataclass
class TaskSpec:
    def get_cache_key(self) -> str:
        data = self.__dict__.copy()
        s = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(f"{self.__class__.__name__}:{s}".encode()).hexdigest()

    async def run(self) -> Any:
        raise NotImplementedError

@dataclass
class CreateThread(TaskSpec):
    prompt: str
    model_key: Optional[str] = None
    system_prompt: Optional[str] = None
    seed: int = 0
    output_files: List[str] = field(default_factory=list) # Files to extract after run

    async def run(self): pass

@dataclass
class ContinueThread(TaskSpec):
    thread_id: str
    content: str
    role: str = "user"
    output_files: List[str] = field(default_factory=list) # Files to extract after run
    
    async def run(self): pass

@dataclass
class ForkThread(TaskSpec):
    source_thread_id: str
    async def run(self): pass

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
        if EGGTHREADS_AVAILABLE:
            self.egg_db = ThreadsDB(egg_db_path)
            self.egg_db.init_schema()
            self.llm_client = create_llm_client()
            self.tools = create_default_tools()
        else:
            self.egg_db = None
            self.llm_client = None
            self.tools = None 

    async def run(self, spec: Union[TaskSpec, List[TaskSpec]]) -> Union[Result, List[Result]]:
        if isinstance(spec, list):
            return await asyncio.gather(*(self._execute_task(s) for s in spec))
        return await self._execute_task(spec)

    async def _execute_task(self, spec: TaskSpec) -> Result:
        key = spec.get_cache_key()
        row = self.store.get(key)
        
        if row and row['status'] == "COMPLETED":
            try: return pickle.loads(row['result_blob'])
            except Exception: pass
        
        if not row: self.store.create(key, spec)
        else: self.store.update(key, "RUNNING")

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
        gen = spec.run()
        final_val = None
        if inspect.isgenerator(gen):
            try:
                to_yield = next(gen)
                while True:
                    res = await self.run(to_yield)
                    to_yield = gen.send(res)
            except StopIteration as e: final_val = e.value
        elif inspect.iscoroutine(gen): final_val = await gen
        else: final_val = gen
        
        res = Result(value=final_val)
        self.store.update(key, "COMPLETED", result=res)
        return res

    # --- EggThread Integration Logic ---

    async def _run_scheduler(self, tid):
        if not EGGTHREADS_AVAILABLE:
            return
        approve_tool_calls_for_thread(self.egg_db, tid, decision='all-in-turn')
        scheduler = SubtreeScheduler(
            self.egg_db,
            tid,
            llm=self.llm_client,
            tools=self.tools
        )
        task = asyncio.create_task(scheduler.run_forever())
        try:
            await wait_subtree_idle(self.egg_db, tid)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _get_last_message(self, tid):
        if not EGGTHREADS_AVAILABLE: return "Mock Response"
        snap = create_snapshot(self.egg_db, tid)
        msgs = snap.get("messages", [])
        return msgs[-1].get("content", "") if msgs else ""

    async def _collect_outputs(self, tid: str, filenames: List[str]) -> Dict[str, str]:
        """Extracts requested files from the thread's working directory."""
        if not filenames: return {}
        if not EGGTHREADS_AVAILABLE:
            return {f: f"Mock content of {f}" for f in filenames}
        
        wd = get_thread_working_directory(self.egg_db, tid)
        outputs = {}
        for fname in filenames:
            fpath = wd / fname
            if fpath.exists():
                try: outputs[fname] = fpath.read_text(errors='replace')
                except Exception as e: outputs[fname] = f"Error reading: {e}"
        return outputs

    async def _handle_create_thread(self, spec: CreateThread, key: str, row) -> Result:
        if row and row['external_id']:
            tid = row['external_id']
        else:
            if not EGGTHREADS_AVAILABLE: tid = f"mock_thread_{key[:8]}"
            else:
                tid = create_root_thread(self.egg_db, initial_model_key=spec.model_key)
                append_message(self.egg_db, tid, "user", spec.prompt)
                create_snapshot(self.egg_db, tid)
            self.store.update(key, "RUNNING", external_id=tid)

        await self._run_scheduler(tid)
        content = await self._get_last_message(tid)
        artifacts = await self._collect_outputs(tid, spec.output_files)
        
        res = Result(value=content, metadata={"thread_id": tid, "artifacts": artifacts})
        self.store.update(key, "COMPLETED", result=res)
        return res

    async def _handle_continue_thread(self, spec: ContinueThread, key: str) -> Result:
        tid = spec.thread_id
        if not EGGTHREADS_AVAILABLE:
            artifacts = await self._collect_outputs(tid, spec.output_files)
            res = Result(value=f"Mock Reply to: {spec.content}", metadata={"thread_id": tid, "artifacts": artifacts})
            self.store.update(key, "COMPLETED", result=res)
            return res

        # Idempotency
        snap = create_snapshot(self.egg_db, tid)
        msgs = snap.get("messages", [])
        last_msg = msgs[-1] if msgs else None

        if not (last_msg and last_msg.get('role') == spec.role and last_msg.get('content') == spec.content):
            append_message(self.egg_db, tid, spec.role, spec.content)
            create_snapshot(self.egg_db, tid)
        
        await self._run_scheduler(tid)
        content = await self._get_last_message(tid)
        artifacts = await self._collect_outputs(tid, spec.output_files)
        
        res = Result(value=content, metadata={"thread_id": tid, "artifacts": artifacts})
        self.store.update(key, "COMPLETED", result=res)
        return res

    async def _handle_fork_thread(self, spec: ForkThread, key: str) -> Result:
        if not EGGTHREADS_AVAILABLE:
            new_id = f"{spec.source_thread_id}_fork_{key[:4]}"
            res = Result(value=new_id, metadata={"thread_id": new_id})
            self.store.update(key, "COMPLETED", result=res)
            return res

        await wait_subtree_idle(self.egg_db, spec.source_thread_id)
        new_tid = duplicate_thread(self.egg_db, spec.source_thread_id)
        
        res = Result(value=new_tid, metadata={"thread_id": new_tid})
        self.store.update(key, "COMPLETED", result=res)
        return res
