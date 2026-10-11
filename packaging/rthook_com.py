"""Select MTA before bundled pywin32/comtypes runtime hooks import Windows COM."""

import sys

if sys.platform == "win32":
    sys.coinit_flags = 0  # COINIT_MULTITHREADED
