from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Dict, Optional

import re

from prompt_toolkit.completion import Completer, Completion

# Import eggthreads API helpers through the app's sys.path. The CLI already
# inserts eggthreads on sys.path, so this import should succeed at runtime.
try:
    from eggthreads import list_threads, list_children_with_meta
except Exception:
    list_threads = None  # type: ignore
    list_children_with_meta = None  # type: ignore


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
                    yield Completion(s, start_position=-len(prefix))
            except Exception:
                pass
            return

        # Configured display names
        try:
            display_names = list((llm.registry.models_config or {}).keys())
        except Exception:
            display_names = []
        for name in sorted(display_names):
            if pref_norm == "" or pref_norm in self._normalize(name):
                if name not in seen:
                    seen.add(name)
                    yield Completion(name, start_position=-len(prefix))

        # provider:name and provider:alias
        try:
            items = list((llm.registry.models_config or {}).items())
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
      - '/spawn ' filesystem paths and conversation words
      - For plain user text: filesystem paths and conversation words
    """

    def __init__(self, db: Any, get_current_thread, llm: Any | None):
        self.db = db
        self.get_current_thread = get_current_thread
        self.llm = llm
        self.model_completer = ModelCompleter(llm)

    # ---- Helpers --------------------------------------------------------
    def _get_filesystem_suggestions(self, prefix: str):
        import glob as _glob
        import os as _os
        try:
            expanded = _os.path.expanduser(prefix)
            escaped = _glob.escape(expanded)
            matches = _glob.glob(escaped + '*')
            out = []
            for m in matches:
                m2 = m.replace('\\', '/')
                if _os.path.isdir(m2):
                    out.append(m2 + '/')
                else:
                    out.append(m2)
            return out
        except Exception:
            return []

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
                    txt = (m or {}).get('content') or ''
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
        if text.startswith('/delete '):
            prefix = text[len('/delete '):]
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

        # 5) /child: suggest direct children of current thread (ids with name/recap meta)
        if text.startswith('/child'):
            if text == '/child':
                return
            if text.startswith('/child '):
                prefix = text[len('/child '):]
                cur_id = None
                try:
                    cur_id = self.get_current_thread()
                except Exception:
                    cur_id = None
                rows = []
                if cur_id and list_children_with_meta:
                    try:
                        rows = list_children_with_meta(self.db, cur_id)
                    except Exception:
                        rows = []
                pref_l = prefix.lower()
                for r in rows:
                    tid2, name, recap = (r[0] if isinstance(r, (list, tuple)) else getattr(r, 'thread_id', '')), (r[1] if isinstance(r, (list, tuple)) else getattr(r, 'name', '')), (r[2] if isinstance(r, (list, tuple)) else getattr(r, 'short_recap', ''))
                    if (not prefix or
                        tid2.lower().startswith(pref_l) or tid2.lower().endswith(pref_l) or pref_l in tid2.lower() or
                        (isinstance(name, str) and pref_l in name.lower()) or
                        (isinstance(recap, str) and pref_l in recap.lower())):
                        disp = f"{tid2[-8:]}  {name}" if name else tid2[-8:]
                        meta = recap if isinstance(recap, str) else ''
                        yield Completion(tid2, start_position=-len(prefix), display=disp, display_meta=meta)
                return

        # 6) /spawn: support filesystem paths and conversation words
        if text.startswith('/spawn'):
            input_after = text[len('/spawn'):].lstrip()
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
        if text.startswith('/wait '):
            prefix = text[len('/wait '):]
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
def get_autocomplete_items(line: str, col: int, db: Any, get_current_thread, llm: Any | None) -> List[Dict[str, str]]:
    """Return a list of autocomplete items for eggdisplay InputPanel.

    Each item: {display: str, insert: str, optional replace: int}
    - insert is the text to insert at cursor.
    - replace (if present) deletes N characters before the cursor prior to insertion.
    """
    try:
        prefix = line[:col]
    except Exception:
        prefix = line

    def _last_token(s: str) -> str:
        m = re.search(r"([\w\-.:/~]+)$", s)
        return m.group(1) if m else ""

    def _mk_items(cands: List[str], base: str) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        seen = set()
        base_l = base or ""
        for c in cands:
            if not isinstance(c, str) or not c:
                continue
            if base_l and c.lower().startswith(base_l.lower()):
                ins = c[len(base):]
                rep = 0
            elif not base_l:
                ins = c
                rep = 0
            else:
                # containment match -> replace the current token with full candidate
                ins = c
                rep = len(base)
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
        import os as _os
        if not token:
            return []
        expanded = _os.path.expanduser(token)
        base_dir = expanded
        needle = ''
        if not _os.path.isdir(expanded):
            base_dir = _os.path.dirname(expanded) or '.'
            needle = _os.path.basename(expanded)
        try:
            entries = _os.listdir(base_dir)
        except Exception:
            return []
        results: List[str] = []
        for name in entries:
            if needle and not name.startswith(needle):
                continue
            path = _os.path.join(base_dir, name)
            suffix = '/' if _os.path.isdir(path) else ''
            results.append(_os.path.join(base_dir, name) + suffix)
        results.sort(key=lambda s: (0 if s.endswith('/') else 1, s))
        return results[:50]

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
                    txt = (m or {}).get('content') or ''
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
            cmds = [
                '/help', '/model', '/updateAllModels', '/pause', '/resume',
                '/spawn', '/spawn_auto', '/wait', '/child', '/parent',
                '/children', '/threads', '/thread', '/delete', '/new', '/dup',
                '/schedulers', '/enterMode', '/toggle_auto_approval',
                '/toolson', '/toolsoff', '/disabletool', '/enabletool', '/toolstatus',
                '/quit',
            ]
            return _mk_items([c for c in cmds if c.startswith(prefix)], prefix)

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
                status = (db.get_thread(tid).status if db.get_thread(tid) else 'unknown')
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
            # Contains filtering based on the current token only (not whole sub-line)
            atok = (arg_tok or '').lower()
            if atok:
                items = [s for s in items if atok in s.lower()]
            # If the user typed additional non-whitespace after the last token, treat it as part of the token
            # e.g., '/model hi'+tab+'g' should filter to ones containing 'hig'.
            if sub and not sub.endswith(' '):
                # recompute token from end of sub
                import re as _re
                m = _re.search(r"([\w\-.:/~]+)$", sub)
                tok2 = m.group(1) if m else arg_tok
                if tok2 != arg_tok:
                    items = [s for s in items if tok2.lower() in s.lower()]
                    return _mk_items(items, tok2)
            return _mk_items(items, arg_tok)

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
        if cmd in ('/thread', '/delete'):
            return _thread_arg_items(arg_tok)

        # /wait: thread selectors, same suggestions as /thread
        if cmd == '/wait':
            return _thread_arg_items(arg_tok)

        # /child pattern -> show child ids
        if cmd == '/child':
            try:
                tid = get_current_thread()
                rows = list_children_with_meta(db, tid) if list_children_with_meta else []
            except Exception:
                rows = []
            ids = [(r[0] if isinstance(r, (list, tuple)) else getattr(r, 'thread_id', '')) for r in rows]
            return _mk_items(ids, arg_tok)

        # /spawn and /spawn_auto -> filesystem suggestions for arg
        if cmd in ('/spawn', '/spawn_auto'):
            return _mk_items(_fs_suggestions(arg_tok), arg_tok)

        # /disabletool and /enabletool: suggest known tool names from
        # the default ToolRegistry. We keep this best-effort and
        # local-only; if anything fails we simply return no suggestions.
        if cmd in ('/disabletool', '/enabletool'):
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
