import os

# Prevent OpenMP/MKL multi-threading crashes on macOS ARM64 (Apple Silicon)
# when loading PyTorch (via sentence-transformers) or FAISS.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
