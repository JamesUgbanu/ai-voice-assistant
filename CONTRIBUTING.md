# Contributing

Thanks for helping improve the voice service. Keep changes focused and preserve
the existing protocol and portfolio integration.

## Development setup

```bash
python3.12 -m venv .venv
. .venv/bin/activate
# Full model stack:
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -e .
# Or, for the stdlib unit suite and the stub demo:
pip install fastapi 'uvicorn[standard]' httpx
```

The unit suite is intentionally dependency-free:

```bash
python -m unittest discover -s tests
python -m py_compile app/*.py tests/*.py examples/*.py
```

Add tests for behavior changes. Keep protocol, audio-format, and turn-timing
changes out of refactors; if one is required, update `PROTOCOL.md`,
`INTEGRATION.md`, and the compatibility notes together.

## Style

- Python 3.11+, standard library first.
- Keep functions focused and explicit; prefer clear control flow over layers.
- Comments explain *why* (constraints, concurrency, audio edge cases), not what.
- Line length is 100 (`pyproject.toml`).
- Do not add a dependency without a demonstrated need; model packages are heavy.

## Privacy and security

- Never commit `.env`, tokens, private recordings, diagnostic captures, or logs
  containing visitor data.
- Use `examples/` for runnable examples; do not add personal voice samples.
- Report security issues privately (see `SECURITY.md`).

## Pull requests

- Describe the behavior change and any compatibility impact.
- Include test results and any checks you could not run.
- Do not deploy, publish, or push packages as part of a pull request.

By contributing, you agree that your contribution is licensed under the
repository's MIT license.
