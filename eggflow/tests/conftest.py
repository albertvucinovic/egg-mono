import pytest

from eggflow import FlowExecutor, TaskStore, Config

@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_flow.db"
    return TaskStore(str(db_file))

@pytest.fixture
def executor(store):
    return FlowExecutor(store)

@pytest.fixture(autouse=True)
def mock_mode():
    Config.MOCK_MODE = True
    yield
    Config.MOCK_MODE = True
