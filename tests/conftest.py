import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SAMPLE_SVG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.svg")


@pytest.fixture
def sample_svg():
    return SAMPLE_SVG
