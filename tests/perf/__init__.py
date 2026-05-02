"""Performance smoke gates for caqrs (Task #89).

Tests in this package are marked ``@pytest.mark.perf`` and deselected from
the default ``pytest -q`` invocation via ``addopts = "-m 'not perf'"`` in
``pyproject.toml``. Run them explicitly with ``just perf`` (or
``uv run pytest -m perf --benchmark-only --benchmark-disable-gc``).

The ``perf`` job in CI is non-blocking (``continue-on-error: true``) until
~1 week of stable baselines on the GitHub-hosted runner makes it safe to
promote to a hard gate.
"""
