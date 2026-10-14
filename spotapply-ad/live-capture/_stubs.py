"""Stub the intentionally-omitted ML stack, exactly as CI does
(.github/workflows/ci.yml, step "App imports from requirements.txt alone").

Imported first by launcher.py and seed.py, so `import app...` works from the
trimmed requirements (no torch / faiss-cpu / sentence-transformers)."""
import importlib.util
import os
import sys
import types

for name, attrs in (('sentence_transformers', {'SentenceTransformer': object, 'CrossEncoder': object}),
                    ('faiss', {}), ('rank_bm25', {'BM25Okapi': object})):
    if importlib.util.find_spec(name) is None:
        m = types.ModuleType(name)
        m.__path__ = []
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
u = types.ModuleType('sentence_transformers.util')
u.cos_sim = lambda *a, **k: None
sys.modules.setdefault('sentence_transformers.util', u)

# The server resolves templates ("app/templates"), ./data/resume_master.* and
# the static dir relative to the working directory: run from the run root.
RUNROOT = os.environ.get("RUNROOT")
if RUNROOT:
    os.chdir(RUNROOT)
    if RUNROOT not in sys.path:
        sys.path.insert(0, RUNROOT)
