"""Bound native math workers before Vosk, NumPy or Torch loads its DLLs."""

import os


def configure():
    # Audio arrives in small blocks. A BLAS team on every block costs more than
    # the calculation and may spin between blocks, even with no spoken command.
    # Silero sets its own (1–4) intra-op thread limit after Torch is imported.
    for key in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[key] = "1"
    os.environ["OMP_WAIT_POLICY"] = "PASSIVE"
    os.environ["KMP_BLOCKTIME"] = "0"
    os.environ["OPENBLAS_THREAD_TIMEOUT"] = "1"
