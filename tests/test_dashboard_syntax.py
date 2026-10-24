import ast
from pathlib import Path


def test_streamlit_dashboard_module_parses():
    dashboard = Path(__file__).resolve().parents[1] / "app.py"
    ast.parse(dashboard.read_text(encoding="utf-8"), filename=str(dashboard))
