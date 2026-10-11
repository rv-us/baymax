"""python -m robot_api: the Baymax agent CLI."""
import os
import sys

# The robot's ~/.zshrc puts ~/.local site-packages on PYTHONPATH (for the
# Hiwonder software). That shadows the venv's newer typing_extensions and
# breaks LangChain, so restart once without it.
if os.environ.get("PYTHONPATH") and not os.environ.get("BAYMAX_KEEP_PYTHONPATH"):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    os.execve(sys.executable, [sys.executable, "-m", "robot_api", *sys.argv[1:]], env)

from .agent import main  # noqa: E402

sys.exit(main())
