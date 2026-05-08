"""Thread management command mixins for the egg application."""
from __future__ import annotations

from typing import List, Optional

from eggthreads import (
    get_parent,
    list_threads,
)


class ThreadCommandsMixin:
    """Mixin providing thread management commands."""

    # ---- Thread selector helpers ----
    def select_threads_by_selector(self, selector: str) -> List[str]:
        """Select threads matching a selector (id, suffix, name, or recap fragment)."""
        try:
            rows = list_threads(self.db)
        except Exception:
            rows = []
        sel_l = (selector or '').lower()
        matches: List[str] = []
        for r in rows:
            if r.thread_id == selector:
                matches = [r.thread_id]
                break
        if not matches and sel_l:
            suf = [r.thread_id for r in rows if r.thread_id.lower().endswith(sel_l)]
            if suf:
                matches = suf
        if not matches and sel_l:
            cont = [r.thread_id for r in rows if sel_l in r.thread_id.lower()]
            if cont:
                matches = cont
        if not matches and sel_l:
            name_matches = [r.thread_id for r in rows if isinstance(r.name, str) and sel_l in r.name.lower()]
            if name_matches:
                matches = name_matches
        if not matches and sel_l:
            recap_matches = [r.thread_id for r in rows if isinstance(r.short_recap, str) and sel_l in r.short_recap.lower()]
            if recap_matches:
                matches = recap_matches
        return matches

    def resolve_single_thread_selector(self, selector: str) -> Optional[str]:
        """Resolve a free-form thread selector to a single thread_id.

        This wraps select_threads_by_selector with the same additional
        fallbacks and created_at ordering used by /thread and /delete so
        that other commands (e.g. /wait) can reuse the exact selector
        semantics.
        """
        sel = (selector or '').strip()
        if not sel:
            return None

        matches = self.select_threads_by_selector(sel)
        if not matches and ' ' in sel:
            sel_first = sel.split()[0]
            matches = self.select_threads_by_selector(sel_first)
        if not matches:
            try:
                rows_all = list_threads(self.db)
                suf = sel.lower()
                matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
            except Exception:
                matches = []
        if not matches:
            return None

        # Order by created_at newest-first, mirroring /thread behavior
        try:
            rows = list_threads(self.db)
            ca = {r.thread_id: r.created_at for r in rows}
        except Exception:
            ca = {}
        matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
        return matches[0]

    # ---- Thread hierarchy helpers ----
    def thread_root_id(self, tid: str) -> str:
        """Return the root thread id for any thread id.

        Egg's SubtreeScheduler is keyed by *root* thread id. The UI
        needs a reliable way to map any thread in a subtree to its root
        so we can accurately mark threads as "scheduled" in the tree.

        We primarily use the backend's get_parent() helper (shared
        semantics with eggthreads). We also keep a tiny SQL fallback in
        case get_parent is unavailable or fails.
        """
        from typing import Optional

        cur = tid
        seen: set[str] = set()
        # Hard cap to avoid infinite loops in case of corrupted parent
        # links.
        for _ in range(2048):
            if not cur:
                break
            if cur in seen:
                # Cycle detected; best-effort: treat the current node as
                # the root to avoid crashing the UI.
                return cur
            seen.add(cur)

            parent: Optional[str] = None
            try:
                parent = get_parent(self.db, cur)
            except Exception:
                parent = None
            if parent is None:
                # Fallback (should be equivalent to get_parent)
                try:
                    row = self.db.conn.execute(
                        'SELECT parent_id FROM children WHERE child_id=?',
                        (cur,),
                    ).fetchone()
                    parent = row[0] if row and row[0] else None
                except Exception:
                    parent = None

            if not parent:
                return cur
            cur = parent

        return cur or tid

    def is_thread_scheduled(self, tid: str) -> bool:
        """True if tid's root has an entry in active_schedulers."""
        rid = self.thread_root_id(tid)
        return rid in (self.active_schedulers or {})
