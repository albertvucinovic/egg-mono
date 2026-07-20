import asyncio
from pickle import dumps, loads

from eggflow import FlowExecutor, TaskStore
from eggthreads import ThreadsDB, get_parent, get_thread_tools_config

from eggopt.eggthreads import CreateRunRoots, RunRoots


def _run(task, flow_path):
    store = TaskStore(str(flow_path))
    try:
        return asyncio.run(FlowExecutor(store).run(task))
    finally:
        store.conn.close()


def test_run_roots_replay_as_authoritative_ancestry(tmp_path) -> None:
    threads_path = tmp_path / "threads.sqlite"
    flow_path = tmp_path / "flow.db"
    first = _run(
        CreateRunRoots(str(threads_path), "StudyRoot", "StrategyRunRoot"),
        flow_path,
    )
    replay = _run(
        CreateRunRoots(str(threads_path), "StudyRoot", "StrategyRunRoot"),
        flow_path,
    )

    assert isinstance(first, RunRoots)
    assert replay == first
    assert loads(dumps(first)) == first
    db = ThreadsDB(threads_path)
    db.init_schema()
    try:
        count = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        assert count == 2
        assert get_parent(db, first.study_thread_id) is None
        assert get_parent(db, first.strategy_thread_id) == first.study_thread_id
        assert not get_thread_tools_config(db, first.study_thread_id).llm_tools_enabled
        assert not get_thread_tools_config(db, first.strategy_thread_id).llm_tools_enabled
    finally:
        db.conn.close()
