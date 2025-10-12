from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import re

from prompt_toolkit.completion import Completer, Completion


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
                rows = self.db.conn.execute(
                    "SELECT thread_id, name, short_recap, created_at FROM threads ORDER BY created_at DESC"
                ).fetchall()
            except Exception:
                rows = []
            pref_l = prefix.lower()
            for r in rows:
                tid2, name, recap = (r[0] or ''), (r[1] or ''), (r[2] or '')
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
                rows = self.db.conn.execute(
                    "SELECT thread_id, name, short_recap, created_at FROM threads ORDER BY created_at DESC"
                ).fetchall()
            except Exception:
                rows = []
            cur_id = None
            try:
                cur_id = self.get_current_thread()
            except Exception:
                cur_id = None
            pref_l = prefix.lower()
            for r in rows:
                tid2, name, recap = (r[0] or ''), (r[1] or ''), (r[2] or '')
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
                if cur_id:
                    try:
                        cur = self.db.conn.execute(
                            "SELECT c.child_id, t.name, t.short_recap FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=?",
                            (cur_id,)
                        )
                        rows = cur.fetchall()
                    except Exception:
                        rows = []
                pref_l = prefix.lower()
                for r in rows:
                    tid2, name, recap = (r[0] or ''), (r[1] or ''), (r[2] or '')
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

        # 7) Generic filename completion for the last token when not a recognized command
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
