import sys
from pathlib import Path


CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for path in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if path in sys.path:
        sys.path.remove(path)
for path in (str(CURRENT_DIR), str(PROJECT_ROOT)):
    sys.path.insert(0, path)

from bit_playwright.bit_infractions_info import *  # noqa: F401,F403


def main():
    return get_infractions_info_all()


if __name__ == "__main__":
    main()
