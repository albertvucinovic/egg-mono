import json
import logging
import inspect
import hashlib
import pickle
import sqlite3
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, ClassVar, Coroutine, Dict, Optional, List, Tuple, Union

_current_executor: ContextVar['FlowExecutor'] = ContextVar('executor')

logger = logging.getLogger("flow")

@dataclass
class Task:
  cacheable: ClassVar[bool] = True

  def get_cache_key(self) -> str:
    data = self.__dict__.copy()
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(f"{self.__class__.__name__}:{s}".encode()).hexdigest()

  async def run(self) -> Any:
    raise NotImplementedError

  async def execute(self, cached: bool = True) -> 'Result':
    """Execute this task through the current executor.

    Args:
        cached: If False, skip caching for this execution (default True).
    """
    executor = _current_executor.get(None)
    if executor:
      if cached:
        return await executor.run(self)
      else:
        return await executor.run(nocache(self))
    # No executor in context - run directly without caching
    gen = self.run()
    if inspect.iscoroutine(gen):
      val = await gen
    else:
      val = gen
    return Result(value=val)

class NoCache:
  """Wrapper to mark a task as uncacheable for this execution."""
  __slots__ = ('task',)

  def __init__(self, task: Task):
    self.task = task

def nocache(task: Task) -> NoCache:
  """Wrap a task to skip caching for this execution.

  Usage:
      result = yield nocache(MyTask("foo"))
  """
  return NoCache(task)

class MethodTask(Task):
  """Task that wraps a method call with configurable cache key."""

  def __init__(self, instance: Any, method: Callable, cache_attrs: Tuple[str, ...],
               args: tuple, kwargs: dict):
    self.instance = instance
    self.method = method
    self.cache_attrs = cache_attrs
    self.args = args
    self.kwargs = kwargs

  def get_cache_key(self) -> str:
    # Build cache key from method name, instance attrs, and args
    data = {
      'method': f"{self.method.__module__}.{self.method.__qualname__}",
      'instance_state': {attr: getattr(self.instance, attr) for attr in self.cache_attrs},
      'args': self.args,
      'kwargs': self.kwargs,
    }
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()

  async def run(self) -> Any:
    result = self.method(self.instance, *self.args, **self.kwargs)
    if inspect.iscoroutine(result):
      return await result
    elif inspect.isgenerator(result):
      # Handle generator-based tasks
      return result
    return result

def taskmethod(*cache_attrs: str) -> Callable:
  """Decorator to convert a method into a Task factory.

  The decorated method returns a MethodTask when called, which can be
  yielded in a flow. The cache key is built from:
  1. The method's qualified name
  2. Specified instance attributes
  3. Method arguments

  Usage:
      class MyService:
          def __init__(self, model: str):
              self.model = model
              self.debug = False  # Not included in cache

          @taskmethod('model')  # Only 'model' affects cache key
          async def generate(self, prompt: str):
              return f"generated with {self.model}: {prompt}"

      # In a flow:
      service = MyService("gpt-4")
      result = yield service.generate("hello")  # Returns MethodTask

  Args:
      cache_attrs: Names of instance attributes to include in cache key.
                   Use @taskmethod() for no instance state (just args).
  """
  def decorator(method: Callable) -> Callable:
    @wraps(method)
    def wrapper(self, *args, **kwargs) -> MethodTask:
      return MethodTask(
        instance=self,
        method=method,
        cache_attrs=cache_attrs,
        args=args,
        kwargs=kwargs,
      )
    return wrapper
  return decorator

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
      self.conn.execute("insert into tasks (cache_key, status) values (?, ?)",
                        (key, "PENDING"))
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

  async def run(self, flow: Union[Task, List[Task], Coroutine]) -> Union[Result, List[Result]]:
    # Set this executor as current in context
    token = _current_executor.set(self)
    try:
      return await self._run_internal(flow)
    finally:
      _current_executor.reset(token)

  async def _run_internal(self, flow: Union[Task, List[Task], Coroutine, NoCache]) -> Union[Result, List[Result]]:
    if isinstance(flow, NoCache):
      # Unwrap and execute without caching
      try:
        return await self._handle_task_uncached(flow.task)
      except Exception as e:
        return Result(error=str(e))
    if inspect.iscoroutine(flow):
      # Raw coroutine - execute without caching
      try:
        value = await flow
        return Result(value=value)
      except Exception as e:
        return Result(error=str(e))
    if isinstance(flow, list):
      return await asyncio.gather(*(self._run_item(item) for item in flow))
    return await self._execute_task(flow)

  async def _run_item(self, item: Union[Task, Coroutine, NoCache]) -> Result:
    """Handle Task, coroutine, or NoCache wrapper in a list."""
    if isinstance(item, NoCache):
      try:
        return await self._handle_task_uncached(item.task)
      except Exception as e:
        return Result(error=str(e))
    if inspect.iscoroutine(item):
      try:
        value = await item
        return Result(value=value)
      except Exception as e:
        return Result(error=str(e))
    return await self._execute_task(item)

  async def _execute_task(self, task: Task) -> Result:
    # Check if task should skip caching
    if not getattr(task, 'cacheable', True):
      try:
        return await self._handle_task_uncached(task)
      except Exception as e:
        return Result(error=str(e))

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

  async def _handle_task_uncached(self, task: Task) -> Result:
    """Execute task without caching."""
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
    return Result(value=final_val)
