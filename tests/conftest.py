import os, sys, tempfile
from pathlib import Path

os.environ["AGENTLENS_DATA"] = tempfile.mkdtemp(prefix="agentlens_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from app import demo_data
from app.ingest import KnowledgeBase


@pytest.fixture(scope="session")
def kb():
    demo_data.ensure_demo_data()
    return KnowledgeBase()
