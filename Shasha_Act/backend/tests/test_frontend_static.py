from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "frontend"


def test_dashboard_assets_are_local_and_use_password_signin() -> None:
    html = (ROOT / "index.html").read_text()
    script = (ROOT / "assets" / "app.js").read_text()
    assert "/assets/styles.css" in html
    assert "/assets/app.js" in html
    assert "localhost" not in script.lower()
    assert "127.0.0.1" not in script
    assert "X-Session-Token" in script
    assert "X-Dashboard-Token" not in script
    assert "type=\"password\"" in html
    assert "Create the initial owner" in html
    assert "/api/auth/signup" in script
    assert "dashboard authentication cannot be switched off" in script
    assert "Daily P&amp;L ledger" in html



def test_dashboard_has_paper_first_safety_copy_and_unavailable_states() -> None:
    html = (ROOT / "index.html").read_text()
    script = (ROOT / "assets" / "app.js").read_text()
    assert "PAPER MODE" in html
    assert "LIVE is fail-closed" in html
    assert "Unavailable" in script
    assert "stale" in html.lower()
    assert "CLOSE POSITION" in script
    assert "/api/pending-entries" in script
    assert "/cancel-entry" in script
    assert "entry_expires_at" in script
