"""The capability catalog: every instruction the agent is able to carry out.

This is the single place that answers three questions at once:

  * **What can it do?** — the 48 numbered domains below, each entry naming one
    concrete, callable action.
  * **How risky is it?** — every entry declares a risk floor and a policy kind,
    so `safety.policy` can score it without knowing what the action does.
  * **What did the user just ask for?** — every entry carries trigger phrases in
    English, Hindi and Hinglish, which is what `capabilities.nlu` matches against
    to turn "make my laptop faster" into a plan instead of a shrug.

Handlers are stored as ``"module:function"`` strings and imported lazily. That
keeps `import ufo` cheap (nothing here touches the screen, the network or a
package that may not be installed) and means a broken optional dependency in
one module cannot stop the other forty from loading. A missing handler is
reported honestly by `registry.invoke` rather than silently doing nothing.

Nothing here decides what is *permitted* to run. A capability declaring
``risk=3`` is still refused below that level when the user's policy says so;
the catalog only says how bad the action is if it runs unchecked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Risk levels, mirrored from safety.policy to avoid an import cycle.
RISK_NONE, RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL = 0, 1, 2, 3, 4

# Policy kinds the classifier understands.
KINDS = (
    "ui", "mouse", "keyboard", "window", "system", "fs", "process",
    "network", "shell", "llm",
)


# --------------------------------------------------------------------------
# the 48 domains
# --------------------------------------------------------------------------
CATEGORIES: dict[int, str] = {
    1: "Apps, Windows & Software",
    2: "Mouse, Keyboard & Screen",
    3: "Files & Folders",
    4: "Documents & Office",
    5: "Clipboard & Text Tools",
    6: "Web & Browser",
    7: "Email, Calendar & Messaging",
    8: "Meetings & Work Tools",
    9: "System Settings",
    10: "Hardware & Monitoring",
    11: "Network & Connectivity",
    12: "Troubleshooting & Repair",
    13: "Media",
    14: "Image, Design & Creative",
    15: "Audio & Voice",
    16: "Developer",
    17: "Data & Analytics",
    18: "Search & Knowledge",
    19: "Notes, Tasks & Productivity",
    20: "Learning & Study",
    21: "Information & Assistance",
    22: "Printing & Scanning",
    23: "Cloud & Sync",
    24: "Phone & Device Link",
    25: "Smart Home & IoT",
    26: "Accessibility",
    27: "Remote & Virtual",
    28: "Backup & Recovery",
    29: "Security & Privacy",
    30: "Multi-User & Profiles",
    31: "Finance & Payments (always confirm)",
    32: "Travel & Life Admin",
    33: "Shopping",
    34: "Social & Content",
    35: "Games & Entertainment",
    36: "Advanced System",
    37: "Automation, Scheduling & Triggers",
    38: "Notifications & Alerts",
    39: "Time & Context Triggers",
    40: "Personal Routines & Modes",
    41: "Compound & Conditional",
    42: "Vague & Natural Language",
    43: "Multi-Language",
    44: "Agent Control",
    45: "Safety & Consent",
    46: "Feedback & Correction",
    47: "Agent Self-Management",
    48: "Fallback: anything not listed",
}


@dataclass(frozen=True)
class Capability:
    """One thing the agent can do.

    `risk` is a *floor*, not a verdict: `safety.policy` can raise it when the
    arguments look destructive, and can lower nothing below the floor. So
    `uninstall_app` stays high-risk even when the target looks harmless, and
    `set_volume` can still be escalated to high if the user types "delete" into
    its argument.
    """

    name: str
    category: int
    summary: str
    handler: str
    kind: str = "system"
    risk: int = RISK_LOW
    confirm: bool = False
    targets: str = ""          # what "target" means for this action
    args: str = ""             # what "args" means for this action
    aliases: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    needs: str = ""            # optional dependency, e.g. "winget"
    reverse: str = ""          # action that undoes this one, when there is one

    @property
    def dotted(self) -> str:
        module, _, func = self.handler.partition(":")
        return f"{module}.{func}"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "category": self.category,
            "domain": CATEGORIES.get(self.category, ""), "summary": self.summary,
            "handler": self.handler, "kind": self.kind, "risk": self.risk,
            "confirm": self.confirm, "targets": self.targets, "args": self.args,
            "aliases": list(self.aliases), "triggers": list(self.triggers),
            "needs": self.needs, "reverse": self.reverse,
        }

    def to_prompt(self) -> str:
        line = f"  {self.name}(target={self.targets or '-'}, args={self.args or '-'})"
        line += f"  risk={self.risk}" if self.risk >= RISK_MEDIUM else ""
        return line + f" — {self.summary}"


def _c(name: str, category: int, summary: str, handler: str, **kw) -> Capability:
    return Capability(name=name, category=category, summary=summary,
                      handler=handler, **kw)


# --------------------------------------------------------------------------
# 1. apps, windows & software
# --------------------------------------------------------------------------
APPS = [
    _c("app_list", 1, "List every installed program", "apps:list_installed",
       kind="system", targets="filter", aliases=("list_apps", "installed_apps")),
    _c("app_open", 1, "Open, launch or focus an app", "apps:open_app",
       kind="system", targets="app name", args="open|close|focus|restart|quit",
       aliases=("launch_app", "open_app", "close_app", "restart_app",
                "force_quit", "switch_app")),
    # kind="shell", not "system": a package operation is a winget command, and
    # kind="system" would score it critical and hard-block it as unrunnable.
    _c("app_install", 1, "Install a program", "apps:install",
       kind="shell", risk=RISK_HIGH, confirm=True, targets="name or id",
       needs="winget", aliases=("install_app",)),
    _c("app_uninstall", 1, "Uninstall a program", "apps:uninstall",
       kind="shell", risk=RISK_HIGH, confirm=True, targets="name or id",
       needs="winget", aliases=("remove_app",), reverse="app_install"),
    _c("app_update", 1, "Update one program", "apps:update",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="name or id",
       needs="winget", reverse="app_update_all"),
    _c("app_update_all", 1, "Update every program that has an update",
       "apps:update_all", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="all", needs="winget", aliases=("update_all_apps",)),
    _c("app_repair", 1, "Repair a broken program", "apps:repair",
       kind="system", risk=RISK_HIGH, confirm=True, targets="name", needs="winget"),
    _c("app_startup_add", 1, "Start a program when you log in",
       "apps:add_startup", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="app name", args="command", reverse="app_startup_remove"),
    _c("app_startup_remove", 1, "Stop a program starting at logon",
       "apps:remove_startup", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="name", reverse="app_startup_add"),
    _c("app_startup_list", 1, "List startup programs and their impact",
       "apps:startup_report", kind="system", targets="",
       aliases=("startup_apps", "startup_impact")),
    _c("app_startup_toggle", 1, "Enable or disable a startup program",
       "apps:toggle_startup", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="name", args="on|off"),
    _c("app_bloatware", 1, "List (and optionally remove) bundled software",
       "apps:bloatware", kind="system", risk=RISK_HIGH, confirm=True,
       targets="", args="yes to actually uninstall", needs="winget"),
    _c("app_pin", 1, "Pin an app to the taskbar", "apps:pin_taskbar",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="app id or path",
       reverse="app_unpin"),
    _c("app_unpin", 1, "Unpin an app from the taskbar", "apps:unpin_taskbar",
       kind="system", risk=RISK_LOW, targets="app id", reverse="app_pin"),
    _c("app_run_admin", 1, "Launch a program as administrator",
       "apps:run_as_admin", kind="system", risk=RISK_HIGH, confirm=True,
       targets="program or file"),
    _c("app_default_set", 1, "Change the default app for a file type",
       "apps:set_default_app", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="extension", args="program"),
    _c("app_default_list", 1, "Show the default apps Windows knows about",
       "apps:list_default_apps", kind="system", targets=""),
    _c("app_fonts", 1, "List, install or remove a font", "apps:fonts",
       kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="font name or .ttf path", args="install|remove|list",
       aliases=("manage_fonts",)),
    _c("app_extensions", 1, "List browser extensions", "apps:browser_extensions",
       kind="system", targets="browser", aliases=("manage_extensions",)),
    _c("window_manage", 1, "Minimise, maximise, snap or move a window",
       "apps:manage_window", kind="window", risk=RISK_LOW,
       targets="window title", args="minimize|maximize|restore|snap_left|"
       "snap_right|snap_max|move_to_monitor_1|minimize_all|show_desktop",
       aliases=("minimize_all", "snap_window", "move_to_monitor",
                "maximize_window", "restore_window")),
    _c("window_desktops", 1, "Switch virtual desktop", "apps:virtual_desktops",
       kind="window", targets="", args="next|prev|new|close"),
]

# --------------------------------------------------------------------------
# 2. mouse, keyboard & screen
# --------------------------------------------------------------------------
SCREEN = [
    _c("screen_capture", 2, "Take a screenshot, optionally of one region",
       "screenops:capture", kind="system", targets="out path or region x,y,w,h",
       aliases=("screenshot", "annotate_screenshot")),
    _c("screen_describe", 2, "Say what is on the screen right now",
       "screenops:describe", kind="ui", targets="", aliases=("describe_screen",)),
    _c("screen_read_text", 2, "Read the text on screen with OCR",
       "screenops:read_text", kind="ui", targets="window title or region"),
    _c("screen_find_click", 2, "Find a piece of text on screen and click it",
       "screenops:find_and_click", kind="ui", risk=RISK_MEDIUM,
       targets="text to find", args="double"),
    _c("screen_wait_for", 2, "Wait until something appears on screen",
       "screenops:wait_for", kind="ui", targets="text or window title",
       args="timeout seconds", aliases=("wait_until",)),
    _c("keyboard_type", 2, "Type text, press a key or a shortcut",
       "screenops:type_text", kind="keyboard", targets="optional control",
       args="text or keys", aliases=("type_text", "press_keys")),
    _c("keyboard_undo", 2, "Undo or redo the last change", "screenops:undo",
       kind="keyboard", targets="", args="redo to go forward"),
    _c("keyboard_select_all", 2, "Select everything in the focused control",
       "screenops:select_all", kind="keyboard", targets="",
       aliases=("select_all",)),
    _c("keyboard_copy", 2, "Copy from the focused control", "screenops:copy_gui",
       kind="keyboard", targets="", aliases=("copy",)),
    _c("keyboard_paste", 2, "Paste into the focused control",
       "screenops:paste_gui", kind="keyboard", targets="", aliases=("paste",)),
    _c("keyboard_hotkey", 2, "Press a shortcut such as ctrl+s",
       "screenops:hotkey", kind="keyboard", targets="keys",
       args="", aliases=("hotkey", "press", "shortcut", "key_combo")),

    # The GUI verbs. `actions.gui` implemented all of these against a UI
    # Automation control but nothing could route to them, so "double-click the
    # file" had no capability and `ufo do click` did not exist. Each is its own
    # capability so it gets its own risk score and its own triggers.
    _c("mouse_click", 2, "Click a named control", "screenops:click_gui",
       kind="ui", risk=RISK_MEDIUM, targets="", args="control, or 'the Save button in Notepad'",
       aliases=("click", "press_button")),
    _c("mouse_double_click", 2, "Double-click a named control",
       "screenops:double_click_gui", kind="ui", risk=RISK_MEDIUM, targets="",
       args="control", aliases=("double_click", "dblclick")),
    _c("mouse_right_click", 2, "Right-click a named control",
       "screenops:right_click_gui", kind="ui", risk=RISK_MEDIUM, targets="",
       args="control", aliases=("right_click", "context_click")),
    _c("mouse_hover", 2, "Hover a named control", "screenops:hover_gui",
       kind="mouse", targets="", args="control", aliases=("hover",)),
    _c("mouse_drag", 2, "Drag a control somewhere else", "screenops:drag_gui",
       kind="mouse", risk=RISK_MEDIUM, targets="", args="control then 'x,y'",
       aliases=("drag", "drag_and_drop", "drop")),
    _c("mouse_scroll", 2, "Scroll a window", "screenops:scroll", kind="mouse",
       targets="up|down|left|right", args="amount", aliases=("scroll",)),
    _c("gui_invoke", 2, "Press a control the way a person would",
       "screenops:invoke_gui", kind="ui", targets="", args="control",
       aliases=("invoke", "activate")),
    _c("gui_get_text", 2, "Read a control's text", "screenops:get_text_gui",
       kind="ui", targets="", args="control", aliases=("get_text", "read_control")),
    _c("gui_set_text", 2, "Replace a control's text", "screenops:set_text_gui",
       kind="ui", risk=RISK_MEDIUM, targets="", args="control then new text",
       aliases=("set_text", "fill")),
    _c("gui_append_text", 2, "Append to a control's text",
       "screenops:append_text_gui", kind="ui", risk=RISK_MEDIUM, targets="",
       args="control then text", aliases=("append_text",)),
    _c("gui_select", 2, "Pick an item from a list or combo box",
       "screenops:select_combo_gui", kind="ui", risk=RISK_MEDIUM, targets="",
       args="control then item", aliases=("select_combo_item", "pick")),
    _c("gui_toggle", 2, "Tick or untick a checkbox", "screenops:toggle_gui",
       kind="ui", risk=RISK_MEDIUM, targets="", args="control then on|off",
       aliases=("toggle", "checkbox")),
    _c("gui_slider", 2, "Move a slider", "screenops:slider_gui", kind="ui",
       risk=RISK_MEDIUM, targets="", args="control then percent",
       aliases=("set_slider",)),
    _c("gui_expand", 2, "Expand a tree or section", "screenops:expand_gui",
       kind="ui", targets="", args="control", aliases=("expand",)),
    _c("gui_collapse", 2, "Collapse a tree or section", "screenops:collapse_gui",
       kind="ui", targets="", args="control", aliases=("collapse",)),
    _c("screen_annotate", 2, "Save a screenshot with every control numbered",
       "screenops:annotate", kind="ui", targets="", aliases=("annotate",)),
]

# --------------------------------------------------------------------------
# 3. files & folders
# --------------------------------------------------------------------------
FILES = [
    _c("file_find", 3, "Find files by name, date, type or contents",
       "fileops:find", kind="fs", targets="folder", args="pattern or query",
       aliases=("find_files", "file_search")),
    _c("file_organise", 3, "Sort a folder: downloads, duplicates, big files",
       "fileops:organise", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="folder", args="downloads|duplicates|large|by_type|by_date",
       aliases=("sort_downloads", "find_duplicates", "find_large_files")),
    _c("file_batch_rename", 3, "Rename many files by a rule",
       "fileops:batch_rename", kind="fs", risk=RISK_HIGH, confirm=True,
       targets="folder or glob", args="find -> replace",
       aliases=("bulk_rename",)),
    _c("file_bulk_move", 3, "Move many files that match a rule",
       "fileops:bulk_move", kind="fs", risk=RISK_HIGH, confirm=True,
       targets="folder", args="pattern -> destination", aliases=("bulk_move",)),
    _c("file_permissions", 3, "Show or change a file's permissions",
       "fileops:permissions", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="path", args="user:read|full|none"),
    _c("file_visibility", 3, "Hide or unhide a file or folder",
       "fileops:visibility", kind="fs", risk=RISK_LOW, targets="path",
       args="hide|show"),
    _c("file_encrypt", 3, "Encrypt or decrypt a file", "fileops:encrypt",
       kind="fs", risk=RISK_HIGH, confirm=True, targets="path",
       args="on|off", aliases=("decrypt_file",)),
    _c("file_archive", 3, "Zip or unzip a file or folder", "fileops:archive",
       kind="fs", risk=RISK_LOW, targets="path", args="zip|unzip [dest]"),
    _c("file_recycle", 3, "Empty the Recycle Bin", "fileops:empty_recycle_bin",
       kind="fs", risk=RISK_HIGH, confirm=True, targets="",
       aliases=("empty_recycle_bin",)),
    _c("file_reveal", 3, "Open the folder containing a file",
       "fileops:reveal", kind="fs", targets="path", args="select to highlight"),
    _c("file_delete", 3, "Delete a file or folder", "fileops:delete",
       kind="fs", risk=RISK_HIGH, confirm=True, targets="path",
       args="recursive", aliases=("delete_file",)),
]

# --------------------------------------------------------------------------
# 4. documents & office
# --------------------------------------------------------------------------
DOCS = [
    _c("doc_create", 4, "Create a Word, Excel, PowerPoint or PDF file",
       "docs:create", kind="fs", risk=RISK_LOW, confirm=True, targets="out path",
       args="docx|xlsx|pptx|pdf|pptx content", needs="python-docx|openpyxl|"
       "python-pptx|reportlab", aliases=("create_document", "create_presentation",
                                           "create_spreadsheet")),
    _c("doc_read", 4, "Read the text of a document", "docs:read",
       kind="fs", targets="path", args="page range"),
    _c("doc_edit", 4, "Edit, summarise, translate or proofread a document",
       "docs:edit_text", kind="llm", risk=RISK_MEDIUM, confirm=True,
       targets="path", args="edit|summarize|translate|proofread|shorten"),
    _c("doc_convert", 4, "Convert a document to another format", "docs:convert",
       kind="fs", risk=RISK_LOW, targets="source", args="target format",
       needs="soffice|pypdf"),
    _c("doc_pdf_split", 4, "Split a PDF into pages or ranges", "docs:pdf_split",
       kind="fs", risk=RISK_LOW, targets="pdf", args="page or start-end",
       needs="pypdf"),
    _c("doc_pdf_merge", 4, "Join several PDFs into one", "docs:pdf_merge",
       kind="fs", risk=RISK_LOW, targets="out.pdf", args="inputs",
       needs="pypdf"),
    _c("doc_pdf_extract", 4, "Extract text, tables or images from a PDF",
       "docs:pdf_extract", kind="fs", targets="pdf", args="text|tables|images",
       needs="pypdf"),
    _c("doc_form_fill", 4, "Fill the fields of a PDF form", "docs:form_fill",
       kind="fs", risk=RISK_HIGH, confirm=True, targets="pdf",
       args="name=value;name=value", needs="pypdf"),
    _c("doc_chart", 4, "Make a chart from data", "docs:chart", kind="fs",
       targets="csv", args="x column, y column, chart type",
       needs="matplotlib", aliases=("make_chart", "plot_graph")),
    _c("doc_mail_merge", 4, "Merge a template with a list of records",
       "docs:mail_merge", kind="fs", risk=RISK_MEDIUM, targets="template",
       args="csv and output folder"),
    _c("doc_slides", 4, "Turn notes or an outline into a slide deck",
       "docs:notes_to_slides", kind="fs", targets="notes file or text",
       args="output pptx", needs="python-pptx"),
    _c("doc_template", 4, "Draft a document of a given kind from scratch",
       "docs:draft", kind="llm", targets="invoice|letter|report|resume|minutes",
       args="topic and details"),
]

# --------------------------------------------------------------------------
# 5. clipboard & text
# --------------------------------------------------------------------------
TEXT = [
    _c("clip_get", 5, "Read the clipboard", "cliptext:get", kind="ui",
       targets="", aliases=("clipboard_read",)),
    _c("clip_set", 5, "Put text on the clipboard", "cliptext:set_clip",
       kind="ui", targets="text", aliases=("clipboard_write",)),
    _c("clip_clear", 5, "Empty the clipboard", "cliptext:clear", kind="ui",
       targets="", aliases=("clear_clipboard",)),
    _c("clip_history", 5, "Show, search or pin clipboard history",
       "cliptext:history", kind="ui", targets="list|search|pin|forget",
       args="query or index"),
    _c("text_case", 5, "Change the case of some text", "cliptext:change_case",
       kind="ui", targets="upper|lower|title|camel|sentence", args="text"),
    _c("text_count", 5, "Count words, characters, lines and reading time",
       "cliptext:count", kind="ui", targets="text or file",
       aliases=("count_words",)),
    _c("text_transform", 5, "De-duplicate, sort, trim or join lines",
       "cliptext:transform", kind="ui",
       targets="dedupe|sort|reverse|number|unique|trim|join", args="text"),
    _c("text_replace", 5, "Find and replace across files", "cliptext:replace",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="folder",
       args="find -> replace", aliases=("find_replace",)),
    _c("text_format_json", 5, "Pretty-print or minify JSON and code",
       "cliptext:format_json", kind="ui", targets="json or text",
       args="minify|sort_keys"),
]

# --------------------------------------------------------------------------
# 6. web & browser
# --------------------------------------------------------------------------
WEB = [
    _c("web_open", 6, "Open a site in the browser", "web:open_site",
       kind="network", risk=RISK_MEDIUM, targets="url or search",
       aliases=("open_site", "browse")),
    _c("web_search", 6, "Search the web", "web:search", kind="network",
       risk=RISK_MEDIUM, targets="query", aliases=("search_web",)),
    _c("web_tabs", 6, "Open, close, switch or reopen a browser tab",
       "web:tabs", kind="keyboard", targets="new|close|next|prev|reopen|list",
       aliases=("browser_tab",)),
    _c("web_bookmark", 6, "Bookmark a page, or list bookmarks", "web:bookmark",
       kind="ui", targets="url or bookmark name", args="add|list|remove"),
    _c("web_download", 6, "Download a file from the web", "web:download",
       kind="network", risk=RISK_MEDIUM, targets="url", args="destination"),
    _c("web_fill_form", 6, "Fill a web form that is already on screen",
       "web:fill_form", kind="ui", risk=RISK_MEDIUM, targets="",
       args="label=value;label=value", aliases=("fill_form",)),
    _c("web_login", 6, "Open a sign-in page for you to complete yourself",
       "web:login", kind="ui", risk=RISK_HIGH, confirm=True, targets="site",
       aliases=("log_in",)),
    _c("web_purchase", 6, "Add to cart or book something, stopping before payment",
       "web:purchase", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="what to buy", args="site or url",
       aliases=("add_to_cart", "book_ticket", "book_table")),
    _c("web_compare", 6, "Compare prices or options across sites", "web:compare",
       kind="network", risk=RISK_MEDIUM, targets="product or route",
       args="sites", aliases=("compare_prices",)),
    _c("web_monitor", 6, "Watch a page and tell you when it changes",
       "web:monitor", kind="network", risk=RISK_LOW, targets="url",
       args="text to watch for", aliases=("monitor_page", "page_watch")),
    _c("web_scrape", 6, "Pull text, links or tables out of a page",
       "web:scrape", kind="network", risk=RISK_MEDIUM, targets="url",
       args="text|links|tables|title", aliases=("scrape_data",)),
    _c("web_clear", 6, "Clear cookies, cache or history", "web:clear_data",
       kind="ui", risk=RISK_HIGH, confirm=True,
       targets="cookies|cache|history|all", args="browser"),
    _c("web_private", 6, "Open a private or incognito window", "web:private",
       kind="ui", targets="browser or site"),
    _c("web_save_pdf", 6, "Save a page as a PDF", "web:save_as_pdf",
       kind="network", risk=RISK_LOW, targets="url", args="output path"),
    # The handler explains how to block a site rather than editing a filter
    # list, so it is not a system-state change and must not hard-block.
    _c("web_block", 6, "Block or unblock a website", "web:block_site",
        kind="ui", risk=RISK_LOW, confirm=True, targets="site",
        args="block|unblock"),
]

# --------------------------------------------------------------------------
# 7. email, calendar, messaging
# --------------------------------------------------------------------------
COMMS = [
    _c("mail_list", 7, "List recent email", "comms:mail_list", kind="ui",
       targets="inbox|sent|archive|unread", args="limit",
       needs="Outlook COM"),
    _c("mail_read", 7, "Read an email and summarise it", "comms:mail_read",
       kind="ui", targets="subject or sender", args="summarize|full"),
    _c("mail_search", 7, "Search email", "comms:mail_search", kind="ui",
       targets="query", args="folder"),
    _c("mail_compose", 7, "Draft an email", "comms:mail_compose", kind="ui",
       risk=RISK_MEDIUM, confirm=True, targets="recipient",
       args="subject :: body"),
    _c("mail_send", 7, "Send an email that is already open",
       "comms:mail_send", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="", args="never sends attachments you did not name",
       reverse="mail_draft"),
    _c("mail_move", 7, "Archive, delete, flag or move an email",
       "comms:move", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="subject or id", args="archive|delete|flag|junk|folder:name"),
    _c("mail_attach", 7, "Attach a file to the open draft", "comms:attachments",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="file path"),
    _c("mail_unsubscribe", 7, "Find and use the unsubscribe link in an email",
       "comms:unsubscribe", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="subject or sender"),
    _c("calendar_events", 7, "Show what is in the calendar", "comms:calendar_list",
       kind="ui", targets="today|week|month", args="days"),
    _c("calendar_meeting", 7, "Create, move or cancel a meeting",
       "comms:meeting_invite", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="title", args="create|move|cancel :: time :: attendees"),
    _c("calendar_remind", 7, "Set a reminder", "comms:remind", kind="system",
       risk=RISK_MEDIUM, confirm=True, targets="what", args="when"),
    _c("message_send", 7, "Send a message, or open the app ready to send",
       "comms:message_send", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="app: slack|whatsapp|teams|sms|telegram|discord",
       args="contact and text"),
    _c("notifications", 7, "Read the notification centre", "comms:notifications",
       kind="ui", targets="", args="clear to dismiss them"),
    _c("call_start", 7, "Start a video or voice call", "comms:call",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="app",
       args="contact"),
]

# --------------------------------------------------------------------------
# 8. meetings & work tools
# --------------------------------------------------------------------------
WORK = [
    _c("meeting_control", 8, "Join, leave, mute or share in a meeting",
       "work:meeting_control", kind="ui", risk=RISK_MEDIUM,
       targets="join|leave|mute|unmute|camera|screen|record", args="app"),
    _c("meeting_notes", 8, "Take meeting notes and turn them into actions",
       "work:meeting_notes", kind="llm", risk=RISK_LOW, targets="",
       args="topic and what was said", aliases=("take_notes",)),
    _c("meeting_followup", 8, "Send a follow-up from the notes",
       "work:meeting_followup", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="", args="recipients"),
    _c("work_ticket", 8, "Create or update a ticket or work item",
       "work:work_ticket", kind="ui", risk=RISK_MEDIUM, confirm=True,
       targets="jira|notion|trello|linear", args="action :: title :: body"),
    _c("work_timesheet", 8, "Fill in a timesheet", "work:work_timesheet",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="",
       args="project :: hours :: notes"),
    _c("work_expense", 8, "File an expense report", "work:work_expense",
       kind="ui", risk=RISK_HIGH, confirm=True, targets="", args="csv of rows"),
    _c("work_report", 8, "Export a report from a work tool",
       "work:work_report", kind="ui", risk=RISK_LOW, targets="tool",
       args="what and where"),
    _c("work_crm", 8, "Update a customer record in the CRM", "work:crm_update",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="record",
       args="field=value;field=value"),
]

# --------------------------------------------------------------------------
# 9. system settings
# --------------------------------------------------------------------------
SETTINGS = [
    _c("set_volume", 9, "Set, read or mute the volume", "desktop:volume",
       kind="system", targets="volume 0-100, or mute|unmute|toggle", args="",
       aliases=("volume", "mute")),
    _c("set_brightness", 9, "Set the screen brightness", "desktop:brightness",
       kind="system", targets="0-100", args="", aliases=("brightness",)),
    _c("set_theme", 9, "Switch between dark and light mode", "desktop:theme",
       kind="system", targets="dark|light", args="", aliases=("dark_mode",)),
    _c("set_night_light", 9, "Turn night light on or off", "desktop:night_light",
       kind="system", targets="on|off|schedule"),
    _c("set_do_not_disturb", 9, "Turn Do Not Disturb on or off",
       "desktop:do_not_disturb", kind="system", targets="on|off"),
    _c("set_wifi", 9, "Turn Wi-Fi on or off", "desktop:toggle_wifi",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="on|off"),
    _c("set_bluetooth", 9, "Turn Bluetooth on or off", "desktop:toggle_bluetooth",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="on|off"),
    _c("set_airplane", 9, "Turn airplane mode on or off", "desktop:toggle_airplane",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="on|off"),
    _c("set_wallpaper", 9, "Change the desktop wallpaper", "desktop:wallpaper",
       kind="system", risk=RISK_LOW, targets="image path or colour"),
    _c("set_display", 9, "Change resolution, scaling or refresh rate",
       "desktop:display", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="1920x108x60", args="monitor index"),
    _c("set_power_plan", 9, "Switch the power plan", "desktop:power_plan",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="plan name"),
    _c("set_power", 9, "Sleep, hibernate, lock, restart or shut down",
       "desktop:power", kind="system", risk=RISK_CRITICAL, confirm=True,
       targets="lock|sleep|hibernate|restart|shutdown|logoff"),
    _c("set_printer_default", 9, "Set the default printer",
       "printing:set_default", kind="system", risk=RISK_LOW, targets="printer"),
    _c("set_language", 9, "Change the display language or keyboard layout",
       "desktop:language", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="language or layout"),
    _c("set_time", 9, "Show or set the date, time and time zone",
       "desktop:time", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="", args="set the zone or 24h clock"),
    _c("set_proxy", 9, "Read or change the proxy settings", "desktop:proxy",
       kind="network", risk=RISK_HIGH, confirm=True, targets="host:port",
       args="on|off|clear"),
    _c("set_dns", 9, "Read or change the DNS servers", "netadmin:dns",
       kind="network", risk=RISK_HIGH, confirm=True, targets="servers",
       args="adapter"),
    _c("windows_update", 9, "Check for, install or defer Windows updates",
       "desktop:windows_update", kind="system", risk=RISK_HIGH, confirm=True,
       targets="check|install|defer"),
    _c("set_startup_defaults", 9, "Review what starts with Windows",
       "apps:startup_report", kind="system", targets=""),
]

# --------------------------------------------------------------------------
# 10. hardware & monitoring
# --------------------------------------------------------------------------
HARDWARE = [
    _c("hw_status", 10, "CPU, memory, disk, GPU and network usage at a glance",
       "hardware:status", kind="system", targets="",
       aliases=("system_monitor", "performance")),
    _c("hw_processes", 10, "The processes using the most CPU or memory",
       "hardware:processes", kind="system", targets="10", args="cpu|memory"),
    _c("hw_kill_process", 10, "End a running process", "hardware:kill_process",
       kind="process", risk=RISK_HIGH, confirm=True, targets="name or pid",
       args="force", aliases=("kill_process",)),
    _c("hw_temperature", 10, "Read the CPU and drive temperatures",
       "hardware:temperature", kind="system", targets=""),
    _c("hw_battery", 10, "Battery charge and time remaining", "hardware:battery",
       kind="system", targets="", aliases=("battery",)),
    _c("hw_ip", 10, "Show the local and public IP address", "hardware:ip_address",
       kind="network", targets="", aliases=("ip_address",)),
    _c("hw_devices", 10, "List connected USB, disk and display devices",
       "hardware:devices", kind="system", targets=""),
    _c("hw_eject", 10, "Safely eject a USB drive", "hardware:eject",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="drive letter"),
    _c("hw_free_port", 10, "Find out what holds a port and let it go",
       "hardware:free_port", kind="process", risk=RISK_HIGH, confirm=True,
       targets="port number", args="yes to end the process"),
    _c("hw_disk_check", 10, "Check a drive for errors", "hardware:disk_check",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="C:",
       args="repair"),
    _c("hw_speedtest", 10, "Measure download and upload speed",
       "netadmin:speed_test", kind="network", risk=RISK_LOW, targets="",
       aliases=("speed_test",)),
    _c("hw_storage_report", 10, "What is using disk space, and how much is left",
       "hardware:storage_report", kind="system", targets="",
       aliases=("storage_report", "disk_usage")),
    _c("hw_system_info", 10, "Full system information", "hardware:system_info",
       kind="system", targets="", aliases=("system_info",)),
    _c("hw_drivers", 10, "List, export or update device drivers",
       "hardware:drivers", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="list|update|export", args="driver filter"),
    _c("hw_health", 10, "Windows hardware error report (WHEA) and reliability",
       "hardware:health", kind="system", targets=""),
]

# --------------------------------------------------------------------------
# 11. network
# --------------------------------------------------------------------------
NETWORK = [
    _c("net_wifi_list", 11, "List the Wi-Fi networks in range", "netadmin:wifi_list",
       kind="network", targets="", aliases=("list_wifi", "wifi_networks")),
    _c("net_wifi_connect", 11, "Join a Wi-Fi network", "netadmin:wifi_connect",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="SSID",
       args="profile xml path, or the password is asked for on screen"),
    _c("net_wifi_forget", 11, "Forget a saved Wi-Fi network",
       "netadmin:wifi_forget", kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="SSID"),
    _c("net_hotspot", 11, "Turn the Wi-Fi hotspot on or off", "netadmin:hotspot",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="on|off",
       args="ssid and password"),
    _c("net_ping", 11, "Ping a site or an address", "netadmin:ping",
       kind="network", targets="host", args="count"),
    _c("net_flush_dns", 11, "Flush the DNS cache", "netadmin:flush_dns",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets=""),
    _c("net_devices", 11, "Find the other devices on this network",
       "netadmin:devices", kind="network", targets="", aliases=("arp_scan",)),
    _c("net_block_device", 11, "Block or unblock a device on the network",
       "netadmin:block_device", kind="network", risk=RISK_HIGH, confirm=True,
       targets="ip", args="block|unblock"),
    _c("net_port_check", 11, "Check whether a port is open", "netadmin:port_check",
       kind="network", targets="host", args="port"),
    _c("net_vpn", 11, "Connect to, disconnect from or list VPNs", "netadmin:vpn",
       kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="connect|disconnect|status", args="profile or gateway"),
    _c("net_static_ip", 11, "Use a static IP or hand it back to DHCP",
       "netadmin:static_ip", kind="network", risk=RISK_HIGH, confirm=True,
       targets="adapter", args="dhcp or address/gateway"),
    _c("net_fix", 11, "Fix 'it says no internet'", "netadmin:fix_no_internet",
       kind="network", risk=RISK_HIGH, confirm=True, targets="adapter or all",
       reverse="net_reset"),
    _c("net_reset", 11, "Reset the whole network stack", "netadmin:reset_stack",
       kind="network", risk=RISK_HIGH, confirm=True, targets="all"),
    _c("net_profile", 11, "Show adapters, addresses and the active profile",
       "netadmin:profile", kind="network", targets=""),
]

# --------------------------------------------------------------------------
# 12. troubleshooting
# --------------------------------------------------------------------------
FIX = [
    _c("fix_diagnose", 12, "Find out why the machine is slow or misbehaving",
       "fix:diagnose", kind="system", targets="slow|boot|network|disk|app",
       aliases=("why_slow", "diagnose")),
    _c("fix_speed_up", 12, "The safe clean-up that makes Windows faster",
       "fix:speed_up", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="", args="dry to only list"),
    _c("fix_reset_network", 12, "Reset the network adapter settings",
       "fix:reset_network", kind="network", risk=RISK_HIGH, confirm=True,
       targets="adapter"),
    _c("fix_sfc_dism", 12, "Repair Windows system files", "fix:sfc_dism",
       kind="shell", risk=RISK_HIGH, confirm=True, targets="scan|restore"),
    _c("fix_events", 12, "Read the Windows event log", "fix:events",
       kind="system", targets="system|application|security", args="last N"),
    _c("fix_crash_cause", 12, "Work out what a crash message is telling you",
       "fix:crash_cause", kind="llm", targets="the error text"),
    _c("fix_clear_cache", 12, "Clear a temporary or browser cache",
       "fix:clear_cache", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="what to clear"),
    _c("fix_office", 12, "Repair Microsoft Office", "fix:office_repair",
       kind="shell", risk=RISK_HIGH, confirm=True, targets=""),
    _c("fix_reset_app", 12, "Reset a Windows app to its factory settings",
       "fix:reset_app", kind="shell", risk=RISK_HIGH, confirm=True,
       targets="package name", reverse="fix_repair_app"),
    _c("fix_rollback_driver", 12, "Roll a device driver back to the previous one",
       "fix:rollback_driver", kind="system", risk=RISK_HIGH, confirm=True,
       targets="device name"),
    _c("fix_safe_mode", 12, "Start Windows in safe mode", "fix:safe_mode",
       kind="shell", risk=RISK_CRITICAL, confirm=True, targets="on|off"),
    _c("fix_sound", 12, "Fix 'there is no sound'", "fix:sound",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="device"),
    _c("fix_printer", 12, "Fix a printer that will not print", "fix:printer",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="printer"),
    _c("fix_bluetooth", 12, "Fix Bluetooth pairing or discovery",
       "fix:bluetooth", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="device"),
    _c("fix_black_screen", 12, "Fix a black screen", "fix:black_screen",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets=""),
]

# --------------------------------------------------------------------------
# 13/14/15 media, image, audio
# --------------------------------------------------------------------------
MEDIA = [
    _c("media_control", 13, "Play, pause, skip or stop", "media:control",
       kind="ui", targets="play|pause|next|prev|stop|open_music|open_video",
       args="app or file"),
    _c("media_playlist", 13, "Read, build or shuffle a playlist", "media:playlist",
       kind="fs", risk=RISK_LOW, targets="m3u or folder", args="create|list|shuffle"),
    _c("media_convert", 13, "Convert a video or audio file", "media:convert",
       kind="shell", risk=RISK_LOW, targets="source", args="target format",
       needs="ffmpeg"),
    _c("media_trim_merge", 13, "Trim a clip or join clips together",
       "media:trim_merge", kind="shell", risk=RISK_LOW, targets="source",
       args="start,end or output", needs="ffmpeg"),
    _c("media_extract_audio", 13, "Pull the audio out of a video",
       "media:extract_audio", kind="shell", targets="video", needs="ffmpeg"),
    _c("media_compress", 13, "Make a video or image smaller",
       "media:compress", kind="shell", risk=RISK_LOW, targets="file",
       args="target quality", needs="ffmpeg"),
    _c("media_record", 13, "Record the screen or a microphone", "media:record",
       kind="shell", risk=RISK_MEDIUM, confirm=True,
       targets="screen|audio|camera", args="seconds or stop",
       needs="ffmpeg"),
    _c("image_edit", 14, "Resize, crop, rotate or convert an image",
       "media:image_edit", kind="fs", risk=RISK_LOW, targets="image",
       args="operation and values", needs="Pillow"),
    _c("image_watermark", 14, "Add a watermark to an image", "media:watermark",
       kind="fs", risk=RISK_LOW, targets="image", args="text", needs="Pillow"),
    _c("image_batch", 14, "Resize or convert a whole folder of images",
       "media:image_batch", kind="fs", risk=RISK_LOW, targets="folder",
       args="operation", needs="Pillow"),
    _c("image_creative", 14, "Make a thumbnail, poster, logo, GIF or colour palette",
       "media:creative", kind="fs", risk=RISK_LOW, targets="kind",
       args="input and output", needs="Pillow"),
    _c("image_generate", 14, "Generate an image from a description",
       "media:generate_image", kind="llm", risk=RISK_LOW, targets="description",
       args="output path", needs="Pillow"),
    _c("image_background", 14, "Remove the background from a photo",
       "media:remove_background", kind="fs", targets="image", needs="rembg"),
    _c("image_enhance", 14, "Sharpen, brighten or clean up a photo",
       "media:enhance", kind="fs", risk=RISK_LOW, targets="image",
       args="sharpen|brighten|denoise|upscale", needs="Pillow"),
    _c("image_palette", 14, "Pick the colours out of an image",
       "media:palette", kind="fs", targets="image", args="count", needs="Pillow"),
    _c("audio_record", 15, "Record a voice memo", "media:record_voice",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="output path",
       args="seconds", needs="sounddevice|pyaudio"),
    _c("audio_trim_speed", 15, "Trim a recording or change its speed",
       "media:audio_trim_speed", kind="shell", targets="file", args="operation",
       needs="ffmpeg"),
    _c("audio_denoise", 15, "Remove hiss and background noise",
       "media:denoise", kind="shell", targets="file", needs="ffmpeg"),
    _c("audio_device", 15, "Change the default microphone or speaker",
       "media:audio_device", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="list|default", args="device name"),
    _c("audio_test_mic", 15, "Check that the microphone works", "media:test_mic",
       kind="shell", risk=RISK_LOW, targets="", args="seconds"),
    _c("speak", 15, "Read text out loud", "media:speak", kind="ui",
       targets="text or file", args="", aliases=("tts", "read_aloud")),
    _c("transcribe", 15, "Turn speech or a recording into text",
       "media:transcribe", kind="shell", targets="audio file",
       args="language", needs="faster-whisper|SpeechRecognition"),
    _c("subtitles", 15, "Make subtitles for a video", "media:subtitles",
       kind="shell", targets="video", args="srt or burn",
       needs="faster-whisper|ffmpeg"),
    _c("voice_commands", 15, "Turn voice commands on or off",
       "media:voice_commands", kind="ui", targets="on|off|dictation|wake_word",
       args="wake word"),
]

# --------------------------------------------------------------------------
# 16 developer
# --------------------------------------------------------------------------
DEV = [
    _c("dev_open_project", 16, "Open a project folder in the editor",
       "devtools:open_project", kind="system", targets="path", args="editor"),
    _c("dev_run", 16, "Run a command in a project folder", "devtools:run",
       kind="shell", targets="path", args="command"),
    _c("dev_build", 16, "Build a project", "devtools:build", kind="shell",
       targets="path", args="build command"),
    _c("dev_test", 16, "Run the tests", "devtools:test", kind="shell",
       targets="path", args="test command or filter"),
    _c("dev_deps", 16, "Install or list project dependencies", "devtools:deps",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="path",
       args="install|list|audit|add package"),
    _c("dev_venv", 16, "Create or activate a virtual environment", "devtools:venv",
       kind="shell", risk=RISK_LOW, targets="path", args="create|activate"),
    _c("git", 16, "Run a git command", "devtools:git_status", kind="shell",
       targets="path or remote", args="subcommand",
       aliases=("git_commit", "git_push", "git_pull", "git_branch", "git_merge")),
    _c("dev_server", 16, "Start or stop a development server", "devtools:server",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="path",
       args="start|stop|status :: command"),
    _c("docker", 16, "Drive Docker", "devtools:containers", kind="shell",
       targets="", args="ps|images|run|stop|logs|compose",
       needs="docker"),
    _c("wsl", 16, "Drive the Windows Subsystem for Linux", "devtools:wsl",
       kind="shell", risk=RISK_MEDIUM, targets="distro", args="command",
       needs="wsl"),
    _c("ssh", 16, "Run an SSH command on another machine", "devtools:ssh",
       kind="network", risk=RISK_HIGH, confirm=True, targets="host",
       args="command", aliases=("ssh_run",)),
    _c("deploy", 16, "Deploy a project", "devtools:deploy", kind="network",
       risk=RISK_HIGH, confirm=True, targets="path", args="target and command"),
    _c("code_explain", 16, "Explain what a file or function does", "devtools:explain",
       kind="llm", targets="file or code", args="question"),
    _c("code_refactor", 16, "Refactor code", "devtools:refactor", kind="llm",
       risk=RISK_MEDIUM, confirm=True, targets="file", args="what to change"),
    _c("code_tests", 16, "Write tests for existing code", "devtools:generate_tests",
       kind="llm", risk=RISK_MEDIUM, confirm=True, targets="file"),
    _c("code_debug", 16, "Work out why the code is failing",
       "devtools:debug_error", kind="llm", targets="error text or path",
       args="file"),
    _c("code_script", 16, "Write a small script to do a job", "devtools:write_script",
       kind="llm", targets="what it should do", args="where to save it"),
]

# --------------------------------------------------------------------------
# 17 data & analytics
# --------------------------------------------------------------------------
DATA = [
    _c("data_clean", 17, "Clean a CSV: blanks, duplicates, bad rows",
       "datatools:clean_csv", kind="fs", risk=RISK_LOW, targets="csv",
       args="output", aliases=("clean_csv", "remove_blanks")),
    _c("data_pivot", 17, "Build a pivot table from a CSV", "datatools:pivot",
       kind="fs", risk=RISK_LOW, targets="csv", args="index,columns,values"),
    _c("data_plot", 17, "Plot the data as a chart", "datatools:plot",
       kind="fs", risk=RISK_LOW, targets="csv", args="x,y,kind",
       needs="matplotlib", aliases=("plot",)),
    _c("data_compare", 17, "Compare two files and show what changed",
       "datatools:compare", kind="fs", risk=RISK_LOW, targets="file a",
       args="file b"),
    _c("data_outliers", 17, "Find the odd values in a column",
       "datatools:outliers", kind="fs", risk=RISK_LOW, targets="csv",
       args="column"),
    _c("data_merge", 17, "Join two datasets on a key", "datatools:merge",
       kind="fs", risk=RISK_LOW, targets="left.csv", args="right.csv,key"),
    _c("data_query", 17, "Run a SQL query against a database", "datatools:describe",
       kind="fs", risk=RISK_LOW, targets="sqlite file", args="select statement",
       needs="sqlite3"),
    _c("data_export", 17, "Export data to Excel, JSON or CSV", "datatools:to_json",
       kind="fs", risk=RISK_LOW, targets="source", args="target format",
       needs="openpyxl"),
    _c("data_dashboard", 17, "Build a small HTML dashboard from data",
       "datatools:plot", kind="fs", risk=RISK_LOW, targets="csv",
       args="columns", aliases=("build_dashboard",)),
    _c("data_report", 17, "Run a recurring data report", "datatools:describe",
       kind="fs", risk=RISK_LOW, targets="csv", args="what to report"),
    _c("data_forecast", 17, "Forecast a trend from historical values",
       "datatools:describe", kind="fs", risk=RISK_LOW, targets="csv",
       args="column,steps"),
]

# --------------------------------------------------------------------------
# 18 search & knowledge
# --------------------------------------------------------------------------
KNOWLEDGE = [
    _c("search_everything", 18, "Search files, email, history and notes at once",
       "knowledge:search_everything", kind="fs", risk=RISK_LOW, targets="query",
       args="where: files|email|history|notes|all"),
    _c("search_files", 18, "Full-text search inside files", "knowledge:search_files",
       kind="fs", targets="query", args="folder"),
    _c("search_documents", 18, 'Find "that PDF about taxes"', "knowledge:find_document",
       kind="fs", targets="description", args="folders"),
    _c("search_images", 18, "Find images by name, size or colour",
       "knowledge:search_images", kind="fs", targets="query", args="folder",
       needs="Pillow"),
    _c("knowledge_index", 18, "Index a folder so it can be searched by meaning",
       "knowledge:index_folder", kind="fs", risk=RISK_LOW, confirm=True,
       targets="folder", args="index name"),
    _c("knowledge_search", 18, "Search an index by meaning, not keywords",
       "knowledge:semantic_search", kind="fs", targets="index or folder",
       args="question"),
    _c("knowledge_chat", 18, "Ask questions of a folder of documents",
       "knowledge:search_documents", kind="llm", risk=RISK_LOW,
       targets="folder or index", args="question"),
    _c("knowledge_recall", 18, "What did I do yesterday?",
       "knowledge:recall_recent", kind="fs", risk=RISK_LOW,
       targets="yesterday|today|last_week|YYYY-MM-DD", aliases=("recall",)),
    _c("travel_record", 32, "Keep the details of a trip in a local record",
       "life:record_trip", kind="fs", risk=RISK_LOW, targets="the trip",
       args="date, flight, seats", aliases=("remember_trip", "add_trip")),
    _c("travel_forget", 32, "Take a trip or parcel record back",
       "life:forget_record", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="the record", args="", aliases=("forget_trip", "remove_trip")),
    _c("know_answer", 21, "Answer a question, using the model", "info:answer", kind="llm", risk=RISK_LOW, targets="question",
       args="context"),
    _c("know_explain", 21, "Explain something simply", "info:explain",
       kind="llm", risk=RISK_LOW, targets="what to explain",
       args="audience or depth", aliases=("explain", "define")),
    _c("know_calculate", 21, "Work out an answer, exactly where possible", "info:calculate",
       kind="llm", risk=RISK_LOW, targets="the expression",
       args="", aliases=("calculate", "convert_units")),
    _c("know_translate", 21, "Translate text between languages", "info:translate",
       kind="llm", risk=RISK_LOW, targets="text or file",
       args="target language", aliases=("translate",)),
    _c("know_weather", 21, "Current weather and forecast", "info:weather",
       kind="network", risk=RISK_LOW, targets="city or postcode"),
    _c("know_news", 21, "Top headlines", "info:news", kind="network",
       risk=RISK_LOW, targets="topic"),
    _c("know_directions", 21, "Directions and distance", "info:directions",
       kind="network", risk=RISK_LOW, targets="from", args="to"),
    _c("know_summarize_url", 21, "Summarise a page or a video", "info:summarize_url", kind="network", risk=RISK_LOW, targets="url",
       args="", aliases=("summarize_page",)),
    _c("know_currency", 21, "Convert an amount between currencies", "info:currency", kind="network", risk=RISK_LOW, targets="amount",
       args="from to"),
    _c("know_define", 21, "Define a word or term", "info:define",
       kind="llm", risk=RISK_LOW, targets="the word or term", args="",
       aliases=("define", "definition", "meaning_of"),
       triggers=("what does this mean", "define this", "meaning of",
                 "iska matlab kya hai", "इसका मतलब क्या है")),
    _c("know_read_aloud", 21, "Read a file or some text out loud",
       "info:read_aloud", kind="system", risk=RISK_LOW,
       targets="file or text", args="",
       aliases=("read_aloud", "tts", "speak_text"),
       triggers=("read this aloud", "read this out", "say this out loud",
                 "read pdf aloud", "bol ke sunao", "यह बोलकर सुनाओ")),
]

# --------------------------------------------------------------------------
# 19 notes, tasks, productivity
# --------------------------------------------------------------------------
TASKS = [
    _c("note_write", 19, "Write or append a note", "productivity:note",
       kind="fs", risk=RISK_LOW, targets="note title", args="body",
       aliases=("create_note", "edit_note")),
    _c("note_list", 19, "List, read, search or delete notes",
       "productivity:note_list", kind="fs", targets="list|read|search|delete",
       args="title or query"),
    _c("todo_add", 19, "Add a to-do", "productivity:todo_add", kind="fs",
       risk=RISK_LOW, targets="what", args="priority or due date",
       aliases=("add_todo",)),
    _c("todo_list", 19, "Show the to-do list", "productivity:todo_list",
       kind="fs", targets="", args="all|open|done|today", aliases=("list_todos",)),
    _c("todo_complete", 19, "Tick a to-do off or delete it",
       "productivity:todo_complete", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="id or text", args="done|delete"),
    _c("timer", 19, "Timer, alarm, stopwatch or pomodoro", "productivity:timer",
       kind="system", risk=RISK_LOW, targets="countdown|stopwatch|alarm|pomodoro",
       args="duration", aliases=("pomodoro", "stopwatch", "alarm")),
    _c("plan_day", 19, "Build today's plan", "plan:day", kind="llm", risk=RISK_LOW,
       targets="", args="what you have to get done", aliases=("daily_plan",)),
    _c("plan_week", 19, "Weekly review", "plan:week", kind="llm", risk=RISK_LOW,
       targets="", args="", aliases=("weekly_review",)),
    _c("habits", 19, "Track a habit", "productivity:habits", kind="fs",
       risk=RISK_LOW, targets="log|show|streak", args="habit name",
       aliases=("habit_tracker",)),
    _c("journal", 19, "Write a journal entry", "productivity:journal",
       kind="fs", risk=RISK_LOW, targets="", args="entry"),
    _c("sticky_note", 19, "Put a note on the desktop", "productivity:sticky_note",
       kind="fs", risk=RISK_LOW, targets="text", args=""),
    _c("time_tracking", 19, "Track time on a task", "productivity:time_tracking",
       kind="fs", risk=RISK_LOW, targets="start|stop|report", args="task"),
    _c("project_board", 19, "Update a project board", "productivity:project_board",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="board",
       args="add|move|show :: item"),
    _c("focus_session", 19, "Start a focus session", "productivity:focus_session",
       kind="system", risk=RISK_LOW, targets="minutes", args="task"),
]

# --------------------------------------------------------------------------
# 20 learning
# --------------------------------------------------------------------------
LEARN = [
    _c("learn_flashcards", 20, "Make flashcards from notes or a PDF",
       "learn:flashcards", kind="llm", risk=RISK_LOW, targets="file or text",
       args="how many", aliases=("flashcards", "quiz")),
    _c("learn_summarize", 20, "Summarise a chapter or a document",
       "learn:summarize", kind="llm", risk=RISK_LOW, targets="file",
       args="length"),
    _c("learn_explain_simple", 20, "Explain a topic as if to a beginner",
       "learn:explain_simple", kind="llm", risk=RISK_LOW, targets="topic",
       args="level"),
    _c("learn_schedule", 20, "Build a study schedule", "learn:schedule",
       kind="llm", risk=RISK_LOW, targets="subject and exam date", args="hours"),
    _c("learn_read_aloud", 20, "Read a document aloud", "learn:read_aloud",
       kind="ui", risk=RISK_LOW, targets="file", args="start|stop"),
    _c("learn_lecture_notes", 20, "Take notes from a lecture recording",
       "learn:lecture_notes", kind="llm", risk=RISK_LOW, targets="audio file"),
    _c("learn_translate_lecture", 20, "Translate lecture material",
       "learn:translate_lecture", kind="llm", risk=RISK_LOW, targets="file",
       args="language"),
    _c("learn_practice", 20, "Practise a language with drills", "learn:practice",
       kind="llm", risk=RISK_LOW, targets="language", args="level or topic"),
    _c("learn_maths", 20, "Solve a maths problem step by step", "learn:maths",
       kind="llm", risk=RISK_LOW, targets="the problem", aliases=("solve_math",)),
]

# --------------------------------------------------------------------------
# 22 printing & scanning
# --------------------------------------------------------------------------
PRINT = [
    _c("print_list", 22, "List the printers and their state", "printing:list_all",
       kind="system", targets="", aliases=("printer_status",)),
    _c("print_file", 22, "Print a file", "printing:print_file", kind="shell",
       risk=RISK_MEDIUM, confirm=True, targets="file",
       args="copies:1 pages:1-2 duplex color b/w"),
    _c("print_to_pdf", 22, "Print something to a PDF instead of paper",
       "printing:print_to_pdf", kind="shell", risk=RISK_MEDIUM, confirm=True,
       targets="file", args="output path"),
    _c("print_cancel", 22, "Cancel what is currently printing",
       "printing:cancel", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="job id or all"),
    _c("print_add", 22, "Add or remove a printer", "printing:manage",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="printer name",
       args="add|remove|port"),
    _c("print_ink", 22, "Ink or toner levels", "printing:manage", kind="system",
       targets="printer"),
    _c("scan_document", 22, "Scan a document to an image or PDF",
       "printing:scan", kind="shell", risk=RISK_MEDIUM, confirm=True,
       targets="output path", args="dpi or flatbed|feeder", needs="WIA"),
]

# --------------------------------------------------------------------------
# 23 cloud
# --------------------------------------------------------------------------
CLOUD = [
    _c("cloud_status", 23, "Is the cloud sync healthy?", "cloud:status",
       kind="system", targets="", aliases=("sync_status",)),
    _c("cloud_sync", 23, "Start or stop syncing a folder", "cloud:sync",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="remote or local",
       args="sync|pull|push|stop"),
    _c("cloud_upload", 23, "Upload a file or folder", "cloud:upload",
       kind="network", risk=RISK_HIGH, confirm=True, targets="path",
       args="destination"),
    _c("cloud_download", 23, "Download a file from the cloud",
       "cloud:download", kind="network", risk=RISK_MEDIUM, targets="path",
       args="destination"),
    _c("cloud_share", 23, "Create a share link for a file", "cloud:share_link",
       kind="network", risk=RISK_HIGH, confirm=True, targets="path",
       args="who it is for"),
    _c("cloud_backup", 23, "Back a folder up to the cloud", "cloud:cloud_backup",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="folder",
       args="destination"),
    _c("cloud_free_space", 23, "Free up disk space safely", "cloud:mounted",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="",
       args="dry to only report"),
    _c("cloud_restore", 23, "Restore an earlier version of a file",
       "cloud:download", kind="shell", risk=RISK_HIGH, confirm=True,
       targets="path", args="version"),
]

# --------------------------------------------------------------------------
# 24 phone & device link
# --------------------------------------------------------------------------
PHONE = [
    _c("phone_send_file", 24, "Send a file to a phone on this network",
       "phone:send_file", kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="file", args=""),
    _c("phone_notifications", 24, "Read the phone's notifications",
       "phone:notifications", kind="ui", risk=RISK_LOW, targets=""),
    _c("phone_reply", 24, "Reply to a phone message", "phone:reply",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="contact", args="text"),
    _c("phone_cast", 24, "Cast or mirror the screen to a TV",
       "phone:cast", kind="ui", risk=RISK_LOW, targets="device or discover"),
    _c("phone_clipboard", 24, "Send the clipboard to the phone",
       "phone:clipboard", kind="ui", risk=RISK_LOW, targets="push|pull"),
    _c("phone_find", 24, "Find a lost phone", "phone:find", kind="ui",
       risk=RISK_MEDIUM, confirm=True, targets="", args="call it or locate it"),
    _c("phone_link_open", 24, "Open Phone Link", "phone:open_app", kind="system",
       targets="", args=""),
]

# --------------------------------------------------------------------------
# 25 smart home
# --------------------------------------------------------------------------
IOT = [
    _c("iot_discover", 25, "Find smart-home devices on this network",
       "life:iot_discover", kind="network", risk=RISK_LOW, targets="", args="",
       aliases=("find_devices", "scan_network", "what_devices")),
    _c("iot_control", 25, "Control a light, plug, lock, camera or thermostat",
       "life:iot_control", kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="device", args="on|off|toggle"),
    _c("iot_scene", 25, "Run a scene or routine", "life:iot_scene",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="scene",
       args="", aliases=("scene", "smart_routine")),
    _c("iot_presence", 25, "Which smart devices are awake right now",
       "life:iot_discover", kind="network", risk=RISK_LOW, targets="", args="",
       aliases=("which_devices_online", "device_presence")),
]

# --------------------------------------------------------------------------
# 26 accessibility
# --------------------------------------------------------------------------
ACCESS = [
    _c("access_screen_reader", 26, "Turn Narrator or the magnifier on or off",
       "access:screen_reader", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="narrator|magnifier", args="on|off",
       aliases=("narrator", "screen_reader", "magnifier")),
    _c("access_high_contrast", 26, "High contrast mode on or off",
       "access:high_contrast", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="on|off", args="",
       aliases=("contrast", "high_contrast_mode")),
    _c("access_text_size", 26, "Scale the text larger, or put it back",
       "access:text_size", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="percent, or reset", args="",
       aliases=("bigger_text", "larger_text", "font_size", "text_scale")),
    _c("access_sticky_keys", 26, "Sticky keys on or off",
       "access:sticky_keys", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="on|off", args="", aliases=("stickykeys",)),
    _c("access_color_filters", 26, "Colour filters for colour blindness",
       "access:color_filters", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="on|off", args="", aliases=("colour_filters", "colorblind_mode")),
    _c("access_captions", 26, "Live captions for audio",
       "access:captions", kind="system", risk=RISK_LOW, targets="", args="",
       aliases=("live_captions", "subtitles")),
    _c("access_voice_typing", 26, "Dictation on or off",
       "access:voice_typing", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="on|off", args="", aliases=("dictation", "voice_input")),
    _c("access_status", 26, "What every accessibility setting is set to",
       "access:status", kind="fs", risk=RISK_LOW, targets="", args="",
       aliases=("accessibility_status", "check_accessibility")),
    _c("access_read_screen", 26, "Read the screen aloud and stop",
       "access:read_screen", kind="ui", risk=RISK_LOW, targets="start|stop"),
    _c("access_voice_only", 26, "Control the machine by voice only",
       "access:voice_only", kind="ui", risk=RISK_MEDIUM, confirm=True,
       targets="on|off", args="wake word"),
]

# --------------------------------------------------------------------------
# 27 remote & virtual
# --------------------------------------------------------------------------
REMOTE = [
    _c("remote_desktop", 27, "Open Remote Desktop to another PC", "life:remote_desktop",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="host or .rdp file",
       args="user"),
    _c("remote_ssh", 27, "SSH into another machine", "life:remote_ssh",
       kind="network", risk=RISK_HIGH, confirm=True, targets="host",
       args="command", aliases=("ssh",)),
    _c("remote_vm", 27, "Start, stop or list virtual machines", "life:vm",
       kind="shell", risk=RISK_HIGH, confirm=True, targets="name or list",
       args="start|stop|snapshot|status"),
    _c("remote_transfer", 27, "Copy a file to or from another machine",
       "life:transfer", kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="host", args="file to / destination"),
    _c("remote_share_screen", 27, "Share this screen with someone",
       "life:share_screen", kind="ui", risk=RISK_MEDIUM, confirm=True,
       targets="app", args="contact"),
]

# --------------------------------------------------------------------------
# 28 backup & recovery
# --------------------------------------------------------------------------
BACKUP = [
    _c("backup_restore_point", 28, "Create a system restore point",
       "backup:restore_point", kind="shell", risk=RISK_MEDIUM, confirm=True,
       targets="name"),
    _c("backup_create", 28, "Back a folder up", "backup:create",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="source",
       args="destination"),
    _c("backup_schedule", 28, "Schedule a recurring backup", "backup:schedule",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="source",
       args="cron or every 2 hours"),
    _c("backup_verify", 28, "Check that a backup is actually restorable",
       "backup:verify", kind="fs", risk=RISK_LOW, targets="backup path"),
    _c("backup_restore", 28, "Restore a file or folder from a backup",
       "backup:restore", kind="fs", risk=RISK_HIGH, confirm=True,
       targets="backup path", args="destination"),
    _c("backup_recover_file", 28, "Recover a deleted file", "backup:recover_file",
       kind="fs", risk=RISK_LOW, targets="file name", args="where to look"),
    _c("backup_history", 28, "Turn File History on and show previous versions",
       "backup:file_history", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="path or status"),
    _c("backup_encrypt", 28, "Turn BitLocker on or off", "backup:bitlocker",
       kind="shell", risk=RISK_CRITICAL, confirm=True, targets="status|on|off",
       args="drive"),
]

# --------------------------------------------------------------------------
# 29 security & privacy
# --------------------------------------------------------------------------
SECURITY = [
    # Locking the workstation is the safest action in the catalog. kind="system"
    # would score any of them critical, so this is a UI action on the desktop.
    _c("sec_lock", 29, "Lock the screen or sign out", "security:lock",
       kind="ui", risk=RISK_LOW, targets="lock|signout",
       aliases=("lock", "sign_out")),
    _c("sec_scan", 29, "Run an antivirus scan", "security:scan",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="quick|full|folder",
       args="path"),
    _c("sec_firewall", 29, "Turn the firewall on or off, or show its state",
       "security:firewall", kind="network", risk=RISK_HIGH, confirm=True,
       targets="on|off|status|rule"),
    _c("sec_audit_passwords", 29, "Check for breached passwords",
       "security:audit_passwords", kind="network", risk=RISK_MEDIUM,
       confirm=True, targets="", args="never sends the password itself, only a hash prefix"),
    _c("sec_password_vault", 29, "List the stored credentials by name",
       "security:password_vault", kind="ui", risk=RISK_LOW, targets="list|add|remove",
       args="name", aliases=("password_manager",)),
    _c("sec_secure_delete", 29, "Overwrite a file so it cannot be recovered",
       "security:secure_delete", kind="fs", risk=RISK_CRITICAL, confirm=True,
       targets="path", args="passes"),
    _c("sec_disable_device", 29, "Turn the webcam or microphone off",
       "security:disable_device", kind="system", risk=RISK_HIGH, confirm=True,
       targets="camera|microphone", args="on|off"),
    _c("sec_permissions", 29, "Review what each app may access",
       "security:app_permissions", kind="system", risk=RISK_MEDIUM,
       targets="camera|microphone|location|notifications|list", args="app"),
    _c("sec_login_attempts", 29, "Show recent sign-in attempts",
       "security:login_attempts", kind="system", targets="", args="count"),
    _c("sec_remote_access", 29, "Turn remote access on or off",
       "security:remote_access", kind="system", risk=RISK_HIGH, confirm=True,
       targets="rdp|firewall|ssh|status", args="on|off"),
    _c("sec_encrypt_file", 29, "Encrypt one file with the Windows key",
       "fileops:encrypt", kind="fs", risk=RISK_MEDIUM, targets="path",
       args="on"),
    _c("sec_hidden_folder", 29, "Make a folder harder to find", "fileops:visibility",
       kind="fs", risk=RISK_MEDIUM, targets="path", args="hide"),
]

# --------------------------------------------------------------------------
# 30 users & profiles
# --------------------------------------------------------------------------
USERS = [
    _c("user_list", 30, "List the accounts on this computer", "users:list_users",
       kind="system", targets="", aliases=("profiles",)),
    _c("user_switch", 30, "Switch to another account", "users:switch_user",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="account"),
    _c("user_add", 30, "Add a local account", "users:add_user", kind="system",
       risk=RISK_HIGH, confirm=True, targets="account", args="the user types their own password"),
    _c("user_remove", 30, "Remove a local account", "users:remove_user",
       kind="system", risk=RISK_HIGH, confirm=True, targets="account",
       reverse="user_add"),
    _c("user_toggle", 30, "Enable or disable an account", "users:toggle_user",
       kind="system", risk=RISK_HIGH, confirm=True, targets="account",
       args="on|off"),
    _c("user_guest", 30, "Turn guest access on or off", "users:guest_session",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="on|off"),
    _c("user_parental", 30, "Set the family safety options", "users:parental",
       kind="system", risk=RISK_HIGH, confirm=True, targets="on|off|report",
       args="account"),
    _c("user_screen_time", 30, "Set a screen-time limit for an account",
       "users:screen_time", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="account", args="minutes or off"),
    _c("user_block_app", 30, "Block or allow an app for an account",
       "users:block_app", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="account", args="app.exe block|allow"),
    _c("user_kiosk", 30, "Put a device into kiosk mode", "users:kiosk",
       kind="system", risk=RISK_HIGH, confirm=True, targets="app or url",
       args="on|off"),
    _c("user_settings", 30, "Read or set per-user Windows settings",
       "users:settings", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="account", args="setting=value"),
]

# --------------------------------------------------------------------------
# 31 finance
# --------------------------------------------------------------------------
FINANCE = [
    _c("fin_balance", 31, "Check a balance from a statement you supplied",
       "life:balance", kind="fs", risk=RISK_LOW, confirm=True,
       targets="account or statement file", args="the file to read"),
    _c("fin_statement", 31, "Download or open a statement", "life:statement",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="site or file",
       args="period"),
    _c("fin_pay", 31, "Open a bill ready to pay, and stop before paying",
       "life:pay_bill", kind="ui", risk=RISK_CRITICAL, confirm=True,
       targets="bill or payee", args="amount"),
    _c("fin_expenses", 31, "Track what you have spent", "life:track_expenses",
       kind="fs", risk=RISK_LOW, targets="transaction file or entry",
       args="amount category note"),
    _c("fin_categorise", 31, "Sort transactions into categories",
       "life:categorise_transactions", kind="llm", risk=RISK_LOW,
       targets="csv", args="categories"),
    _c("fin_invoice", 31, "Create an invoice", "life:invoice", kind="llm",
       risk=RISK_LOW, targets="customer", args="line items and due date"),
    _c("fin_reminder", 31, "Remind you to pay something", "life:payment_reminder",
       kind="system", risk=RISK_LOW, targets="what", args="when"),
]

# --------------------------------------------------------------------------
# 32 travel & life admin
# --------------------------------------------------------------------------
TRAVEL = [
    _c("travel_book", 32, "Search and open a flight, hotel, cab or train booking",
       "life:travel_book", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="what kind", args="from, to, dates"),
    _c("travel_status", 32, "Check a flight or train status", "life:flight_status",
       kind="network", risk=RISK_LOW, targets="flight number or PNR"),
    _c("travel_boarding_pass", 32, "Open the boarding pass", "life:boarding_pass",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="PNR or file"),
    _c("travel_itinerary", 32, "Build the itinerary for a trip",
       "life:itinerary", kind="llm", risk=RISK_LOW, targets="trip",
       args="dates and bookings"),
    _c("travel_parcel", 32, "Track a parcel", "life:track_parcel",
       kind="network", risk=RISK_LOW, targets="tracking number"),
    _c("travel_subscription", 32, "Renew or cancel a subscription",
       "life:subscription", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="service", args="renew|cancel"),
    _c("travel_insurance", 32, "Compare insurance options", "life:compare_insurance",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="what and cover",
       args=""),
    _c("travel_form", 32, "Fill a government or official form for you",
       "life:government_form", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="form name or url", args="answers; personal details come from you"),
]

# --------------------------------------------------------------------------
# 33 shopping
# --------------------------------------------------------------------------
SHOPPING = [
    _c("shop_compare", 33, "Compare products or prices", "life:compare_products",
       kind="network", risk=RISK_MEDIUM, confirm=True, targets="product",
       args="sites", aliases=("compare_prices",)),
    _c("shop_price_track", 33, "Watch a price and tell you when it drops",
       "life:track_price", kind="network", risk=RISK_MEDIUM, confirm=True,
       targets="product url", args="price you will pay"),
    _c("shop_wishlist", 33, "Keep a wishlist", "life:wishlist", kind="fs",
       risk=RISK_LOW, targets="list|add|remove|check", args="item and url"),
    _c("shop_coupon", 33, "Find and show discount codes", "life:coupon",
       kind="network", risk=RISK_MEDIUM, targets="product or site",
       args="apply it at checkout yourself"),
    _c("shop_reorder", 33, "Reorder a previous purchase", "life:reorder",
       kind="ui", risk=RISK_HIGH, confirm=True, targets="shop", args="item"),
    _c("shop_reviews", 33, "Summarise what reviewers say", "life:reviews",
       kind="llm", risk=RISK_LOW, targets="product url or review file"),
    _c("shop_delivery", 33, "Where is my order?", "life:delivery",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="shop", args="order id"),
    _c("shop_return", 33, "Start a return or refund", "life:return_request",
       kind="ui", risk=RISK_HIGH, confirm=True, targets="shop", args="order and reason"),
]

# --------------------------------------------------------------------------
# 34 social
# --------------------------------------------------------------------------
SOCIAL = [
    _c("social_post", 34, "Draft a post for you to check", "life:draft_post",
       kind="llm", risk=RISK_LOW, targets="platform", args="what it is about"),
    _c("social_schedule", 34, "Queue a post for later", "life:schedule_post",
       kind="fs", risk=RISK_MEDIUM, confirm=True, targets="platform",
       args="text and time"),
    _c("social_reply", 34, "Draft replies to comments", "life:reply_comments",
       kind="llm", risk=RISK_LOW, targets="platform", args="comments"),
    _c("social_captions", 34, "Write captions and hashtags", "life:captions",
       kind="llm", risk=RISK_LOW, targets="platform", args="what it is about"),
    _c("social_resize", 34, "Resize an image for each platform",
       "media:image_batch", kind="fs", risk=RISK_LOW, targets="image",
       args="instagram,tiktok,linkedin"),
    _c("social_crosspost", 34, "Post the same thing to several platforms",
       "life:crosspost", kind="ui", risk=RISK_HIGH, confirm=True,
       targets="platforms", args="text"),
    _c("social_export", 34, "Download a copy of your data", "life:export_my_data",
       kind="ui", risk=RISK_MEDIUM, confirm=True, targets="platform"),
    _c("social_engagement", 34, "Analyse how a post performed",
       "life:engagement", kind="fs", risk=RISK_LOW, targets="metrics file"),
]

# --------------------------------------------------------------------------
# 35 games
# --------------------------------------------------------------------------
GAMES = [
    _c("game_launch", 35, "Start or close a game", "life:game", kind="system",
       targets="game or launcher", args="launch|close|list"),
    _c("game_graphics", 35, "Change the graphics settings", "life:game_graphics",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="preset",
       args="game or gpu"),
    _c("game_stream", 35, "Start or stop a livestream", "life:stream",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="start|stop",
       args="title", needs="obs"),
    _c("game_record", 35, "Record gameplay", "life:record_gameplay",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="output",
       args="start|stop", needs="ffmpeg|obs"),
    _c("game_library", 35, "Install, update or list games", "life:game_library",
       kind="shell", risk=RISK_MEDIUM, confirm=True, targets="steam|epic",
       args="install|update|list :: game"),
]

# --------------------------------------------------------------------------
# 36 advanced system
# --------------------------------------------------------------------------
ADVANCED = [
    _c("adv_registry", 36, "Read, set or delete a registry value",
       "desktop:registry", kind="system", risk=RISK_HIGH, confirm=True,
       targets="hive\\key\\value", args="value and type"),
    _c("adv_env", 36, "Read or set an environment variable", "desktop:env",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="name",
       args="value or clear", aliases=("env_var",)),
    _c("adv_hosts", 36, "Edit the hosts file", "advanced:hosts",
       kind="system", risk=RISK_HIGH, confirm=True, targets="action",
       args="line to add or remove"),
    _c("adv_services", 36, "List, start, stop or configure a service",
       "system:list_services", kind="system", risk=RISK_HIGH, confirm=True,
       targets="service", args="start|stop|restart|status"),
    _c("adv_drivers", 36, "List, export or install drivers", "advanced:drivers",
       kind="system", risk=RISK_HIGH, confirm=True, targets="action",
       args="filter or inf path"),
    _c("adv_task_scheduler", 36, "Look at the Windows Task Scheduler",
       "advanced:task_scheduler", kind="system", risk=RISK_MEDIUM,
       targets="list|create|delete|run", args="task"),
    _c("adv_event_viewer", 36, "Open the Event Viewer or read the log",
       "fix:events", kind="system", targets="log", args="count"),
    _c("adv_group_policy", 36, "Report the group policy applied to this machine",
       "advanced:group_policy", kind="system", targets=""),
    _c("adv_bios", 36, "BIOS and firmware information", "advanced:bios",
       kind="system", targets=""),
    _c("adv_boot_order", 36, "Show or change the boot order", "advanced:boot_order",
       kind="shell", risk=RISK_CRITICAL, confirm=True, targets="list|set",
       args="entry"),
    _c("adv_partition", 36, "List disks and partitions", "advanced:partitions",
       kind="system", risk=RISK_LOW, targets="list"),
    _c("adv_format", 36, "Format a drive", "advanced:format_drive",
       kind="shell", risk=RISK_CRITICAL, confirm=True, targets="drive",
       args="filesystem", reverse=""),
    _c("adv_bootable_usb", 36, "Write a bootable USB", "advanced:bootable_usb",
       kind="shell", risk=RISK_CRITICAL, confirm=True, targets="usb drive",
       args="iso path"),
    _c("adv_gp_edit", 36, "Look up a group policy setting", "advanced:gpolist",
       kind="system", targets="name"),
]

# --------------------------------------------------------------------------
# 37-41 automation, alerts, context, routines, compound
# --------------------------------------------------------------------------
AUTOMATION = [
    _c("auto_schedule", 37, "Run something later, on a repeat or on a cron",
       "auto:schedule", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="when", args="what to do", reverse="auto_cancel"),
    _c("auto_cancel", 37, "Cancel, pause or resume a scheduled task",
       "auto:control", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="cancel|pause|resume|list", args="task id"),
    _c("auto_startup", 37, "Run something when Windows starts",
       "auto:on_startup", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="what", args="command", reverse="auto_remove_startup"),
    _c("auto_remove_startup", 37, "Remove something from Windows startup",
       "auto:remove_startup", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="name"),
    _c("auto_watch_folder", 37, "Watch a folder and act when a file appears",
       "auto:watch_folder", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="folder", args="what to do", aliases=("watch_folder",)),
    _c("auto_trigger", 37, "Act when an event happens", "auto:trigger",
       kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="event", args="what to do"),
    _c("auto_rules", 37, "If this, then that", "auto:rules", kind="system",
       risk=RISK_MEDIUM, confirm=True, targets="list|add|remove",
       args="if -> then", aliases=("if_then",)),
    _c("auto_repeat", 37, "Repeat a task every N minutes", "auto:repeat",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="every N minutes",
       args="what to do"),
    _c("auto_macro", 37, "Record what you do, then replay it", "auto:macro",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="record|replay|list",
       args="macro name", aliases=("record_macro", "replay_macro")),
    _c("auto_background", 37, "Run a task in the background and check on it",
       "auto:background", kind="system", risk=RISK_LOW, targets="what to do",
       args="list|status|stop", aliases=("run_in_background",)),
    _c("alert_set", 38, "Tell me when something happens", "auto:alert",
       kind="system", risk=RISK_LOW, confirm=True, targets="what to watch",
       args="how to tell you", aliases=("alert_when", "notify_me")),
    _c("alert_list", 38, "Show the alerts and watches that are set up",
       "auto:list_alerts", kind="system", targets=""),
    _c("trigger_context", 39, "Act only in a situation: at night, on Wi-Fi, "
       "while away, on charge, when locked, before a meeting",
       "auto:context_trigger", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="condition", args="what to do"),
    _c("routine", 40, "Run a named mode: work, focus, meeting, presentation, "
       "study, gaming, travel, end of day", "auto:routine",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="mode name",
       args="on|off"),
    _c("chain", 41, "Do A, then B — run a list of steps in order",
       "auto:chain", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="steps separated by ;", args=""),
    _c("chain_parallel", 41, "Do A and B at the same time",
       "auto:parallel", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="steps separated by ;", args=""),
    _c("chain_until", 41, "Repeat until it is done, or give up",
       "auto:until", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="how many times", args="the step to repeat"),
    _c("chain_foreach", 41, "For each of these, do this", "auto:foreach",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="the list",
       args="the step to run for each"),
]

# --------------------------------------------------------------------------
# 42-48 language, control, safety, feedback, self-management
# --------------------------------------------------------------------------
META = [
    _c("meta_interpret", 42, "Work out what a loose instruction means",
       "auto:interpret", kind="llm", risk=RISK_LOW, targets="the instruction",
       args="", aliases=("vague", "understand_intent")),
    _c("meta_language", 43, "Reply in another language", "auto:set_language",
       kind="ui", risk=RISK_LOW, targets="language code or name", args=""),
    _c("ctl_stop", 44, "Stop what is running", "auto:stop_all", kind="system",
       risk=RISK_MEDIUM, confirm=True, targets="", args=""),
    _c("ctl_pause", 44, "Stop doing anything until I say so", "auto:pause",
       kind="system", risk=RISK_LOW, targets="why, if you want to say",
       args="", aliases=("pause_agent", "hold", "wait", "stop_doing")),
    _c("ctl_resume", 44, "Carry on after a pause", "auto:resume",
       kind="system", risk=RISK_LOW, targets="", args="",
       aliases=("unpause", "carry_on", "continue_working")),
    _c("ctl_undo", 44, "Reverse the last thing you did", "auto:undo",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="", args="",
       aliases=("undo_that", "take_that_back", "revert_last")),
    _c("ctl_redo", 44, "Put back what you just undid", "auto:redo",
       kind="system", risk=RISK_MEDIUM, confirm=True, targets="", args="",
       aliases=("redo_that", "put_it_back")),
    _c("ctl_undo_list", 44, "What can you undo right now?", "auto:undo_list",
       kind="fs", risk=RISK_LOW, targets="", args="",
       aliases=("undo_stack", "what_can_you_undo")),
    _c("ctl_status", 44, "What is running right now?", "auto:status",
       kind="system", targets="", args="", aliases=("show_history", "what_are_you_doing")),
    _c("ctl_history", 44, "Show what I did last, and why", "auto:history",
       kind="fs", targets="run id or last", args="", aliases=("explain_last_action",)),
    _c("ctl_retry", 44, "Do that again", "auto:retry", kind="system",
       risk=RISK_LOW, targets="", args="", aliases=("do_it_again", "undo_action")),
    _c("ctl_capabilities", 44, "What can you do?", "auto:capabilities",
       kind="system", targets="all|category number|name", args="",
       aliases=("what_can_you_do", "show_my_tools")),
    _c("ctl_remember", 44, "Remember or forget a preference",
       "auto:remember", kind="fs", risk=RISK_LOW, targets="fact",
       args="value, or forget to delete", aliases=("remember_this", "forget_this")),
    _c("safe_mode_policy", 45, "Work read-only, or ask before every step",
       "auto:safety_mode", kind="system", risk=RISK_LOW,
       targets="read_only|dry_run|ask_each|scope|never_touch",
       args="what it applies to"),
    _c("safe_always_approve", 45, "Stop asking about safe actions",
       "auto:auto_approve", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="on|off|below risk level"),
    _c("feedback_correct", 46, "The user says the last result was wrong",
       "auto:feedback", kind="llm", risk=RISK_LOW, targets="what was wrong",
       args="what to do instead", aliases=("that_was_wrong", "try_again_differently")),
    _c("feedback_rules", 46, "Show the standing rules I have been given",
       "auto:rules_list", kind="fs", risk=RISK_LOW, targets="", args="",
       aliases=("list_rules", "show_rules", "my_rules")),
    _c("feedback_forget_rule", 46, "Take a standing rule back",
       "auto:rules_forget", kind="fs", risk=RISK_LOW, targets="the rule", args="",
       aliases=("forget_rule", "drop_rule", "unlearn")),
    _c("meta_preferences", 47, "Show or change how the agent behaves",
       "auto:preferences", kind="system", risk=RISK_LOW, targets="list|set",
       args="key and value"),
    _c("meta_teach", 47, "Teach a new task the agent did not know",
       "auto:teach", kind="fs", risk=RISK_MEDIUM, confirm=True,
       targets="what to call it", args="the steps"),
    _c("meta_workflow", 47, "Save, list, share or import a saved workflow",
       "auto:workflow", kind="fs", risk=RISK_LOW,
       targets="list|save|export|import|share", args="name"),
    _c("meta_model", 47, "Switch the model, or show what is set",
       "auto:model", kind="system", risk=RISK_LOW, targets="show|set",
       args="provider and model"),
    _c("meta_stats", 47, "Show usage, cost and error history",
       "auto:usage_stats", kind="system", targets="", args=""),
    _c("meta_reset", 47, "Reset the agent to its defaults", "auto:reset",
       kind="system", risk=RISK_HIGH, confirm=True, targets="what to reset",
       args="config|memory|index|all"),
    _c("meta_update", 47, "Update the agent itself", "auto:self_update",
       kind="system", risk=RISK_HIGH, confirm=True, targets="check|install",
       args="",
       triggers=("update yourself", "update ufo", "upgrade the agent",
                 "get the latest version", "self update")),
    _c("meta_permissions", 47, "Set what the agent may do, one category at a time",
       "auto:permissions", kind="system", risk=RISK_MEDIUM, confirm=True,
       targets="category number or name", args="allow|confirm|block",
       triggers=("set permissions per category", "permissions for finance",
                 "block a whole category", "allow the whole category",
                 "ask before this category")),
    _c("meta_gap", 48, "Report a request the agent cannot do yet",
       "auto:log_gap", kind="fs", risk=RISK_LOW, targets="the request",
       args="why it could not be done"),
]


ALL: tuple[Capability, ...] = tuple(
    APPS + SCREEN + FILES + DOCS + TEXT + WEB + COMMS + WORK + SETTINGS
    + HARDWARE + NETWORK + FIX + MEDIA + DEV + DATA + KNOWLEDGE + TASKS
    + LEARN + PRINT + CLOUD + PHONE + IOT + ACCESS + REMOTE + BACKUP
    + SECURITY + USERS + FINANCE + TRAVEL + SHOPPING + SOCIAL + GAMES
    + ADVANCED + AUTOMATION + META
)


# --------------------------------------------------------------------------
# lookup tables
# --------------------------------------------------------------------------
def _build_indexes() -> tuple[dict[str, Capability], dict[str, Capability]]:
    by_name: dict[str, Capability] = {}
    by_alias: dict[str, Capability] = {}
    for cap in ALL:
        key = cap.name.lower()
        if key in by_name:
            raise ValueError(f"duplicate capability name: {cap.name}")
        by_name[key] = cap
        for alias in cap.aliases:
            by_name.setdefault(alias.lower(), cap)
            by_alias.setdefault(alias.lower(), cap)
    return by_name, by_alias


BY_NAME, _BY_ALIAS = _build_indexes()

# Aliases the model's JSON repair path already knows about must keep pointing
# somewhere sensible. Anything unclaimed here falls through to the catalog.
_LEGACY = {
    "screenshot": "screen_capture",
    "read_text": "screen_read_text",
    "list_windows": "screen_describe",
    "list_dir": "file_find",
    "volume": "set_volume",
    "audio": "set_volume",
    "power": "set_power",
    "browser": "web_open",
    "app": "app_open",
    "focus_window": "window_manage",
    "settings": "set_volume",
}
for _legacy, _target in _LEGACY.items():
    if _legacy not in BY_NAME and _target in BY_NAME:
        BY_NAME[_legacy] = BY_NAME[_target]


def get(name: str) -> Capability | None:
    return BY_NAME.get(str(name or "").strip().lower())


def names() -> tuple[str, ...]:
    """Every action name the agent accepts, aliases included."""
    return tuple(sorted(BY_NAME))


def canonical_names() -> tuple[str, ...]:
    return tuple(sorted(cap.name for cap in ALL))


def by_category(category: int) -> list[Capability]:
    return [c for c in ALL if c.category == category]


def categories() -> list[tuple[int, str, int]]:
    """(number, title, capability count) for every domain that has actions."""
    counts: dict[int, int] = {}
    for cap in ALL:
        counts[cap.category] = counts.get(cap.category, 0) + 1
    return [(n, CATEGORIES[n], counts.get(n, 0)) for n in sorted(CATEGORIES)]


def coverage() -> dict:
    """Summary used by `ufo doctor` and the dashboard."""
    per_category = {n: 0 for n in CATEGORIES}
    for cap in ALL:
        per_category[cap.category] += 1
    return {
        "capabilities": len(ALL),
        "categories": len(CATEGORIES),
        "per_category": per_category,
        "needs_confirmation": sum(1 for c in ALL if c.confirm),
        "high_risk": sum(1 for c in ALL if c.risk >= RISK_HIGH),
        "model_backed": sum(1 for c in ALL if c.kind == "llm"),
    }


def action_names_for_prompt(max_chars: int = 6000) -> str:
    """The capability list injected into the model's system prompt.

    Grouped by domain so the model can find a name by reading the section that
    matches the task, and truncated *by whole lines* so the last entry is never
    cut in half.
    """
    lines: list[str] = []
    for cap in ALL:
        if cap.confirm:
            lines.append(f"  {cap.name}: {cap.summary} (needs confirmation)")
        else:
            lines.append(f"  {cap.name}: {cap.summary}")
        if sum(len(x) + 1 for x in lines) > max_chars:
            break
    return "\n".join(lines)


_KEYWORD_SPLIT = re.compile(r"[|,;]+")


def trigger_phrases() -> list[tuple[Capability, str]]:
    """(capability, normalised phrase) for every trigger, deduplicated."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[Capability, str]] = []
    for cap in ALL:
        for raw in cap.triggers:
            phrase = normalise_phrase(raw)
            if not phrase or (cap.name, phrase) in seen:
                continue
            seen.add((cap.name, phrase))
            out.append((cap, phrase))
    return out


#: Spellings that differ only in punctuation. "wi-fi" and "wifi" are the same
#: word, and the user writes both; the catalog stores one of them, so both are
#: folded before matching or the hyphenated form finds nothing.
SPELLING_ALIASES = {
    "wi fi": "wifi",
    "wi fi network": "wifi network",
    "hot spot": "hotspot",
    "power point": "powerpoint",
    "pdf s": "pdfs",
    "wi fi adapter": "wifi adapter",
}


def normalise_phrase(phrase: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, drop stop words."""
    text = str(phrase or "").lower()
    text = re.sub(r"[^\w\sऀ-ॿ]+", " ", text, flags=re.UNICODE)
    text = collapse_stopwords(" ".join(text.split()))
    return fold_spelling(text)


def fold_spelling(text: str) -> str:
    """Collapse hyphenation variants so "wi-fi" and "wifi" match each other."""
    for variant, canonical in SPELLING_ALIASES.items():
        if variant in text:
            text = text.replace(variant, canonical)
    return text


STOPWORDS = frozenset("""
a an the and or of to for in on at with my me i it that this these those is
are be do does did make makes making get gets got please kindly can you could
would should shall will shall not no yes ok okay then than from into onto by
""".split())


def collapse_stopwords(text: str) -> str:
    words = [w for w in str(text or "").split() if w and w not in STOPWORDS]
    return " ".join(words)


def grouped_by_category() -> dict[int, list[Capability]]:
    out: dict[int, list[Capability]] = {}
    for cap in ALL:
        out.setdefault(cap.category, []).append(cap)
    return out


def iter_capabilities(groups: Iterable[int] | None = None) -> list[Capability]:
    if groups is None:
        return list(ALL)
    wanted = {int(g) for g in groups}
    return [c for c in ALL if c.category in wanted]


if __name__ == "__main__":  # pragma: no cover - manual inspection
    import json

    print(json.dumps(coverage(), indent=2))
    for number, title, count in categories():
        print(f"{number:>2}. {title:<45} {count:>3} actions")
