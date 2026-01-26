import json
import logging
import inspect
import hashlib
import pickle
import sqlite3
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Coroutine, Dict, Optional, List, Tuple, Union

_current_executor: ContextVar['FlowExecutor'] = ContextVar('executor')
_key_scope: ContextVar[tuple] = ContextVar('key_scope', default=())

logger = logging.getLogger("flow")


def _is_terminal_exception(e: Exception) -> bool:
  """Check if an exception type is inherently terminal (non-recoverable).

  Terminal exceptions propagate automatically through wrapped() calls.
  """
  # Import here to avoid circular imports
  try:
    from .eggthreads_tasks import ContextLimitExceededError
    if isinstance(e, ContextLimitExceededError):
      return True
  except ImportError:
    pass

  # Check for terminal attribute on exception
  return getattr(e, 'terminal', False)

@dataclass
class Task:
  """Base class for cacheable tasks.

  Subclass and implement run() to define task behavior.
  Tasks are cached by default based on their attributes.
  """
  cacheable: ClassVar[bool] = True

  def get_cache_key(self) -> str:
    """Generate a unique cache key for this task.

    The default implementation hashes the task's class name and all
    instance attributes. Override this method to customize cache key
    generation for specific task types.

    Returns:
        A SHA-256 hex digest uniquely identifying this task configuration.
    """
    data = self.__dict__.copy()
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(f"{self.__class__.__name__}:{s}".encode()).hexdigest()

  async def run(self) -> Any:
    """Execute the task logic. Must be implemented by subclasses.

    This method can be:
    - An async function returning a value
    - A generator yielding subtasks for composition

    Returns:
        The task result value.

    Raises:
        NotImplementedError: If not overridden by subclass.
    """
    raise NotImplementedError

  async def recover(self) -> bool:
    """Called before re-running a FAILED task. Returns whether to retry.

    This method is called when eggflow is about to re-execute a task that
    previously failed. Use it to:
    - Clean up partial/corrupted state from the failed run
    - Reset external resources to a known-good state
    - Decide whether retry makes sense

    Returns:
        True: Retry the task (state has been fixed, retry should succeed)
        False: Don't retry (failure is permanent, or retry limit reached)

    The default implementation returns True (always retry).
    Override in subclasses that need state cleanup or retry control.
    """
    return True

  async def execute(self, cached: bool = True, raw: bool = False) -> Union['Result', Any]:
    """Execute this task through the current executor.

    Args:
        cached: If False, skip caching for this execution (default True).
        raw: If True, return Result object instead of unwrapped value (default False).

    Returns:
        Value directly by default, or Result if raw=True.

    Raises:
        TaskError: If raw=False and the result has an error.

    Note:
        If called within a keyed_scope, the task's cache key is automatically
        extended with the scope's keys (unless cached=False).
    """
    executor = _current_executor.get(None)
    if executor:
      # Apply key scope if caching is enabled
      task = self
      if cached:
        scope_keys = _key_scope.get()
        if scope_keys:
          task = Keyed(self, scope_keys)

      if cached:
        if raw:
          result = await executor.run(wrapped(task))
        else:
          return await executor.run(task)
      else:
        if raw:
          result = await executor.run(wrapped(nocache(self)))
        else:
          return await executor.run(nocache(self))
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
  """Raised when a task fails and values are being returned (not wrapped).

  Terminal errors (like context limit exceeded) propagate automatically
  even through wrapped() calls - they can only be caught with try/except.
  """
  def __init__(self, message: str, result: 'Result', terminal: bool = False):
    super().__init__(message)
    self.result = result
    self.terminal = terminal

  @property
  def is_terminal(self) -> bool:
    """Check if this is a terminal error that should not be retried."""
    return self.terminal

class NoCache:
  """Wrapper to skip caching for a specific task execution."""
  __slots__ = ('task',)

  def __init__(self, task: Task):
    self.task = task

def nocache(task: Task) -> NoCache:
  """Wrap a task to skip caching for this execution.

  Usage:
      value = yield nocache(MyTask("foo"))
  """
  return NoCache(task)

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

class Keyed(Task):
  """Wrapper to extend a task's cache key with additional dependency context.

  Use this when a task's result depends on external state not captured in its
  default cache key. The extension is appended to the original key.

  This is a Task subclass that delegates run() to the wrapped task while
  providing a modified cache key.
  """
  def __init__(self, task: Task, keys: tuple):
    self.task = task
    self.keys = keys

  def get_cache_key(self) -> str:
    original = self.task.get_cache_key()
    extension = ":".join(str(k) for k in self.keys)
    return hashlib.sha256(f"{original}:{extension}".encode()).hexdigest()

  def run(self):
    return self.task.run()

  async def recover(self) -> bool:
    # Delegate recovery to wrapped task
    recover_method = getattr(self.task, 'recover', None)
    if recover_method and callable(recover_method):
      result = recover_method()
      if inspect.iscoroutine(result):
        return await result
      return result
    return True

def keyed(task: Task, *keys) -> Keyed:
  """Extend a task's cache key with additional dependency context.

  Use this when a task depends on external state not captured in its parameters.
  The keys are appended to the task's original cache key.

  Usage:
      # Cache key now includes solution_hash as dependency
      result = yield keyed(ExecuteBashCommand(...), solution_hash)

      # Can combine with wrapped
      result = yield wrapped(keyed(MyTask(...), version, context_id))
  """
  return Keyed(task, keys)

class Rekeyed(Task):
  """Wrapper to completely replace a task's cache key.

  Use this when you need complete control over the cache key, replacing
  the task's default key entirely. Use with caution - you lose any
  key components the original task computed.

  This is a Task subclass that delegates run() to the wrapped task while
  providing a completely new cache key.
  """
  def __init__(self, task: Task, keys: tuple):
    self.task = task
    self.keys = keys

  def get_cache_key(self) -> str:
    key_str = ":".join(str(k) for k in self.keys)
    return hashlib.sha256(f"rekeyed:{key_str}".encode()).hexdigest()

  def run(self):
    return self.task.run()

  async def recover(self) -> bool:
    # Delegate recovery to wrapped task
    recover_method = getattr(self.task, 'recover', None)
    if recover_method and callable(recover_method):
      result = recover_method()
      if inspect.iscoroutine(result):
        return await result
      return result
    return True

def rekeyed(task: Task, *keys) -> Rekeyed:
  """Completely replace a task's cache key with custom keys.

  Use this when the task's default cache key is wrong for your context.
  The original cache key is discarded entirely.

  Usage:
      # Cache key is now based solely on custom_id
      result = yield rekeyed(SomeTask(...), custom_id)

  Warning: Use with caution. The task's original cache key components
  (which may capture important state) are completely replaced.
  """
  return Rekeyed(task, keys)

class keyed_scope:
  """Context manager that automatically keys all tasks executed within its scope.

  All tasks executed via Task.execute() within this scope will have their
  cache keys extended with the scope's keys. Scopes can be nested - keys
  accumulate from outer to inner scopes.

  Usage:
      async with keyed_scope(attempt):
          # All tasks here are automatically keyed by `attempt`
          result = await SomeTask(...).execute()
          result2 = await AnotherTask(...).execute()

      # Can nest scopes
      async with keyed_scope(outer_key):
          async with keyed_scope(inner_key):
              # Keyed by both outer_key and inner_key
              result = await Task(...).execute()
  """
  def __init__(self, *keys):
    self.keys = keys
    self.token = None

  def __enter__(self):
    current = _key_scope.get()
    self.token = _key_scope.set(current + self.keys)
    return self

  def __exit__(self, *args):
    _key_scope.reset(self.token)

  async def __aenter__(self):
    return self.__enter__()

  async def __aexit__(self, *args):
    return self.__exit__(*args)

def get_current_key_scope() -> tuple:
  """Get the current key scope (for internal use)."""
  return _key_scope.get()

class FuncTask(Task):
  """Task that wraps a function or method call with configurable cache key.

  Created via as_task() to convert existing functions/methods into cacheable tasks.
  """

  def __init__(self, func: Callable, args: tuple, kwargs: dict,
               cache_key: Optional[Tuple[Any, ...]] = None):
    """Initialize a FuncTask.

    Args:
        func: The function or method to wrap.
        args: Positional arguments to pass to the function.
        kwargs: Keyword arguments to pass to the function.
        cache_key: Explicit cache key tuple. If None, uses args + kwargs values.
    """
    self.func = func
    self.args = args
    self.kwargs = kwargs
    # If cache_key not specified, use all args and kwargs values
    self._cache_key = cache_key if cache_key is not None else args + tuple(kwargs.values())

  def get_cache_key(self) -> str:
    """Generate cache key from function name and cache_key tuple."""
    data = {
      'func': f"{self.func.__module__}.{self.func.__qualname__}",
      'cache_key': self._cache_key,
    }
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()

  async def run(self) -> Any:
    """Execute the wrapped function with stored arguments."""
    result = self.func(*self.args, **self.kwargs)
    if inspect.iscoroutine(result):
      return await result
    elif inspect.isgenerator(result):
      return result
    return result

def as_task(func_or_method: Callable, *args,
            cache_key: Optional[Tuple[Any, ...]] = None,
            **kwargs) -> Task:
  """Wrap a function or method call as a Task.

  This allows converting existing functions/methods to cacheable Tasks at the call site.

  Usage:
      # Wrap a method - include relevant state in cache_key:
      value = yield as_task(service.generate, "hello", cache_key=(service.model, "hello"))

      # Wrap a function - specify which args affect caching:
      value = yield as_task(fetch_data, url, timeout, cache_key=(url,))

      # Default: all args used for cache key
      value = yield as_task(compute, x, y)

  Args:
      func_or_method: A function or bound method
      *args: Arguments to pass
      cache_key: Explicit tuple of values for cache key. If not specified, all args are used.
      **kwargs: Keyword arguments to pass

  Returns:
      A FuncTask that can be yielded or executed.
  """
  return FuncTask(
    func=func_or_method,
    args=args,
    kwargs=kwargs,
    cache_key=cache_key,
  )

@dataclass
class Result:
  """Result of a task execution, containing value or error.

  Attributes:
      value: The return value if task succeeded, None otherwise.
      metadata: Additional data like artifacts, timing info, etc.
      error: Error message if task failed, None if successful.
      terminal: If True, this is a terminal (non-recoverable) error that will
                auto-propagate through wrapped() calls as a TaskError exception.
                Terminal errors skip recover() and are never retried.
  """
  value: Any = None
  metadata: Dict[str, Any] = field(default_factory=dict)
  error: Optional[str] = None
  terminal: bool = False

  @property
  def is_success(self) -> bool:
    """Check if the task completed successfully (no error)."""
    return self.error is None

  @property
  def is_terminal(self) -> bool:
    """Check if this is a terminal error that auto-propagates through wrapped()."""
    return self.error is not None and self.terminal

  @property
  def artifacts(self) -> Dict[str, str]:
    """Convenience accessor for extracted files from metadata."""
    return self.metadata.get('artifacts', {})

class TaskStore:
  """SQLite-based storage for task results and caching.

  Stores task results by cache key with status tracking (PENDING, RUNNING,
  COMPLETED, FAILED). Results are serialized using pickle.
  """

  def __init__(self, db_path: str = "flow.db"):
    """Initialize the task store.

    Args:
        db_path: Path to SQLite database file. Created if doesn't exist.
    """
    self.conn = sqlite3.connect(db_path, check_same_thread=False)
    self.conn.row_factory = sqlite3.Row
    self._init_db()

  def _init_db(self):
    """Create the tasks table if it doesn't exist."""
    self.conn.execute("""
      create table if not exists tasks (
        cache_key text primary key,
        status text,
        result_blob blob,
        updated_at timestamp default current_timestamp
      )
    """)
    self.conn.commit()

  def get(self, key: str) -> Optional[sqlite3.Row]:
    """Retrieve a task record by cache key.

    Args:
        key: The cache key to look up.

    Returns:
        Row with cache_key, status, result_blob, updated_at, or None if not found.
    """
    return self.conn.execute("select * from tasks where cache_key=?", (key,)).fetchone()

  def create(self, key: str, task: Task) -> None:
    """Create a new task record with PENDING status.

    Args:
        key: The cache key for this task.
        task: The task object (used for logging/debugging).
    """
    try:
      self.conn.execute("insert into tasks (cache_key, status) values (?, ?)",
                        (key, "PENDING"))
      self.conn.commit()
    except Exception as e:
      logger.error(str(e))
      raise e

  def update(self, key: str, status: str, result: Optional[Result] = None) -> None:
    """Update a task's status and optionally its result.

    Args:
        key: The cache key to update.
        status: New status (PENDING, RUNNING, COMPLETED, FAILED).
        result: Optional Result object to store (pickled).
    """
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
  """Executes tasks with automatic caching and composition support.

  The executor handles:
  - Running tasks and caching results by cache key
  - Generator-based task composition (yield subtasks)
  - Parallel execution of task lists
  - Error propagation and wrapped result handling
  """

  def __init__(self, store: TaskStore):
    """Initialize the executor with a task store.

    Args:
        store: TaskStore instance for caching results.
    """
    self.store = store

  async def run(self, flow: Union[Task, List[Task], Coroutine]) -> Union[Result, List[Result]]:
    """Run a task, list of tasks, or coroutine.

    Returns values directly by default. Use wrapped() to get Result objects.
    Raises TaskError on failure unless wrapped().
    """
    token = _current_executor.set(self)
    try:
      return await self._run_internal(flow)
    finally:
      _current_executor.reset(token)

  async def _run_internal(self, flow: Union[Task, List[Task], Coroutine, NoCache, Wrapped]) -> Union[Result, List[Result], Any]:
    # Handle Wrapped - return Result object instead of unwrapped value
    if isinstance(flow, Wrapped):
      inner = flow.task
      if isinstance(inner, NoCache):
        try:
          return await self._handle_task_uncached(inner.task)
        except Exception as e:
          is_terminal = _is_terminal_exception(e)
          result = Result(error=str(e), terminal=is_terminal)
          if is_terminal:
            raise TaskError(str(e), result, terminal=True)
          return result
      elif isinstance(inner, Task):
        result = await self._execute_task(inner)
        # CRITICAL: Terminal errors propagate even through wrapped()
        if result.is_terminal:
          raise TaskError(result.error, result, terminal=True)
        return result
      else:
        # Handle wrapped coroutine
        try:
          value = await inner
          return Result(value=value)
        except Exception as e:
          is_terminal = _is_terminal_exception(e)
          result = Result(error=str(e), terminal=is_terminal)
          if is_terminal:
            raise TaskError(str(e), result, terminal=True)
          return result

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

  async def _run_item(self, item: Union[Task, Coroutine, NoCache, Wrapped]) -> Union[Result, Any]:
    """Handle Task, coroutine, NoCache, or Wrapped wrapper in a list."""
    # Handle Wrapped - return Result object
    if isinstance(item, Wrapped):
      inner = item.task
      if isinstance(inner, NoCache):
        try:
          return await self._handle_task_uncached(inner.task)
        except Exception as e:
          is_terminal = _is_terminal_exception(e)
          result = Result(error=str(e), terminal=is_terminal)
          if is_terminal:
            raise TaskError(str(e), result, terminal=True)
          return result
      elif isinstance(inner, Task):
        result = await self._execute_task(inner)
        # CRITICAL: Terminal errors propagate even through wrapped()
        if result.is_terminal:
          raise TaskError(result.error, result, terminal=True)
        return result
      else:
        # Wrapped coroutine
        try:
          value = await inner
          return Result(value=value)
        except Exception as e:
          is_terminal = _is_terminal_exception(e)
          result = Result(error=str(e), terminal=is_terminal)
          if is_terminal:
            raise TaskError(str(e), result, terminal=True)
          return result

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
    # Apply key scope if present (extends cache key with scope keys)
    scope_keys = _key_scope.get()
    if scope_keys:
      task = Keyed(task, scope_keys)

    # Check if task should skip caching
    if not getattr(task, 'cacheable', True):
      try:
        return await self._handle_task_uncached(task)
      except Exception as e:
        # Preserve terminal flag for uncached tasks too
        is_terminal = _is_terminal_exception(e)
        return Result(error=str(e), terminal=is_terminal)

    key = task.get_cache_key()
    row = self.store.get(key)

    if row and row['status'] == "COMPLETED":
      try:
        return pickle.loads(row['result_blob'])
      except Exception as e:
        return Result(error=f"Completed Task but the result not unpickeling, error: {str(e)}", metadata={"corrupt": True})

    # Call recover() before re-running a FAILED task
    if row and row['status'] == "FAILED":
      logger.debug(f"Task {task.__class__.__name__} was FAILED, attempting recovery...")

      # CRITICAL: Check if the cached result is terminal - if so, DON'T retry
      cached_result = None
      try:
        cached_result = pickle.loads(row['result_blob'])
      except Exception:
        pass

      if cached_result and cached_result.is_terminal:
        # Terminal error - never retry, return cached failure immediately
        logger.debug(f"Task {task.__class__.__name__} has terminal error, not retrying")
        return cached_result

      # Non-terminal error - check if task wants to recover
      should_retry = True  # Default: retry
      try:
        recover_method = getattr(task, 'recover', None)
        if recover_method and callable(recover_method):
          logger.debug(f"Calling recover() on {task.__class__.__name__}")
          result = recover_method()
          if inspect.iscoroutine(result):
            result = await result
          logger.debug(f"recover() returned: {result}")
          # recover() returns bool: True=retry, False=stay FAILED
          if result is False:
            should_retry = False
      except Exception as e:
        # Log recovery failure but continue with re-execution
        logger.warning(f"Task recovery failed: {e}")

      if not should_retry:
        # Task decided not to retry - return the cached FAILED result
        if cached_result:
          return cached_result
        return Result(error="Task recovery returned False, not retrying")

    if not row:
      self.store.create(key, task)
    else:
      self.store.update(key, "RUNNING")

    try:
      return await self._handle_task(task, key)
    except Exception as e:
      # Check if this is a terminal error (e.g., ContextLimitExceededError)
      is_terminal = _is_terminal_exception(e)
      res = Result(error=str(e), terminal=is_terminal)
      self.store.update(key, "FAILED", result=res)
      return res

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
            yielded = gen.throw(type(e), e, e.__traceback__)
      except StopIteration as e:
        final_val = e.value
    elif inspect.iscoroutine(gen):
      final_val = await gen
    else:
      final_val = gen

    res = Result(value=final_val)
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
            yielded = gen.throw(type(e), e, e.__traceback__)
      except StopIteration as e:
        final_val = e.value
    elif inspect.iscoroutine(gen):
      final_val = await gen
    else:
      final_val = gen
    return Result(value=final_val)
