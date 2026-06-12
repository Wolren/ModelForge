"""
FixtureGeneratorService - legacy import compatibility.
Import from fixture_spec and fixture_runner instead.
"""
from .fixture_runner import CompleteFixtureService, FixtureRunner
from .fixture_spec import FixtureGeneratorService, FixtureResult, FixtureSpec, TestSuite

__all__ = [
    "FixtureGeneratorService",
    "FixtureSpec",
    "TestSuite",
    "FixtureResult",
    "FixtureRunner",
    "CompleteFixtureService",
]
