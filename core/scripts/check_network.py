"""Diagnostic réseau du terminal (équivalent de la commande ``check-network``).

    python core/scripts/check_network.py [--only ECB STOOQ] [--timeout 8] [--json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal_core.check_network import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
