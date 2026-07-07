import sys
from unittest import mock

import pytest

from fable.ai import FableAIError, _client


def test_missing_anthropic_raises_helpful_error():
    with mock.patch.dict(sys.modules, {"anthropic": None}):
        with pytest.raises(FableAIError, match="fable-tool\\[ai\\]"):
            _client()
