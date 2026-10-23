# Development guidance

Use the existing isolated checkout; do not create worktrees unless requested.
Use Python 3.12+ and `uv sync --locked`; run `uv run pytest -q` for changes.
Keep business workflows in skills; enforce permissions in Python independently of skill text.
Never expose LOCAL_NO_AUTH mode through a public listener or tunnel.
Do not commit .env, tokens, SQLite databases, or real user data.
Current scope is documented in README.md. Real ChatGPT/OAuth validation is pending external configuration; do not claim it passed from local tests.

Prefer relational SQLite schemas and constraints for structured records. Domain-specific SQL examples are permitted; keep domain workflows out of Python. Preserve legacy JSON records and skill content unless explicitly migrating them.
