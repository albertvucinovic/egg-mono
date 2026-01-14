import json
import logging
import inspect
import hashlib
import pickle
import sqlite3
import asyncio
import warnings
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

  async def execute(self, cached: bool = True, raw: bool = False) -> Union['Result', Any]:
    """Execute this task through the current executor.

    Args:
        cached: If False, skip caching for this execution (default True).
        raw: If True, return Result object instead of unwrapped value (default False).

    Returns:
        Value directly by default, or Result if raw=True.

    Raises:
        TaskError: If raw=False and the result has an error.
    """
    executor = _current_executor.get(None)
    if executor:
      if cached:
        if raw:
          result = await executor.run(wrapped(self))
        else:
          return await executor.run(self)  # Returns value, raises on error
      else:
        if raw:
          result = await executor.run(wrapped(nocache(self)))
        else:
          return await executor.run(nocache(self))  # Returns value, raises on error
    else:
      # No executor in context - run directly without caching
      gen = self.run()
      if inspect.iscoroutine(gen):
        val = await gen
      else:
        val = gen
      result = Result(value=val)
      if not raw:
        return val

    return result

class TaskError(Exception):
  """Raised when unwrapping a failed Result."""
  def __init__(self, message: str, result: 'Result'):
    super().__init__(message)
    self.result = result

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

class Unwrap:
  """Wrapper to unwrap Result and raise on error.

  DEPRECATED: Values are now returned by default. Use wrapped() to get Result.
  """
  __slots__ = ('task',)

  def __init__(self, task: Task):
    warnings.warn(
      "Unwrap is deprecated. Values are now returned by default. "
      "Use wrapped() to get Result objects.",
      DeprecationWarning,
      stacklevel=2
    )
    self.task = task

def unwrap(task: Task) -> Unwrap:
  """Wrap a task to return unwrapped value and raise TaskError on failure.

  DEPRECATED: Values are now returned by default. This function is a no-op.
  """
  warnings.warn(
    "unwrap() is deprecated. Values are now returned by default. "
    "Use wrapped() to get Result objects.",
    DeprecationWarning,
    stacklevel=2
  )
  return Unwrap(task)

class Wrapped:
  """Wrapper to get Result object instead of unwrapped value.

  By default, yielding a task returns the value directly and raises TaskError on failure.
  Use wrapped() when you need access to the Result object (for error checking, metadata, etc).
  """
  __slots__ = ('task',)

  def __init__(self, task: Task):
    self.task = task

def wrapped(task: Task) -> Wrapped:
  """Wrap a task to return Result object instead of unwrapped value.

  By default, yielding a task returns the value directly and raises TaskError on failure.
  Use this when you need access to the Result object.

  Usage:
      # Get Result object (won't raise on error)
      result = yield wrapped(MyTask("foo"))
      if result.is_success:
          print(result.value)
      else:
          print(result.error)

      # Can combine with nocache
      result = yield wrapped(nocache(MyTask("foo")))
  """
  return Wrapped(task)

class MethodTask(Task):
  """Task that wraps a method call with configurable cache key.

  DEPRECATED: Use as_task() with explicit cache_key instead.
  """

  def __init__(self, instance: Any, method: Callable, cache_attrs: Tuple[str, ...],
               args: tuple, kwargs: dict):
    warnings.warn(
      "MethodTask is deprecated. Use as_task() with explicit cache_key instead.",
      DeprecationWarning,
      stacklevel=2
    )
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

class FuncTask(Task):
  """Task that wraps a function call with configurable cache key."""

  def __init__(self, func: Callable, args: tuple, kwargs: dict,
               cache_key: Optional[Tuple[Any, ...]] = None):
    self.func = func
    self.args = args
    self.kwargs = kwargs
    # If cache_key not specified, use all args and kwargs values
    self._cache_key = cache_key if cache_key is not None else args + tuple(kwargs.values())

  def get_cache_key(self) -> str:
    data = {
      'func': f"{self.func.__module__}.{self.func.__qualname__}",
      'cache_key': self._cache_key,
    }
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()

  async def run(self) -> Any:
    result = self.func(*self.args, **self.kwargs)
    if inspect.iscoroutine(result):
      return await result
    elif inspect.isgenerator(result):
      return result
    return result

def taskmethod(*cache_attrs: str) -> Callable:
  """Decorator to convert a method into a Task factory.

  DEPRECATED: Use as_task() with explicit cache_key instead.

  Example migration:
      # Old:
      @taskmethod('model')
      async def generate(self, prompt): ...
      result = yield service.generate("hello")

      # New:
      async def generate(self, prompt): ...
      value = yield as_task(service.generate, "hello", cache_key=(service.model, "hello"))
  """
  warnings.warn(
    "taskmethod() is deprecated. Use as_task() with explicit cache_key instead.",
    DeprecationWarning,
    stacklevel=2
  )
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

def as_task(func_or_method: Callable, *args,
            cache_key: Optional[Tuple[Any, ...]] = None,
            **kwargs) -> Task:
  """Wrap a method or function call as a Task without modifying the class/module.

  This allows converting existing methods or functions to Tasks at the call site.
  Works with both bound methods and plain functions using the same pattern.

  Usage with methods:
      class ExternalService:
          def __init__(self, model: str):
              self.model = model

          async def generate(self, prompt: str):
              return f"[{self.model}] {prompt}"

      service = ExternalService("gpt-4")

      # Wrap method - include instance attributes in cache_key:
      value = yield as_task(service.generate, "hello", cache_key=(service.model, "hello"))

  Usage with functions:
      async def fetch_data(url: str, timeout: int):
          ...

      # Wrap function - use cache_key to specify which args matter:
      value = yield as_task(fetch_data, url, timeout, cache_key=(url,))

      # Default: all args used for cache key
      value = yield as_task(fetch_data, url, timeout)

  Args:
      func_or_method: A bound method or function
      *args: Arguments to pass
      cache_key: Explicit tuple of values for cache key.
                 If not specified, all args are used.
      **kwargs: Keyword arguments to pass

  Returns:
      A FuncTask that can be yielded or executed. Returns value directly by default.
  """
  # Always use FuncTask - works for both functions and bound methods
  return FuncTask(
    func=func_or_method,
    args=args,
    kwargs=kwargs,
    cache_key=cache_key,
  )

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

  async def _run_internal(self, flow: Union[Task, List[Task], Coroutine, NoCache, Unwrap, Wrapped]) -> Union[Result, List[Result], Any]:
    # Handle Wrapped - return Result object instead of unwrapped value
    if isinstance(flow, Wrapped):
      inner = flow.task
      # Recursively handle the inner task but return Result
      if isinstance(inner, NoCache):
        try:
          return await self._handle_task_uncached(inner.task)
        except Exception as e:
          return Result(error=str(e))
      elif isinstance(inner, Task):
        return await self._execute_task(inner)
      else:
        # Handle wrapped coroutine
        try:
          value = await inner
          return Result(value=value)
        except Exception as e:
          return Result(error=str(e))

    # Handle Unwrap (DEPRECATED - now same as default behavior)
    if isinstance(flow, Unwrap):
      inner = flow.task
      # Just unwrap and process normally (values are default now)
      return await self._run_internal(inner)

    if isinstance(flow, NoCache):
      # NoCache wrapper - execute without caching, return value
      try:
        result = await self._handle_task_uncached(flow.task)
        if result.error:
          raise TaskError(result.error, result)
        return result.value
      except TaskError:
        raise
      except Exception as e:
        raise TaskError(str(e), Result(error=str(e)))

    if inspect.iscoroutine(flow):
      # Raw coroutine - execute without caching, return value
      try:
        value = await flow
        return value
      except Exception as e:
        raise TaskError(str(e), Result(error=str(e)))

    if isinstance(flow, list):
      return await asyncio.gather(*(self._run_item(item) for item in flow))

    # Regular task - execute and return value (raise on error)
    result = await self._execute_task(flow)
    if result.error:
      raise TaskError(result.error, result)
    return result.value

  async def _run_item(self, item: Union[Task, Coroutine, NoCache, Unwrap, Wrapped]) -> Union[Result, Any]:
    """Handle Task, coroutine, NoCache, Unwrap, or Wrapped wrapper in a list.

    By default returns unwrapped values and raises TaskError on failure.
    Use wrapped() to get Result objects.
    """
    # Handle Wrapped - return Result object
    if isinstance(item, Wrapped):
      inner = item.task
      if isinstance(inner, NoCache):
        try:
          return await self._handle_task_uncached(inner.task)
        except Exception as e:
          return Result(error=str(e))
      elif isinstance(inner, Task):
        return await self._execute_task(inner)
      else:
        # Wrapped coroutine
        try:
          value = await inner
          return Result(value=value)
        except Exception as e:
          return Result(error=str(e))

    # Handle Unwrap (DEPRECATED - now same as default)
    if isinstance(item, Unwrap):
      inner = item.task
      return await self._run_item(inner)

    if isinstance(item, NoCache):
      try:
        result = await self._handle_task_uncached(item.task)
        if result.error:
          raise TaskError(result.error, result)
        return result.value
      except TaskError:
        raise
      except Exception as e:
        raise TaskError(str(e), Result(error=str(e)))

    if inspect.iscoroutine(item):
      try:
        value = await item
        return value
      except Exception as e:
        raise TaskError(str(e), Result(error=str(e)))

    # Regular task - return value, raise on error
    result = await self._execute_task(item)
    if result.error:
      raise TaskError(result.error, result)
    return result.value

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
          try:
            res = await self.run(yielded)
            yielded = gen.send(res)
          except TaskError as e:
            # Throw the error into the generator so it can be caught
            yielded = gen.throw(type(e), e, e.__traceback__)
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
          try:
            res = await self.run(yielded)
            yielded = gen.send(res)
          except TaskError as e:
            # Throw the error into the generator so it can be caught
            yielded = gen.throw(type(e), e, e.__traceback__)
      except StopIteration as e: final_val = e.value
    elif inspect.iscoroutine(gen): final_val = await gen
    else: final_val = gen
    return Result(value=final_val)
