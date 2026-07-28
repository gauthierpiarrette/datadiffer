"""datadiffer: semantic table diffs with segment attribution.

A maintained successor to data-diff. See https://github.com/gauthierpiarrette/datadiffer.
"""

__version__ = "0.1.0"

from datadiffer.api import diff
from datadiffer.errors import DatadifferError
from datadiffer.report.model import DiffReport

__all__ = ["diff", "DiffReport", "DatadifferError", "__version__"]
