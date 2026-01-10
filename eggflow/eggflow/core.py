import json
import logging
import inspect
import hashlib
import pickle
import sqlite3
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Union

logger = logging.getLogger("flow")

@dataclass
class Task:
  def get_cache_key(self) -> str:
    data = self.__dict__.copy()
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(f"{self.__class__.__name__}:{s}".encode()).hexdigest()

  async def run(self) -> Any:
    raise NotImplementedError

@dataclass
class Result:
  value: Any = None
  metadata: Dict[str, Any] = field(default_factory = dict)
  error: Optional[str] = None

  @property
  def is_success(self) -> bool: return self.error is None

  @property
  def artifacts(self) -> Dict[str, str]:
    """Convenience accessor for extracted files."""
    return self.metadata.get('artifacts', {})

class TaskStore:
  def __init__(self, db_path: str = "flow.db"):
    self.conn = sqlite3.connect(db_path, check_same_thread=False)
    self.conn.row_factory = sqlite3.Row
    self._init_db()

  def _init_db(self):
    self.conn.execute("""
      create table if not exists tasks (
        cache_key text primary key,
        task_blob blob,
        status text,
        result_blob blob,
        updated_at timestamp default current_timestamp
      )
    """)
    self.conn.commit()

  def get(self, key):
    return self.conn.execute("select * from tasks where cache_key=?", (key,)).fetchone()

  def create(self, key, task):
    try:
      self.conn.execute("insert into tasks (cache_key, task_blob, status) values (?, ?, ?)",
                        (key, pickle.dumps(task), "PENDING"))
      self.conn.commit()
    except Exception as e:
      logger.error(str(e))
      raise e

  def update(self, key, status, result=None):
    try:
      if result:
        self.conn.execute("update tasks set status=?, result_blob=?, updated_at=current_timestamp where cache_key=?",
                          (status, pickle.dumps(result), key))
      else: 
        self.conn.execute("update tasks set status=?, updated_at=current_timestamp where cache_key=?",
                          (status, key))
      self.conn.commit()
    except Exception as e:
      logger.error(str(e))
      raise e

class FlowExecutor:
  def __init__(self, store: TaskStore):
    self.store = store

  async def run(self, flow: Union[Task, List[Task]]) -> Union[Result, List[Result]]:
    if isinstance(flow, list):
      return await asyncio.gather(*(self._execute_task(t) for t in flow))
    return await self._execute_task(flow)

  async def _execute_task(self, task: Task) -> Result:
    key = task.get_cache_key()
    row = self.store.get(key)

    if row and row['status'] == "COMPLETED":
      #Task completed, can be failed or succeeded (depends on Result, handled by code defining the flow)
      try: return pickle.loads(row['result_blob'])
      except Exception as e:
        return Result(error=f"Completed Task but the result not unpickeling, error: {str(e)}", metadata={"corrupt": True})

    if not row: self.store.create(key, task)
    else: self.store.update(key, "RUNNING")

    try:
      return await self._handle_task(task, key)
    except Exception as e:
      res = Result(error = str(e))
      self.store.update(key, "FAILED", result=res)
      return res # err Result

  async def _handle_task(self, task: Task, key: str) -> Result:
    gen = task.run()
    final_val = None
    if inspect.isgenerator(gen):
      try:
        yielded = next(gen)
        while True:
          res = await self.run(yielded)
          yielded = gen.send(res)
      except StopIteration as e: final_val = e.value
    elif inspect.iscoroutine(gen): final_val = await gen
    else: final_val = gen

    res = Result(value = final_val)
    self.store.update(key, "COMPLETED", result=res)
    return res
