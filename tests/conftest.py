"""Pytest configuration: hypothesis profiles and fixtures."""

import os

from hypothesis import HealthCheck, Verbosity, settings

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "dev",
    max_examples=50,
    verbosity=Verbosity.normal,
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
