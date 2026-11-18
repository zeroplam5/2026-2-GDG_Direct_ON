import threading

import pytest

from team.mock.mock_api import serve


@pytest.fixture
def mock_server():
    """Practice mock server on a free port; yields (base_url, store)."""
    srv, store = serve(port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", store
    srv.shutdown()
