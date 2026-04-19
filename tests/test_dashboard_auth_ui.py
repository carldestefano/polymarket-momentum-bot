"""Static checks for the dashboard auth UI state behavior.

These do not run the JS in a browser; they assert on the static files
that bug-prone invariants (like the HTML ``hidden`` attribute actually
hiding the ``.locked`` screen) remain in place so a logged-in user
does not see the login dialog.
"""

from __future__ import annotations

from pathlib import Path

GUI_DIR = Path(__file__).resolve().parents[1] / "infra" / "gui"


def _read(name: str) -> str:
    return (GUI_DIR / name).read_text(encoding="utf-8")


def test_hidden_attribute_overrides_display_rules():
    """Without this rule, `.locked { display: flex }` beats `hidden`."""
    css = _read("style.css")
    assert "[hidden]" in css and "display: none !important" in css, (
        "style.css must force `[hidden] { display: none !important }` "
        "so `el.hidden = true` actually hides elements that have their "
        "own `display` rule (e.g. `.locked { display: flex }`)."
    )


def test_locked_screen_and_app_main_start_hidden():
    """Initial HTML state: locked and main both hidden until JS decides."""
    html = _read("index.html")
    assert 'id="locked-screen"' in html
    # The locked-screen and app-main elements both ship with `hidden` so
    # nothing is shown during the brief moment before init() runs.
    for needle in (
        'id="locked-screen" class="locked" hidden',
        'id="app-main" hidden',
    ):
        assert needle in html, f"expected `{needle}` in index.html"


def test_show_app_hides_locked_screen():
    js = _read("app.js")
    # showApp must hide the locked screen and reveal the main app.
    assert 'function showApp()' in js
    show_app_block = js.split('function showApp()', 1)[1].split('function ', 1)[0]
    assert '"locked-screen").hidden = true' in show_app_block
    assert '"app-main").hidden = false' in show_app_block
    assert '"logout-btn").hidden = false' in show_app_block


def test_show_locked_hides_app_and_logout():
    js = _read("app.js")
    assert 'function showLocked()' in js
    block = js.split('function showLocked()', 1)[1].split('function ', 1)[0]
    assert '"locked-screen").hidden = false' in block
    assert '"app-main").hidden = true' in block
    assert '"logout-btn").hidden = true' in block


def test_clear_auth_removes_all_tokens():
    js = _read("app.js")
    assert 'function clearAuth()' in js
    block = js.split('function clearAuth()', 1)[1].split('function ', 1)[0]
    # Must wipe all sessionStorage keys so a page reload after logout
    # does not leave the app thinking the user is still authed.
    assert 'sessionStorage.removeItem' in block
    assert 'Object.values(STORAGE)' in block


def test_is_authed_requires_unexpired_id_token():
    js = _read("app.js")
    assert 'function isAuthed()' in js
    block = js.split('function isAuthed()', 1)[1].split('function ', 1)[0]
    assert 'idToken' in block and 'expiresAt' in block


def test_init_routes_to_app_when_authed_and_locked_otherwise():
    js = _read("app.js")
    # The bootstrap must consult isAuthed() and call showApp()/showLocked()
    # accordingly so a reload with valid sessionStorage tokens does not
    # flash the login dialog.
    assert 'if (isAuthed())' in js
    assert 'showApp();' in js
    assert 'showLocked();' in js
