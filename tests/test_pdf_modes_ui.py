"""Exercise the complete upload script with native FormData and binary responses."""

from pathlib import Path
import subprocess


def test_pdf_mode_selection_download_and_retry():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root / "tests" / "pdf_modes_ui.cjs")],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
