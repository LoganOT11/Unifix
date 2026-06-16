import sys
import os

# Put the vendored processing source root (unifix_odoo/processing) on sys.path
# so the engine's absolute imports (`from config...`, `from processor...`) resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Shared test helpers — default audio-v1 resolver and validator shims
# ---------------------------------------------------------------------------
import pytest
from config import load_document_config
from db.memory import InMemoryProvider
from validator.fuzzy_resolver import FuzzyResolver

_audio_cfg = load_document_config("audio_v1")
_provider = InMemoryProvider()
_resolver = FuzzyResolver(
    _audio_cfg.validation.fuzzy_fields,
    _provider,
    thresholds=_audio_cfg.validation.thresholds,
)


@pytest.fixture(scope="session")
def audio_config():
    return _audio_cfg


@pytest.fixture(scope="session")
def provider():
    return _provider


@pytest.fixture(scope="session")
def resolver():
    return _resolver
