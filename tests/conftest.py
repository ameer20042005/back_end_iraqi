"""Keep the suite independent of the local trained reranker and its real log."""

import os

# Must run before app.config is imported. The real-model test passes its path directly.
os.environ["DISTRICT_RERANKER_PATH"] = ""
