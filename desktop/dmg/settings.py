"""dmgbuild settings (desktop.yml "Package the dmg"): the window every Mac installer has.

Created: 2026-10-17

Tauri's own dmg step places the icons through Finder with AppleScript, which a CI runner (and a
sandboxed shell) cannot drive, so the release dmg opened as a plain listing. dmgbuild writes the
window's .DS_Store itself. Positions are icon centres in the 600x400 window: the usual installer, 112px icons a third and two thirds across.
"""
import os

app = os.environ["PP_APP"]
format = "UDZO"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = os.environ.get("PP_ICON") or None
background = os.environ["PP_BG"]
window_rect = ((200, 200), (600, 400))
icon_size = 112
text_size = 12
icon_locations = {os.path.basename(app): (150, 185), "Applications": (450, 185)}
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
