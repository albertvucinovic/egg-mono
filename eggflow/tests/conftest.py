import sys
import os

# When running pytest from ./eggflow, add .. to sys.path so "import eggflow" works
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
