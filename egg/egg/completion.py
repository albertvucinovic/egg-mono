from __future__ import annotations

from typing import Any, Callable, Iterable, List, Dict, Mapping, Optional

from dataclasses import dataclass
import re
import threading

try:
    from prompt_toolkit.completion import Completer, Completion
except Exception:  # pragma: no cover - exercised when optional dep missing
    class Completion:  # type: ignore[no-redef]
        """Small fallback matching the prompt_toolkit attributes we use."""

        def __init__(self, text: str, start_position: int = 0, display: Any = None, display_meta: Any = None):
            self.text = text
            self.start_position = start_position
            self.display = display if display is not None else text
            self.display_meta = display_meta

    class Completer:  # type: ignore[no-redef]
        """Fallback base so Egg can run without prompt_toolkit installed."""

        def get_completions(self, document, complete_event):
            return iter(())

# Import eggthreads API helpers. The eggthreads package is a declared
# dependency, so this import should succeed at runtime.
try:
    from eggthreads import list_threads, list_children_with_meta
except Exception:
    list_threads = None  # type: ignore
    list_children_with_meta = None  # type: ignore

from eggthreads.command_catalog import (  # type: ignore
    CommandContext,
    SESSION_ON_COMPLETIONS,
    SESSION_TARGET_COMPLETIONS,
    create_default_command_registry,
    command_completion_names,
)
from eggthreads.content_parts import content_to_plain_text
from eggllm.capabilities import is_chat_model
from eggthreads.artifact_completion import (
    artifact_workspace_from_db,
    filesystem_completion_items,
    is_provider_artifact_export_path_position,
    is_provider_artifact_id_position,
    provider_artifact_completion_items,
)


@dataclass(frozen=True)
class CompletionRequest:
    """Immutable identity for one asynchronous editor completion request."""

    generation: int
    line: str
    row: int
    col: int
    thread_id: str
    snapshot_seq: int


class AsyncCompletionWorker:
    """Latest-request-wins completion worker with a thread-owned database.

    SQLite connections are thread-affine, so the worker opens its own
    :class:`ThreadsDB` inside the worker thread rather than borrowing the UI
    connection.  At most one request waits behind the currently running one; a
    newer request replaces that pending request.
    """

    def __init__(
        self,
        db_path: Any,
        llm: Any,
        command_registry: Any,
        loop: Any,
        on_result: Callable[[CompletionRequest, List[Dict[str, str]]], None],
    ) -> None:
        self._db_path = db_path
        self._llm = llm
        self._command_registry = command_registry
        self._loop = loop
        self._on_result = on_result
        self._condition = threading.Condition()
        self._pending: Optional[CompletionRequest] = None
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="egg-completion", daemon=True
        )
        self._thread.start()

    def request(self, request: CompletionRequest) -> None:
        with self._condition:
            if self._stopping:
                return
            self._pending = request
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        from eggthreads import ThreadsDB

        db = ThreadsDB(self._db_path)
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._stopping:
                        self._condition.wait()
                    if self._stopping:
                        return
                    request = self._pending
                    self._pending = None
                if request is None:
                    continue
                try:
                    items = get_autocomplete_items(
                        request.line,
                        request.col,
                        db,
                        lambda request=request: request.thread_id,
                        self._llm,
                        self._command_registry,
                    )
                except Exception:
                    items = []
                try:
                    self._loop.call_soon_threadsafe(
                        self._on_result, request, items
                    )
                except RuntimeError:
                    return
        finally:
            try:
                db.conn.close()
            except Exception:
                pass


class ModelCompleter(Completer):
    """Completer for '/model ' values, leveraging an eggllm-like client if provided.

    Expects an object 'llm' with:
      - registry.models_config: Dict[str, Any]
      - catalog.get_all_models_suggestions(prefix: str) -> Iterable[str]
      - get_providers() -> Iterable[str]
      - catalog.get_all_models_for_provider(provider: str) -> Iterable[str]
    """

    def __init__(self, llm: Any | None):
        self.llm = llm

    def _normalize(self, s: str) -> str:
        if not s:
            return ""
        ns = re.sub(r"[^0-9a-z]+", " ", s.lower()).strip()
        ns = re.sub(r"\s+", " ", ns)
        return ns

    def _is_chat_provider(self, provider: str) -> bool:
        try:
            cfg = (getattr(getattr(self.llm, 'registry', None), 'providers_config', {}) or {}).get(provider) or {}
            return is_chat_model(cfg)
        except Exception:
            return True

    def _is_chat_all_suggestion(self, suggestion: str) -> bool:
        if not isinstance(suggestion, str) or not suggestion.lower().startswith('all:'):
            return True
        rest = suggestion[4:]
        provider = rest.split(':', 1)[0] if rest else ''
        return self._is_chat_provider(provider) if provider else True

    def get_completions(self, document, complete_event) -> Iterable[Completion]:  # type: ignore
        text = getattr(document, 'text_before_cursor', '')
        if not isinstance(text, str) or not text.startswith('/model '):
            return
        prefix = text[len('/model '):]
        pref_norm = self._normalize(prefix)
        llm = self.llm
        if llm is None:
            return
        seen: set[str] = set()

        # Explicit all: path uses catalog suggestions
        if prefix.lower().startswith('all:'):
            try:
                for s in llm.catalog.get_all_models_suggestions(prefix):
                    if not self._is_chat_all_suggestion(s):
                        continue
                    yield Completion(s, start_position=-len(prefix))
            except Exception:
                pass
            return

        # Configured display names
        try:
            display_names = [
                name for name, cfg in (llm.registry.models_config or {}).items()
                if is_chat_model(
                    llm.registry.get_effective_model_config(name)
                    if hasattr(llm.registry, 'get_effective_model_config') else cfg
                )
            ]
        except Exception:
            display_names = []
        for name in sorted(display_names):
            if pref_norm == "" or pref_norm in self._normalize(name):
                if name not in seen:
                    seen.add(name)
                    yield Completion(name, start_position=-len(prefix))

        # provider:name and provider:alias
        try:
            items = [
                (name, cfg) for name, cfg in (llm.registry.models_config or {}).items()
                if is_chat_model(
                    llm.registry.get_effective_model_config(name)
                    if hasattr(llm.registry, 'get_effective_model_config') else cfg
                )
            ]
        except Exception:
            items = []
        for display, cfg in items:
            prov = (cfg or {}).get('provider', 'unknown')
            prov_pref = f"{prov}:{display}"
            if pref_norm == "" or pref_norm in self._normalize(prov_pref):
                if prov_pref not in seen:
                    seen.add(prov_pref)
                    yield Completion(prov_pref, start_position=-len(prefix))
            for a in (cfg or {}).get('alias', []) or []:
                if not isinstance(a, str):
                    continue
                prov_alias = f"{prov}:{a}"
                if pref_norm == "" or pref_norm in self._normalize(prov_alias):
                    if prov_alias not in seen:
                        seen.add(prov_alias)
                        yield Completion(prov_alias, start_position=-len(prefix))

        # Plain aliases
        for display, cfg in items:
            for a in (cfg or {}).get('alias', []) or []:
                if isinstance(a, str) and (pref_norm == "" or pref_norm in self._normalize(a)):
                    if a not in seen:
                        seen.add(a)
                        yield Completion(a, start_position=-len(prefix))

        # Search cached provider-wide catalogs to surface all:prov:model
        if pref_norm:
            try:
                for prov in (llm.get_providers() or []):
                    if not self._is_chat_provider(prov):
                        continue
                    mids = llm.catalog.get_all_models_for_provider(prov) or []
                    for mid in mids:
                        cand = f"all:{prov}:{mid}"
                        if cand in seen:
                            continue
                        if pref_norm in self._normalize(mid) or pref_norm in self._normalize(cand):
                            seen.add(cand)
                            yield Completion(cand, start_position=-len(prefix))
            except Exception:
                pass


class EggCompleter(Completer):
    """Top-level completer for Egg CLI.

    Features:
      - '/model ' values via ModelCompleter
      - '/updateAllModels ' provider names
      - '/thread ' thread ids with name/recap meta
      - '/child ' direct children ids with meta
      - '/spawnChildThread ' filesystem paths and conversation words
      - For plain user text: filesystem paths and conversation words
    """

    def __init__(self, db: Any, get_current_thread, llm: Any | None):
        self.db = db
        self.get_current_thread = get_current_thread
        self.llm = llm
        self.model_completer = ModelCompleter(llm)
        self.command_registry = create_default_command_registry()

    # ---- Helpers --------------------------------------------------------
    def _get_filesystem_suggestions(self, prefix: str):
        return [item["insert"] for item in filesystem_completion_items(prefix, limit=50)]

    def _recent_words(self, tid: str, limit_msgs: int = 200):
        # Extract recent words from snapshot messages for this thread
        words: list[str] = []
        try:
            th = self.db.get_thread(tid)
            if not th or not th.snapshot_json:
                return words
            import json as _json
            snap = _json.loads(th.snapshot_json)
            msgs = snap.get('messages', []) or []
            # consider only recent slice
            for m in msgs[-limit_msgs:]:
                try:
                    role = (m or {}).get('role')
                    if role not in ('user', 'assistant', 'system', 'tool'):
                        continue
                    txt = content_to_plain_text((m or {}).get('content'))
                    if not isinstance(txt, str) or not txt:
                        continue
                    # tokenize by words; keep 3+ char tokens
                    for w in re.findall(r"[A-Za-z0-9_]{3,}", txt):
                        words.append(w)
                except Exception:
                    continue
        except Exception:
            pass
        return words

    def _conversation_word_matches(self, fragment: str, tid: str) -> list[str]:
        if not fragment:
            return []
        recent = self._recent_words(tid)
        seen: set[str] = set()
        out: list[str] = []
        fl = fragment.lower()
        for w in reversed(recent):  # prefer more recent tokens
            wl = w.lower()
            if wl.startswith(fl) and wl not in seen:
                seen.add(wl)
                out.append(w)
        return out

    # ---- Completion routing --------------------------------------------
    def get_completions(self, document, complete_event) -> Iterable[Completion]:  # type: ignore
        text = getattr(document, 'text_before_cursor', '') or ''
        tid = None
        try:
            tid = self.get_current_thread()
        except Exception:
            pass

        # 1) Delegate /model completion to the original completer
        if text.startswith('/') and ' ' not in text:
            prefix = text
            for command in command_completion_names(self.command_registry):
                if command.startswith(prefix):
                    yield Completion(command, start_position=-len(prefix))
            return

        # Shared command-owned completions (for example /show record IDs) take
        # precedence over the legacy command-specific compatibility branches.
        if text.startswith('/') and ' ' in text:
            command_text, sub = text.split(' ', 1)
            command_name = command_text[1:]
            try:
                items = self.command_registry.complete(
                    command_name,
                    CommandContext(db=self.db, current_thread=tid, llm_client=self.llm),
                    sub,
                )
            except KeyError:
                items = []
            if items:
                fragment = sub.split()[-1] if sub.split() else ''
                for item in items:
                    if isinstance(item, Mapping):
                        insert = str(item.get('insert') or '')
                        display = str(item.get('display') or insert)
                        meta = str(item.get('meta') or '')
                        try:
                            replace_chars = int(item.get('replace', len(fragment)) or 0)
                        except Exception:
                            replace_chars = len(fragment)
                        if insert:
                            yield Completion(
                                insert,
                                start_position=-replace_chars,
                                display=display,
                                display_meta=meta,
                            )
                    elif isinstance(item, str) and item:
                        yield Completion(item, start_position=-len(fragment))
                return

        # 1) Delegate /model completion to the original completer
        if text.startswith('/model '):
            yield from self.model_completer.get_completions(document, complete_event)
            return

        # 2) Providers for /updateAllModels
        if text.startswith('/updateAllModels '):
            prefix = text[len('/updateAllModels '):]
            try:
                if self.llm:
                    provs = sorted(self.llm.get_providers() or [])
                else:
                    provs = []
            except Exception:
                provs = []
            for p in provs:
                if p.startswith(prefix):
                    yield Completion(p, start_position=-len(prefix))
            return

        # /setSandboxConfiguration: suggest config files from .egg/sandbox
        if text.startswith('/setSandboxConfiguration '):
            prefix = text[len('/setSandboxConfiguration '):]
            try:
                from eggthreads import get_srt_sandbox_configuration  # type: ignore
                from pathlib import Path as _Path

                cfg = get_srt_sandbox_configuration()
                cfg_dir = _Path(cfg.settings_dir)
                if cfg_dir.is_dir():
                    files = [p.name for p in cfg_dir.glob('*.json')]
                else:
                    files = []
            except Exception:
                files = []
            for name in sorted(files):
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix))
            return

        if text.startswith('/sessionOn '):
            prefix = text[len('/sessionOn '):]
            try:
                current_fragment = document.get_word_before_cursor(WORD=True)
            except Exception:
                current_fragment = prefix.split()[-1] if prefix.split() else ''
            frag_l = current_fragment.lower()
            for option in SESSION_ON_COMPLETIONS:
                if not frag_l or frag_l in option.lower():
                    yield Completion(option, start_position=-len(current_fragment))
            return

        if text.startswith('/sessionStop ') or text.startswith('/sessionReset '):
            command = '/sessionStop ' if text.startswith('/sessionStop ') else '/sessionReset '
            prefix = text[len(command):]
            try:
                current_fragment = document.get_word_before_cursor(WORD=True)
            except Exception:
                current_fragment = prefix.split()[-1] if prefix.split() else ''
            frag_l = current_fragment.lower()
            for option in SESSION_TARGET_COMPLETIONS:
                if not frag_l or option.startswith(frag_l):
                    yield Completion(option, start_position=-len(current_fragment))
            return

        if text.startswith('/skill '):
            prefix = text[len('/skill '):]
            try:
                from eggthreads.skills import list_skills
                names = [skill.name for skill in list_skills()]
            except Exception:
                names = []
            for name in sorted(names):
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix))
            return

        for command in ('/attachOutput', '/saveProviderArtifact', '/saveProviderOutput'):
            marker = command + ' '
            if text.startswith(marker):
                arg = text[len(marker):]
                if arg.endswith((' ', '\t')):
                    current_fragment = ''
                else:
                    try:
                        current_fragment = document.get_word_before_cursor(WORD=True)
                    except Exception:
                        current_fragment = arg.split()[-1] if arg.split() else ''
                if is_provider_artifact_id_position(command, arg):
                    for item in provider_artifact_completion_items(
                        artifact_workspace_from_db(self.db),
                        self.db,
                        tid,
                        current_fragment,
                    ):
                        yield Completion(
                            item.get('insert', ''),
                            start_position=-len(current_fragment),
                            display=item.get('display'),
                        )
                    return
                if is_provider_artifact_export_path_position(command, arg):
                    suggestions = self._get_filesystem_suggestions(current_fragment)
                    for s in suggestions:
                        yield Completion(s, start_position=-len(current_fragment))
                    return

        # 3) /thread: suggest thread ids (with name/recap meta)
        if text.startswith('/thread '):
            prefix = text[len('/thread '):]
            try:
                rows = list_threads(self.db) if list_threads else []
            except Exception:
                rows = []
            pref_l = prefix.lower()
            # Sort newest-first by created_at
            try:
                rows.sort(key=lambda r: getattr(r, 'created_at', ''), reverse=True)
            except Exception:
                pass
            for r in rows:
                tid2, name, recap = (getattr(r, 'thread_id', '') or ''), (getattr(r, 'name', '') or ''), (getattr(r, 'short_recap', '') or '')
                if (not prefix or
                    tid2.lower().startswith(pref_l) or tid2.lower().endswith(pref_l) or pref_l in tid2.lower() or
                    (isinstance(name, str) and pref_l in name.lower()) or
                    (isinstance(recap, str) and pref_l in recap.lower())):
                    disp = f"{tid2[-8:]}  {name}" if name else tid2[-8:]
                    meta = recap if isinstance(recap, str) else ''
                    yield Completion(tid2, start_position=-len(prefix), display=disp, display_meta=meta)
            return

        # 4) /delete: suggest thread ids to delete (exclude current thread)
        if text.startswith('/deleteThread '):
            prefix = text[len('/deleteThread '):]
            try:
                rows = list_threads(self.db) if list_threads else []
            except Exception:
                rows = []
            cur_id = None
            try:
                cur_id = self.get_current_thread()
            except Exception:
                cur_id = None
            pref_l = prefix.lower()
            # Sort newest-first by created_at
            try:
                rows.sort(key=lambda r: getattr(r, 'created_at', ''), reverse=True)
            except Exception:
                pass
            for r in rows:
                tid2, name, recap = (getattr(r, 'thread_id', '') or ''), (getattr(r, 'name', '') or ''), (getattr(r, 'short_recap', '') or '')
                if cur_id and tid2 == cur_id:
                    continue
                if (not prefix or
                    tid2.lower().startswith(pref_l) or tid2.lower().endswith(pref_l) or pref_l in tid2.lower() or
                    (isinstance(name, str) and pref_l in name.lower()) or
                    (isinstance(recap, str) and pref_l in recap.lower())):
                    disp = f"{tid2[-8:]}  {name}" if name else tid2[-8:]
                    meta = recap if isinstance(recap, str) else ''
                    yield Completion(tid2, start_position=-len(prefix), display=disp, display_meta=meta)
            return

        # 6) /spawn: support filesystem paths and conversation words
        if text.startswith('/spawnChildThread'):
            input_after = text[len('/spawnChildThread'):].lstrip()
            # current fragment according to prompt_toolkit WORD chars
            try:
                current_fragment = document.get_word_before_cursor(WORD=True)
            except Exception:
                current_fragment = ''

            # normal filesystem suggestions relative to cwd
            suggestions = self._get_filesystem_suggestions(current_fragment)
            for s in suggestions:
                yield Completion(s, start_position=-len(current_fragment))
            if suggestions:
                return

            # If we didn't yield any path suggestions, propose conversation words
            if tid:
                matches = self._conversation_word_matches(current_fragment, tid)
                for w in matches:
                    yield Completion(w, start_position=-len(current_fragment))
            return

        # 7) /wait: suggest thread ids similarly to /thread
        if text.startswith('/waitForThreads '):
            prefix = text[len('/waitForThreads '):]
            try:
                rows = list_threads(self.db) if list_threads else []
            except Exception:
                rows = []
            pref_l = prefix.lower()
            # Sort newest-first by created_at
            try:
                rows.sort(key=lambda r: getattr(r, 'created_at', ''), reverse=True)
            except Exception:
                pass
            for r in rows:
                tid2, name, recap = (
                    getattr(r, 'thread_id', '') or '',
                    getattr(r, 'name', '') or '',
                    getattr(r, 'short_recap', '') or '',
                )
                if (not prefix or
                    tid2.lower().startswith(pref_l) or tid2.lower().endswith(pref_l) or pref_l in tid2.lower() or
                    (isinstance(name, str) and pref_l in name.lower()) or
                    (isinstance(recap, str) and pref_l in recap.lower())):
                    disp = f"{tid2[-8:]}  {name}" if name else tid2[-8:]
                    meta = recap if isinstance(recap, str) else ''
                    yield Completion(tid2, start_position=-len(prefix), display=disp, display_meta=meta)
            return

        # 8) /continue: suggest message IDs from current thread
        if text.startswith('/continue '):
            prefix = text[len('/continue '):]
            # Handle msg_id= named argument
            search_term = prefix
            if 'msg_id=' in prefix:
                m = re.search(r'msg_id=(\S*)$', prefix)
                if m:
                    search_term = m.group(1)
            pref_l = search_term.lower()
            if tid:
                try:
                    th = self.db.get_thread(tid)
                    if th and th.snapshot_json:
                        import json as _json
                        snap = _json.loads(th.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        for msg in reversed(msgs):  # Most recent first
                            msg_id = msg.get('msg_id', '')
                            if not msg_id:
                                continue
                            role = msg.get('role', 'unknown')
                            content = content_to_plain_text(msg.get('content', ''))
                            content_preview = content[:40].replace('\n', ' ')
                            hay = f"{msg_id} {role} {content}".lower()
                            if pref_l and pref_l not in hay:
                                continue
                            disp = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                            yield Completion(msg_id, start_position=-len(search_term), display=disp)
                except Exception:
                    pass
            return

        # 9) /duplicateThread: suggest message IDs when in msg_id position
        if text.startswith('/duplicateThread '):
            prefix = text[len('/duplicateThread '):]
            # Handle msg_id= named argument
            search_term = prefix.split()[-1] if prefix.split() else ''
            if 'msg_id=' in prefix:
                m = re.search(r'msg_id=(\S*)$', prefix)
                if m:
                    search_term = m.group(1)
            pref_l = search_term.lower()
            # Only suggest messages if we're past the first arg (name)
            parts = prefix.split()
            if len(parts) >= 1 or 'msg_id=' in prefix:
                if tid:
                    try:
                        th = self.db.get_thread(tid)
                        if th and th.snapshot_json:
                            import json as _json
                            snap = _json.loads(th.snapshot_json)
                            msgs = snap.get('messages', []) or []
                            for msg in msgs:  # Chronological order for checkpoint selection
                                msg_id = msg.get('msg_id', '')
                                if not msg_id:
                                    continue
                                role = msg.get('role', 'unknown')
                                content = content_to_plain_text(msg.get('content', ''))
                                content_preview = content[:40].replace('\n', ' ')
                                hay = f"{msg_id} {role} {content}".lower()
                                if pref_l and pref_l not in hay:
                                    continue
                                disp = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                                yield Completion(msg_id, start_position=-len(search_term), display=disp)
                    except Exception:
                        pass
            return

        # (handled above)

        # 8) Generic filename completion for the last token when not a recognized command
        if text and not text.startswith('/'):
            parts = text.split()
            if parts and not text.endswith(' '):
                prefix_to_complete = parts[-1]
                suggestions = self._get_filesystem_suggestions(prefix_to_complete)
                if len(suggestions) == 1 and suggestions[0].lower() == prefix_to_complete.lower():
                    # exact match, don't spam completions
                    pass
                else:
                    for s in suggestions:
                        yield Completion(s, start_position=-len(prefix_to_complete))
                    if suggestions:
                        return

        # 8) Fallback: words from conversation for user text (not commands)
        if text and not text.strip().startswith('/'):
            m = re.search(r'(\w{3,})$', text)
            if m and tid:
                fragment = m.group(1)
                for w in self._conversation_word_matches(fragment, tid):
                    yield Completion(w, start_position=-len(fragment))
            return


# -------- eggdisplay autocomplete adapter ------------------------------------
def get_autocomplete_items(line: str, col: int, db: Any, get_current_thread, llm: Any | None, command_registry: Any | None = None) -> List[Dict[str, str]]:
    """Return a list of autocomplete items for eggdisplay InputPanel.

    Each item: {display: str, insert: str, optional replace: int}
    - insert is the text to insert at cursor.
    - replace (if present) deletes N characters before the cursor prior to insertion.
    """
    try:
        prefix = line[:col]
    except Exception:
        prefix = line
    command_registry = command_registry or create_default_command_registry()

    def _last_token(s: str) -> str:
        # Strip trailing whitespace to find the last token even if cursor is after a space
        # This handles cases like "/model gemini " where user typed space after token
        s_stripped = s.rstrip()
        m = re.search(r"([\w\-.:/~]+)$", s_stripped)
        return m.group(1) if m else ""

    def _mk_items(cands: List[str], base: str) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        seen = set()
        base_l = base or ""
        for c in cands:
            if not isinstance(c, str) or not c:
                continue
            # Check if candidate matches (prefix or containment)
            if base_l:
                c_match = c.lower().replace("'", "").replace('"', "")
                base_match = base_l.lower().replace("'", "").replace('"', "")
                if not (c_match.startswith(base_match) or base_match in c_match):
                    continue
            # Always return full value and replace the typed token
            # This handles the case where user types more after suggestions are fetched
            ins = c
            rep = len(base) if base_l else 0
            key = (c, ins, rep)
            if key in seen:
                continue
            seen.add(key)
            it: Dict[str, str] = {"display": c, "insert": ins}
            if rep:
                it["replace"] = str(rep)
            items.append(it)
        return items[:50]

    def _fs_suggestions(token: str) -> List[str]:
        return [item["insert"] for item in filesystem_completion_items(token, limit=50)]

    def _conversation_suggestions(tid: str, fragment: str) -> List[str]:
        if not fragment:
            return []
        words: list[str] = []
        try:
            th = db.get_thread(tid)
            if not th or not th.snapshot_json:
                return []
            import json as _json
            snap = _json.loads(th.snapshot_json)
            msgs = snap.get('messages', []) or []
            for m in msgs[-200:]:
                try:
                    role = (m or {}).get('role')
                    if role not in ('user', 'assistant', 'system', 'tool'):
                        continue
                    txt = content_to_plain_text((m or {}).get('content'))
                    if not isinstance(txt, str) or not txt:
                        continue
                    for w in re.findall(r"[A-Za-z0-9_]{3,}", txt):
                        words.append(w)
                except Exception:
                    continue
        except Exception:
            pass
        fl = fragment.lower()
        seen: set[str] = set()
        out: list[str] = []
        for w in reversed(words):
            wl = w.lower()
            if wl.startswith(fl) and wl not in seen:
                seen.add(wl)
                out.append(w)
        return out[:50]

    def _providers() -> List[str]:
        try:
            return sorted(llm.get_providers() or []) if llm else []
        except Exception:
            return []

    # Commands root
    if prefix.startswith('/'):
        sp = prefix.find(' ')
        if sp == -1:
            # Complete command name
            return _mk_items([c for c in command_completion_names(command_registry) if c.startswith(prefix)], prefix)

        cmd = prefix[:sp]
        sub = prefix[sp+1:]  # raw arg text
        arg_tok = _last_token(sub)

        def _thread_arg_items(arg_tok: str) -> List[Dict[str, str]]:
            """Return rich thread suggestions for commands that take a thread selector.

            Shared by /thread, /delete, and /wait so that the selector
            semantics are consistent across commands.
            """
            try:
                rows = list_threads(db) if list_threads else []
            except Exception:
                rows = []
            atok = (arg_tok or '').lower()
            try:
                rows.sort(key=lambda r: getattr(r, 'created_at', ''), reverse=True)
            except Exception:
                pass
            tid_cur = None
            try:
                tid_cur = get_current_thread()
            except Exception:
                tid_cur = None
            out_items: List[Dict[str, str]] = []
            for r in rows:
                tid = getattr(r, 'thread_id', '')
                name = getattr(r, 'name', '') or ''
                recap = getattr(r, 'short_recap', '') or ''
                if atok:
                    hay = f"{tid} {name} {recap}".lower()
                    if atok not in hay:
                        continue
                # Minimal display similar to /threads
                try:
                    streaming = bool(db.current_open(tid))
                except Exception:
                    streaming = False
                id_short = tid[-8:]
                cur_tag = '[bold cyan][CUR][/bold cyan] ' if tid_cur and tid == tid_cur else ''
                sflag = '[bold yellow]STREAMING[/bold yellow] ' if streaming else ''
                status = getattr(r, 'status', None) or 'unknown'
                if status == 'active':
                    status_tag = f"[bold green]{status}[/]"
                elif status == 'paused':
                    status_tag = f"[bold red]{status}[/]"
                else:
                    status_tag = f"[bold]{status}[/]"
                disp = f"{cur_tag}{sflag}[dim]{id_short}[/dim] {status_tag} - {recap}" + (f"  [dim]{name}[/dim]" if name else '')
                rep = len(arg_tok or '')
                out_items.append({"display": disp, "insert": tid, "replace": str(rep)})
                if len(out_items) >= 50:
                    break
            return out_items
        # /model
        if cmd == '/model':
            # Use existing ModelCompleter for parity
            items: List[str] = []
            try:
                mc = ModelCompleter(llm)
                class _Doc:  # minimal document for ModelCompleter
                    def __init__(self, t: str):
                        self.text_before_cursor = t
                suggestions = []
                for c in mc.get_completions(_Doc(f"/model {sub}"), None):
                    try:
                        txt = getattr(c, 'text', None)
                        if isinstance(txt, str) and txt:
                            suggestions.append(txt)
                    except Exception:
                        continue
                items = suggestions
            except Exception:
                items = []
            # Filter by the entire argument (supports multi-word search like "gemini flash")
            # Strip trailing whitespace from sub for matching
            sub_stripped = sub.rstrip()
            if sub_stripped:
                # Split into words and check if all words are found in the model name
                words = sub_stripped.lower().split()
                items = [s for s in items if all(w in s.lower() for w in words)]
            # Replace the ENTIRE argument after /model, not just the last token
            # This ensures "gemini flash" is fully replaced, not just "flash"
            return _mk_items(items, sub_stripped)

        # /updateAllModels providers
        if cmd == '/updateAllModels':
            # Filter providers by current arg token (prefix preferred, then contains)
            provs = _providers()
            atok = (arg_tok or '').lower()
            if atok:
                pref = [p for p in provs if p.lower().startswith(atok)]
                cont = [p for p in provs if atok in p.lower() and p not in pref]
                provs = pref + cont
            rep = len(arg_tok or '')
            out_items: List[Dict[str, str]] = []
            for p in provs:
                it: Dict[str, str] = {"display": p, "insert": p}
                if rep:
                    it["replace"] = str(rep)
                out_items.append(it)
            return out_items

        # /thread and /delete: rich suggestions id/name/recap
        if cmd in ('/thread', '/deleteThread'):
            return _thread_arg_items(arg_tok)

        # /wait: thread selectors, same suggestions as /thread
        if cmd == '/waitForThreads':
            return _thread_arg_items(arg_tok)

        # /spawnChildThread and /spawnAutoApprovedChildThread -> filesystem suggestions for arg
        if cmd in ('/spawnChildThread', '/spawnAutoApprovedChildThread'):
            return _mk_items(_fs_suggestions(arg_tok), arg_tok)

        if cmd in ('/attachOutput', '/saveProviderArtifact', '/saveProviderOutput'):
            if is_provider_artifact_id_position(cmd, sub):
                return provider_artifact_completion_items(
                    artifact_workspace_from_db(db),
                    db,
                    get_current_thread(),
                    arg_tok,
                )
            if is_provider_artifact_export_path_position(cmd, sub):
                path_tok = '' if sub.endswith((' ', '\t')) else arg_tok
                return _mk_items(_fs_suggestions(path_tok), path_tok)
            return []

        # /disabletool, /enabletool, /toolInfo: suggest known tool names from
        # the default ToolRegistry. We keep this best-effort and
        # local-only; if anything fails we simply return no suggestions.
        if cmd in ('/disableTool', '/enableTool', '/toolInfo'):
            try:
                from eggthreads.tools import create_default_tools  # type: ignore
                reg = create_default_tools()
                specs = reg.tools_spec()
                names: list[str] = []
                for spec in specs or []:
                    try:
                        fn = (spec or {}).get('function') or {}
                        nm = fn.get('name')
                        if isinstance(nm, str) and nm and nm not in names:
                            names.append(nm)
                    except Exception:
                        continue
            except Exception:
                names = []
            return _mk_items(names, arg_tok)

        if cmd == '/setSandboxConfiguration':
            # Suggest available .json files from .egg/sandbox
            try:
                from eggthreads import get_srt_sandbox_configuration  # type: ignore
                from pathlib import Path as _Path

                cfg = get_srt_sandbox_configuration()
                cfg_dir = _Path(cfg.settings_dir)
                if cfg_dir.is_dir():
                    files = [p.name for p in cfg_dir.glob('*.json')]
                else:
                    files = []
            except Exception:
                files = []
            return _mk_items(files, arg_tok)

        if cmd == '/sessionOn':
            return _mk_items(SESSION_ON_COMPLETIONS, arg_tok)

        if cmd in ('/sessionStop', '/sessionReset'):
            return _mk_items(SESSION_TARGET_COMPLETIONS, arg_tok)

        if cmd == '/sessionCleanup':
            return _mk_items(['dry-run', 'apply', 'older_than=1h', 'older_than=1d'], arg_tok)

        if cmd == '/skill':
            try:
                from eggthreads.skills import list_skills
                names = [skill.name for skill in list_skills()]
            except Exception:
                names = []
            return _mk_items(names, arg_tok)

        if cmd == '/togglePanel':
            opts = ['chat', 'children', 'system']
            atok = (arg_tok or '').lower()
            if atok:
                pref = [o for o in opts if o.startswith(atok)]
                cont = [o for o in opts if atok in o and o not in pref]
                opts = pref + cont
            return _mk_items(opts, arg_tok)

        if cmd == '/displayMode':
            opts = ['full-screen', 'inline']
            atok = (arg_tok or '').lower()
            if atok:
                pref = [o for o in opts if o.startswith(atok)]
                cont = [o for o in opts if atok in o and o not in pref]
                opts = pref + cont
            return _mk_items(opts, arg_tok)

        try:
            ctx = CommandContext(db=db, current_thread=get_current_thread(), llm_client=llm)
        except Exception:
            ctx = CommandContext(db=db, llm_client=llm)
        try:
            registry_items = command_registry.complete(cmd, ctx, sub)
        except KeyError:
            registry_items = []
        if registry_items:
            out_items: List[Dict[str, str]] = []
            string_items: List[str] = []
            for item in registry_items:
                if isinstance(item, str):
                    string_items.append(item)
                elif isinstance(item, Mapping):
                    out_items.append(dict(item))
            return out_items + _mk_items(string_items, arg_tok)

        # /setThreadPriority: suggest parameter names and thread IDs
        if cmd == '/setThreadPriority':
            # Check if we're completing after thread=
            if 'thread=' in sub:
                m = re.search(r'thread=(\S*)$', sub)
                if m:
                    # Complete thread ID after thread=
                    search_term = m.group(1)
                    return _thread_arg_items(search_term)

            # Otherwise suggest parameter names
            params = ['priority=', 'threshold=', 'apiTimeout=', 'thread=']
            atok = (arg_tok or '').lower()
            out_items: List[Dict[str, str]] = []
            for param in params:
                if not atok or atok in param.lower():
                    rep = len(arg_tok or '')
                    it: Dict[str, str] = {"display": param, "insert": param}
                    if rep:
                        it["replace"] = str(rep)
                    out_items.append(it)
            return out_items

        # /continue: suggest message IDs from current thread
        if cmd == '/continue':
            # Handle named argument: extract value after msg_id=
            search_term = arg_tok
            replace_len = len(arg_tok) if arg_tok else 0
            if 'msg_id=' in sub:
                m = re.search(r'msg_id=(\S*)$', sub)
                if m:
                    search_term = m.group(1)
                    replace_len = len(search_term)

            try:
                tid = get_current_thread()
            except Exception:
                tid = None
            if tid:
                try:
                    th = db.get_thread(tid)
                    if th and th.snapshot_json:
                        import json as _json
                        snap = _json.loads(th.snapshot_json)
                        msgs = snap.get('messages', []) or []
                        out_items: List[Dict[str, str]] = []
                        search_lower = (search_term or '').lower()
                        # Reverse order: most recent first for /continue
                        for msg in reversed(msgs):
                            msg_id = msg.get('msg_id', '')
                            if not msg_id:
                                continue
                            role = msg.get('role', 'unknown')
                            content = content_to_plain_text(msg.get('content', ''))
                            content_preview = content[:40].replace('\n', ' ')
                            if len(content) > 40:
                                content_preview += '...'
                            hay = f"{msg_id} {role} {content}".lower()
                            if search_lower and search_lower not in hay:
                                continue
                            disp = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                            it: Dict[str, str] = {"display": disp, "insert": msg_id}
                            if replace_len:
                                it["replace"] = str(replace_len)
                            out_items.append(it)
                            if len(out_items) >= 30:
                                break
                        return out_items
                except Exception:
                    pass
            return []

        # /duplicateThread: suggest message IDs when in msg_id position
        if cmd == '/duplicateThread':
            # Handle named argument: extract value after msg_id=
            search_term = arg_tok
            replace_len = len(arg_tok) if arg_tok else 0
            if 'msg_id=' in sub:
                m = re.search(r'msg_id=(\S*)$', sub)
                if m:
                    search_term = m.group(1)
                    replace_len = len(search_term)

            # Check if we're in msg_id position (second positional or after msg_id=)
            parts = sub.split()
            in_msg_id_position = len(parts) >= 1 or 'msg_id=' in sub

            if in_msg_id_position:
                try:
                    tid = get_current_thread()
                except Exception:
                    tid = None
                if tid:
                    try:
                        th = db.get_thread(tid)
                        if th and th.snapshot_json:
                            import json as _json
                            snap = _json.loads(th.snapshot_json)
                            msgs = snap.get('messages', []) or []
                            out_items: List[Dict[str, str]] = []
                            search_lower = (search_term or '').lower()
                            # Forward order for /duplicateThread (picking checkpoint)
                            for msg in msgs:
                                msg_id = msg.get('msg_id', '')
                                if not msg_id:
                                    continue
                                role = msg.get('role', 'unknown')
                                content = content_to_plain_text(msg.get('content', ''))
                                content_preview = content[:40].replace('\n', ' ')
                                if len(content) > 40:
                                    content_preview += '...'
                                hay = f"{msg_id} {role} {content}".lower()
                                if search_lower and search_lower not in hay:
                                    continue
                                disp = f"[{msg_id[-8:]}] <{role}> {content_preview}"
                                it: Dict[str, str] = {"display": disp, "insert": msg_id}
                                if replace_len:
                                    it["replace"] = str(replace_len)
                                out_items.append(it)
                                if len(out_items) >= 30:
                                    break
                            return out_items
                    except Exception:
                        pass
            return []

        # Other commands: no specific suggestions
        return []

    # Non-command: prefer filesystem then conversation words
    tok = _last_token(prefix)
    fs = _fs_suggestions(tok)
    if fs:
        return _mk_items(fs, tok)
    try:
        tid = get_current_thread()
    except Exception:
        tid = None
    if tid and tok:
        conv = _conversation_suggestions(tid, tok)
        if conv:
            return _mk_items(conv, tok)
    return []
