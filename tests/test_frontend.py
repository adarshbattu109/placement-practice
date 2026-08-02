"""index.html has no build step, so nothing catches a mistyped id but this.

A `$('who-nam')` fails silently at runtime — the button just stops working. This walks
every id the script reaches for and every endpoint it calls, and checks they exist.
"""

import re

from src.constants.filepaths import INDEX_HTML

# Ids built at runtime from a variable, so the literal never appears in the markup.
TEMPLATED = re.compile(r"^(view|login|signup)-")


def check():
    html = INDEX_HTML.read_text()
    ids = set(re.findall(r'\bid="([^"]+)"', html))

    wanted = set(re.findall(r"""\$\(['"]([^'"]+)['"]\)""", html))
    missing = {w for w in wanted if w not in ids and not TEMPLATED.match(w)}
    assert not missing, f"the script reaches for ids that do not exist: {sorted(missing)}"

    # Every view in VIEWS needs its section, and every wireAuth/data-go target its parts.
    views = re.search(r"const VIEWS = \[([^\]]+)\]", html).group(1)
    for view in re.findall(r"'([^']+)'", views):
        assert f"view-{view}" in ids, f"VIEWS lists {view!r} but there is no #view-{view}"
    for form in ("login", "signup"):
        for part in (f"{form}-form", f"{form}-oops", f"{form}-go"):
            assert part in ids, f"wireAuth({form!r}) needs #{part}"
    for target in set(re.findall(r'data-go="([^"]+)"', html)):
        assert f"view-{target}" in ids, f'data-go="{target}" has no #view-{target}'

    # The gate list must name real views, or show() would leave the header wrong.
    gates = re.search(r"const GATES = \[([^\]]+)\]", html).group(1)
    for gate in re.findall(r"'([^']+)'", gates):
        assert f"view-{gate}" in ids, f"GATES lists {gate!r} but there is no #view-{gate}"

    # get()/post() are the helpers; fetch() is used directly where the body is a Blob.
    called = set(re.findall(r"""(?:get|post|fetch)\(\s*['"](/[^'"?]+)""", html))
    from app.main import app

    served = {r.path for r in app.routes}
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

    print(f"ok — {len(wanted)} element ids resolve, {len(called)} called routes exist")


if __name__ == "__main__":
    check()
