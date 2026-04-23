"""
FixtureGeneratorService - legacy import compatibility.
Import from fixture_spec and fixture_runner instead.
"""
from .fixture_spec import FixtureGeneratorService, FixtureSpec, TestSuite, FixtureResult
from .fixture_runner import FixtureRunner, CompleteFixtureService

__all__ = [
    "FixtureGeneratorService",
    "FixtureSpec", 
    "TestSuite",
    "FixtureResult",
    "FixtureRunner",
    "CompleteFixtureService",
]