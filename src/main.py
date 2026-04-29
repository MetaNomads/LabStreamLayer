import sys
import os

if getattr(sys, 'frozen', False):
    base_dir = sys._MEIPASS
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from PyQt6.QtWidgets import QApplication
from main_window import MainWindow


APP_VERSION = "0.2.0"   # bump on user-visible changes


def _git_sha() -> str:
    try:
        import subprocess
        repo = os.path.dirname(os.path.abspath(__file__))
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=2
        ).decode().strip()
    except Exception:
        return "unknown"


def main():
    print(
        f"[LabStreamLayer] v{APP_VERSION}  build {_git_sha()}  "
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    app = QApplication(sys.argv)
    app.setApplicationName(f"Lab Stream Layer v{APP_VERSION}")
    app.setStyleSheet("""
        QMainWindow, QWidget {
            font-family: -apple-system, "Segoe UI", Arial, sans-serif;
            font-size: 12px;
        }
        QScrollBar:vertical {
            background: #1a1d27;
            width: 8px;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #2a2d3a;
            border-radius: 4px;
            min-height: 20px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
    """)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
