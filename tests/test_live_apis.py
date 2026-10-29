"""Live smoke tests against Groq and the configured embedder. Run with: pytest --run-live"""

import pytest

from app import smoke
from app.config import settings

pytestmark = pytest.mark.live


@pytest.mark.parametrize("model", [settings.classifier_model, settings.generator_model])
def test_groq_model_supports_strict_json_schema(model):
    assert smoke.check_groq_model(model)["answer"] in {"yes", "no"}


def test_embedding():
    assert smoke.check_embedder() in {384, 1024}  # bge-small / voyage-4
