"""index.html has no build step, so nothing catches a mistyped id but this.

A `$('who-nam')` fails silently at runtime — the button just stops working. This walks
every id the script reaches for and every endpoint it calls, and checks they exist.
"""

import re

from src.constants.filepaths import INDEX_HTML

# Ids built at runtime from a variable, so the literal never appears in the markup.
TEMPLATED = re.compile(r"^(view|login|signup)-")


def _const_list(html, name):
    """The string entries of a `const <name> = [...]` array in index.html.

    A missing const is the failure this file exists to catch, so it asserts rather than
    returning empty — an empty list would pass every loop below without checking anything.
    """
    found = re.search(rf"const {name} = \[([^\]]+)\]", html)
    assert found, f"index.html has no `const {name} = [...]`"
    return re.findall(r"'([^']+)'", found.group(1))


def check():
    html = INDEX_HTML.read_text()
    ids = set(re.findall(r'\bid="([^"]+)"', html))

    wanted = set(re.findall(r"""\$\(['"]([^'"]+)['"]\)""", html))
    missing = {w for w in wanted if w not in ids and not TEMPLATED.match(w)}
    assert not missing, f"the script reaches for ids that do not exist: {sorted(missing)}"

    # Every view in VIEWS needs its section, and every wireAuth/data-go target its parts.
    for view in _const_list(html, "VIEWS"):
        assert f"view-{view}" in ids, f"VIEWS lists {view!r} but there is no #view-{view}"
    for form in ("login", "signup"):
        for part in (f"{form}-form", f"{form}-oops", f"{form}-go"):
            assert part in ids, f"wireAuth({form!r}) needs #{part}"
    for target in set(re.findall(r'data-go="([^"]+)"', html)):
        assert f"view-{target}" in ids, f'data-go="{target}" has no #view-{target}'

    # The gate list must name real views, or show() would leave the header wrong.
    for gate in _const_list(html, "GATES"):
        assert f"view-{gate}" in ids, f"GATES lists {gate!r} but there is no #view-{gate}"

    # get()/post() are the helpers; fetch() is used directly where the body is a Blob.
    called = set(re.findall(r"""(?:get|post|fetch)\(\s*['"](/[^'"?]+)""", html))
    from app.main import app

    # getattr: Starlette's BaseRoute has no `.path`; anything without one cannot match a
    # url starting with "/", so "" drops it from the comparison without hiding a route.
    served = {getattr(r, "path", "") for r in app.routes}
    assert called <= served, (
        f"the frontend calls routes the server does not serve: {called - served}"
    )

    # Templated urls (`/assessments/${id}/finish`) miss the check above, which is exactly
    # where a typo hides. Compare shapes: the interpolation stands in for one path param.
    shapes = {tuple("{}" if "{" in s else s for s in path.split("/")) for path in served}
    for url in set(re.findall(r"(?:get|post|fetch)\(\s*`(/[^`?]+)", html)):
        shape = tuple("{}" if "$" in s else s for s in url.split("/"))
        assert shape in shapes, f"the frontend calls `{url}`, which the server does not serve"
        called.add(url)

    # The cosmetic V1 gate claimed nothing was stored. It is now, so the claim must be gone.
    assert "isn't checked and nothing is stored" not in html, (
        "stale login hint still promises no storage"
    )

    _check_exits(html)

    print(
        f"ok — {len(wanted)} element ids resolve, {len(called)} called routes exist, "
        "every exit from a live interview is guarded"
    )


def _check_exits(html):
    """Nothing may navigate away from a live interview without going through leave().

    A sitting is closed only by POST /finish, so walking out of one leaves it unfinished
    forever with no route back — the questions and the takes only ever lived in that tab.
    This is a real bug that shipped: a sitting sat at 'started' permanently.
    """
    # data-go covers the crumbs and the auth links, including the interview's own '← Setup'.
    assert "b.onclick = () => leave(b.dataset.go)" in html, (
        "data-go buttons must navigate through leave(), or the interview crumb abandons a sitting"
    )
    for handler, expected in (("to-home", "leave('home')"), ("sign-out", "interviewLive()")):
        wired = re.search(rf"\$\('{handler}'\)\.onclick = [^;]*?;", html, re.DOTALL)
        assert wired and expected in wired.group(), f"#{handler} must check for a live interview"

    # The browser's own prompt is the only thing that can catch a tab close or a reload.
    assert "beforeunload" in html, "a reload mid-interview must warn before it loses the takes"

    # Abandoning has to clear the sitting, or the guard keeps firing on a sitting that is
    # no longer being submitted.
    abandon = re.search(r"\$\('abandon'\)\.onclick = [^}]*?\n\};", html, re.DOTALL)
    assert abandon and "state.assessment = null" in abandon.group(), (
        "abandoning must clear state.assessment"
    )


if __name__ == "__main__":
    check()
