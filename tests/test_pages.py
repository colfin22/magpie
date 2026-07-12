"""Guard against a whole class of bug: the pages' JavaScript lives inside Python
triple-quoted strings, so a JS escape like '\\n' written as '\n' gets consumed by
Python and silently breaks the <script> (the v0.12.0 backup-codes regression that
killed the settings page's JS entirely). Syntax-check the *rendered* scripts."""
import os
import shutil
import subprocess
import tempfile

import pytest

from app import main


@pytest.mark.parametrize("name", ["dashboard", "settings", "login"])
def test_rendered_page_script_is_valid_js(name):
    node = shutil.which("node")
    if not node:
        # CI installs node (see .github/workflows/ci.yml) precisely so this guard
        # cannot skip there — a silently-skipped guard is no guard at all.
        if os.environ.get("CI"):
            pytest.fail("node is missing in CI — the rendered-JS guard would silently skip")
        pytest.skip("node not available to syntax-check JS")
    html = {"dashboard": main.dashboard(), "settings": main.settings_page(),
            "login": main.login_page(0)}[name]
    if "<script>" not in html:
        return  # no inline script (login is a static form)
    js = html[html.index("<script>") + 8: html.index("</script>")]
    fd, p = tempfile.mkstemp(suffix=".js")
    os.write(fd, js.encode())
    os.close(fd)
    try:
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} page JS is invalid:\n{r.stderr}"
    finally:
        os.unlink(p)
