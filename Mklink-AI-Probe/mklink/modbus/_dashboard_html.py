"""Dashboard HTML template generator — profile-driven, zero external deps.

Uses an external HTML template file with placeholder markers:
  __PROFILE_JSON__  →  full profile JSON object
  __CSRF_TOKEN__    →  CSRF token string
  __MAX_POINTS__    →  max chart data points
  __LANG__          →  language preference (zh/en)

Template lookup order:
  1. .mklink/modbus_dashboard_template.html  (user-customized template)
  2. _dashboard_template.html                (built-in template, same dir)
"""

from __future__ import annotations

import json
import os

_TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILTIN_TEMPLATE = os.path.join(_TEMPLATE_DIR, "_dashboard_template.html")

# Markers used in the template file — chosen to not conflict with HTML/CSS/JS
_MARKER_PROFILE = "__PROFILE_JSON__"
_MARKER_CSRF = "__CSRF_TOKEN__"
_MARKER_MAX_PTS = "__MAX_POINTS__"
_MARKER_LANG = "__LANG__"


def _find_template() -> str:
    """Find the best template file path.

    Priority:
      1. Project-level .mklink/modbus_dashboard_template.html
      2. Built-in _dashboard_template.html (next to this .py file)
    """
    project_template = os.path.join(".mklink", "modbus_dashboard_template.html")
    if os.path.isfile(project_template):
        return project_template
    if os.path.isfile(_BUILTIN_TEMPLATE):
        return _BUILTIN_TEMPLATE
    raise FileNotFoundError(
        f"Dashboard template not found. Searched:\n"
        f"  {os.path.abspath(project_template)}\n"
        f"  {_BUILTIN_TEMPLATE}"
    )


def _load_lang_preference() -> str:
    """Load language preference from .mklink/lang.json, default 'zh'."""
    lang_file = os.path.join(".mklink", "lang.json")
    if os.path.isfile(lang_file):
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
                return data.get("lang", "zh")
        except Exception:
            pass
    return "zh"


def build_html(max_points: int, profile_json: str, csrf_token: str) -> str:
    """Build dashboard HTML by filling placeholders in the template."""
    template_path = _find_template()
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace(_MARKER_PROFILE, profile_json)
    html = html.replace(_MARKER_CSRF, csrf_token)
    html = html.replace(_MARKER_MAX_PTS, str(max_points))
    html = html.replace(_MARKER_LANG, _load_lang_preference())
    return html
