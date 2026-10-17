"""dmgbuild settings (desktop.yml "Package the dmg"): the window every Mac installer has.

Created: 2026-10-17

Tauri's own dmg step places the icons through Finder with AppleScript, which a CI runner (and a
sandboxed shell) cannot drive, so the release dmg opened as a plain listing. dmgbuild writes the
window's .DS_Store itself. Positions are icon centres in the 660x400 window.
"""
import os

app = os.environ["PP_APP"]
format = "UDZO"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = os.environ.get("PP_ICON") or None
background = os.environ["PP_BG"]
window_rect = ((200, 200), (660, 400))
icon_size = 128
text_size = 12
icon_locations = {os.path.basename(app): (180, 180), "Applications": (480, 180)}
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
