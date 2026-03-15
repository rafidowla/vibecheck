"""
pytest configuration — ensures the project root is on sys.path so that
``audit_tool`` is importable when running tests from any directory.
"""
import sys
from pathlib import Path

# Insert project root (the directory containing audit_tool/) at the front
# of sys.path. This is idiomatic for a src-less flat project layout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
