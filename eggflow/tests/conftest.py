import sys
import os
import pytest

# When running pytest from ./eggflow, add .. to sys.path so "import eggflow" works
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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
