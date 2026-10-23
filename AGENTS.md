# Development guidance

Use the existing isolated checkout; do not create worktrees unless requested.
Use Python 3.12+ and `uv sync --locked`; run `uv run pytest -q` for changes.
Keep business workflows in skills; enforce permissions in Python independently of skill text.
Never expose LOCAL_NO_AUTH mode through a public listener or tunnel.
Do not commit .env, tokens, SQLite databases, or real user data.
Current scope is documented in README.md. Real ChatGPT/OAuth validation is pending external configuration; do not claim it passed from local tests.
