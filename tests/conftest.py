"""Shared fixtures."""

import pytest

from surveycto_extractor.pii import PiiConfig, build_analyzer

# The small English model from the dev group; loading it takes seconds.
SM = PiiConfig(languages={"en": "en_core_web_sm"})


@pytest.fixture(scope="session")
def analyzer():
    """Presidio with the small English model, built once per run."""
    return build_analyzer(SM)
