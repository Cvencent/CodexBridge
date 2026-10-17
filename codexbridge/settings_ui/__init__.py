from __future__ import annotations

from codexbridge.settings_ui.markup import MARKUP
from codexbridge.settings_ui.script import APP_JS, LOGIC_JS
from codexbridge.settings_ui.tokens import BASE_CSS


def render_settings_page() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodexBridge 管理中心</title>
  <link rel="icon" href="data:,">
  <style>
{BASE_CSS}
  </style>
</head>
<body>
{MARKUP}
<script>
{LOGIC_JS}
{APP_JS}
</script>
</body>
</html>
"""
