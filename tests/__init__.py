"""Everything that must pass before a push. Run all of it with `uv run python -m tests`.

No framework: plain asserts and one runner. Each module exposes `check()` and prints one
line. The `demo()` self-checks inside `app/` and `src/` stay where they are — the runner
calls them rather than duplicating them.
"""
