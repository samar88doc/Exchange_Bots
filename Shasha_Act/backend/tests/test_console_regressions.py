"""Regression tests for the dashboard console bugs reported against Shasha_Act.

The reported symptoms were:

1. A browser refresh (for example while the Markets page was open) landed on the
   sign-in screen instead of the current page.
2. The "Details" control in the Overview open-positions widget did nothing.
3. Clicking "Refresh" on the Positions page showed the username/password screen.
4. Unrealized P/L and last price only moved on a manual refresh, and
   "Cancel pending entries" / "Pause trading" gave no response.

All four had the same root cause: ``renderSettings`` wrote the symbol filters into
the risk form, where the fields do not exist. The resulting TypeError aborted
``refreshAll``, which made the console treat a rendering failure as a session
failure, dropped the stored credential and stopped the live refresh loop.

These tests lock the fixed behaviour in place so the same class of bug cannot
return unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2] / "frontend"
SCRIPT = (ROOT / "assets" / "app.js").read_text()
HTML = (ROOT / "index.html").read_text()


def _function_body(name: str) -> str:
    """Return the source of a top-level ``function name(...)`` block.

    Brace matching ignores string and template literals so a ``{`` inside a message
    cannot end the block early.
    """
    start = SCRIPT.index(f"function {name}(")
    index = SCRIPT.index("(", start)
    depth = 0
    while True:
        char = SCRIPT[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    body_start = SCRIPT.index("{", index)
    depth = 0
    cursor = body_start
    quote: str | None = None
    while True:
        char = SCRIPT[cursor]
        if quote:
            if char == "\\":
                cursor += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : cursor + 1]
        cursor += 1


def test_symbol_filters_are_written_into_the_symbol_form() -> None:
    """The allow/deny inputs live in #symbol-form, not in the risk form."""
    assert 'id="symbol-form"' in HTML
    assert 'name="allow_symbols"' in HTML
    assert 'name="deny_symbols"' in HTML
    settings = _function_body("renderSettings")
    assert 'const symbolForm = $("#symbol-form")' in settings
    assert "symbolForm.elements.allow_symbols" in settings
    assert "symbolForm.elements.deny_symbols" in settings
    # The risk form must never be asked for a field it does not own: that was the
    # TypeError that aborted every refresh.
    assert "form.elements.allow_symbols" not in settings
    assert "form.elements.deny_symbols" not in settings


def test_a_render_failure_never_drops_the_stored_session() -> None:
    """A widget that cannot draw must not sign the operator out."""
    init = _function_body("init")
    # The stored credential is only dropped when the server rejects the identity.
    assert "await loadIdentity();" in init
    assert init.index("clearCredentials()") < init.index("await enterConsole()")
    # enterConsole reports the failure in place and still starts the refresh loop.
    enter = _function_body("enterConsole")
    assert "catch (error)" in enter
    assert "startPolling();" in enter
    assert "clearCredentials" not in enter

    sign_in = _function_body("signIn")
    assert "sessionEstablished" in sign_in
    assert "if (!sessionEstablished) clearCredentials();" in sign_in


def test_only_a_presented_session_can_be_expired_by_a_401() -> None:
    """A request sent without a token must not delete the saved credential."""
    api_body = _function_body("api")
    assert "const presentedToken = Boolean(state.sessionToken);" in api_body
    assert "if (presentedToken) clearCredentials();" in api_body
    # The credential endpoints answer 401 for wrong credentials; that is not an
    # expired browser session.
    assert "CREDENTIAL_PATHS" in api_body
    assert 'const CREDENTIAL_PATHS = ["/api/auth/login", "/api/auth/signup"];' in SCRIPT


def test_the_console_keeps_refreshing_and_restores_the_open_page() -> None:
    """Live values update on their own and a reload reopens the same page."""
    assert "const FAST_POLL_MS = 1000;" in SCRIPT
    assert "async function refreshPositionsFast()" in SCRIPT
    assert "state.fastPolling = setInterval" in SCRIPT
    assert "state.polling = setInterval" in SCRIPT
    # A hidden tab must not stop the loop for good, and the loop must not depend on
    # a previous request having succeeded.
    fast = _function_body("refreshPositionsFast")
    assert "!state.online" not in fast
    assert "safeRender(\"positions\", renderPositions)" in fast
    # The open page is mirrored into the URL fragment.
    assert "viewFromHash" in SCRIPT
    assert 'window.history.replaceState(null, "", `#${view}`)' in SCRIPT
    assert "hashchange" in SCRIPT


def test_the_live_refresh_does_not_rebuild_the_table_under_the_pointer() -> None:
    """A once-per-second refresh must not swallow a click on a row button."""
    assert "const positionsSignature = (items) =>" in SCRIPT
    assert "state.positionsSignature = positionsSignature(state.positions);" in SCRIPT
    assert "const pendingSignature = (items) =>" in SCRIPT
    assert "state.pendingSignature = pendingSignature(items);" in SCRIPT
    fast = _function_body("refreshPositionsFast")
    assert "positionsSignature(state.positions) !== state.positionsSignature" in fast
    assert "pendingSignature(state.pendingEntries) !== state.pendingSignature" in fast
    assert "if (state.pointerDown) return;" in fast
    assert 'document.addEventListener("mousedown"' in SCRIPT
    assert 'document.addEventListener("mouseup"' in SCRIPT


def test_the_open_positions_widget_has_a_working_details_action() -> None:
    """The Overview widget exposes a labelled Details control per position."""
    dashboard_positions = _function_body("renderDashboardPositions")
    assert 'data-action="details"' in dashboard_positions
    assert ">Details<" in dashboard_positions
    # The delegated handler must open the drawer for the details action.
    assert 'if (action === "details") openDrawer(id);' in SCRIPT


def test_operator_controls_always_answer_their_click() -> None:
    """Pause / cancel show progress and report the server answer."""
    assert "function setBusy(" in SCRIPT
    assert "async function submitAction(" in SCRIPT
    for action in ("togglePause", "cancelPending", "cancelEntry", "closeTrade"):
        body = _function_body(action)
        assert "submitAction(" in body, action
    assert 'setBusy($("#pause-button")' in SCRIPT
    assert 'setBusy($("#cancel-pending-button")' in SCRIPT
    # A refused action explains where it can be unlocked instead of failing silently.
    assert "const actionHint = (detail) =>" in SCRIPT
    assert "DISCLAIMER_NOT_ACCEPTED" in SCRIPT
    assert "SUBSCRIPTION_EXPIRED" in SCRIPT
    assert "actionHint(detail)" in SCRIPT


def test_the_positions_page_shows_a_live_timestamp() -> None:
    assert 'id="positions-last-updated"' in HTML
    assert "$(\"#positions-last-updated\")" in SCRIPT


@pytest.mark.parametrize("path", ["/api/auth/me"])
def test_identity_reports_the_session_expiry(test_client: TestClient, dashboard_headers: dict[str, str], path: str) -> None:
    """The console can warn the operator before the session runs out."""
    response = test_client.get(path, headers=dashboard_headers)
    assert response.status_code == 200
    assert "session_expires_at" in response.json()
