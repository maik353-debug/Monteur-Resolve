import sys
from unittest import mock

import pytest

from monteur.ai import MonteurAIError, _client


def test_missing_anthropic_raises_helpful_error():
    with mock.patch.dict(sys.modules, {"anthropic": None}):
        with pytest.raises(MonteurAIError, match="monteur\\[ai\\]"):
            _client()
