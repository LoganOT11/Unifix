import os
import sys

# The image pipeline pulls in OpenCV/numpy → OpenBLAS, which by default spawns
# one thread per core. On thread-constrained hosts (e.g. WSL with a low
# RLIMIT_NPROC) that pthread_create can fail and segfault the worker. Cap BLAS
# threads unless the operator has already chosen a value.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

# The work-order extraction engine is vendored under unifix_odoo/processing/.
# It uses source-root absolute imports (`from processor...`, `from config...`),
# so put that directory on sys.path before anything imports it. This keeps the
# module self-contained — installs as a normal addon with no external setup.
_PROCESSING_ROOT = os.path.join(os.path.dirname(__file__), "processing")
if _PROCESSING_ROOT not in sys.path:
    sys.path.insert(0, _PROCESSING_ROOT)

from . import controllers
from . import models
