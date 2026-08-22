from pathlib import Path

from vklass_mcp.session_store import SessionStore


def test_session_cookie_is_encrypted(tmp_path: Path) -> None:
    path = tmp_path / "session.enc"
    store = SessionStore(path, "a long configured secret")
    store.save("top-secret-cookie")
    assert b"top-secret-cookie" not in path.read_bytes()
    assert store.load() == "top-secret-cookie"
    store.delete()
    assert store.load() is None
