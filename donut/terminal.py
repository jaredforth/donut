"""Donut's interactive terminal, backed by provider-owned sessions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from rich.style import Style
from rich.text import Text
from textual import work
from textual.message import Message
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from . import conversations
from .config import forget_role, load, profile_name, remember_role, role_choice, save
from .highlight import Highlighter
from .summaries import claude_context, codex_context, is_connected, mcp_servers, parse_mcp_list, render_mcp
from .sessions import ClaudeSession, CodexSession, QwenSession
from .planner_output import ticket_snapshot
from .install_skills import install
from . import background, effort, plugins, ponytail, routing, workspace
from .platform import copy_native, open_window, window_command
from .platform import process_options
from .providers import PERMISSION_CYCLE, PERMISSION_MODES, ollama_base_url
from .ui import COLORS, logo_segments, release_version


def hex_color(name):
    return "#%02x%02x%02x" % COLORS[name]


ORANGE, CORAL, TEAL, NAVY, GRAY = (hex_color(name) for name in ("orange", "coral", "teal", "navy", "gray"))
DIM = "#8a96a3"

# Lines the batch runner prints that belong in the status bar, not the transcript.
RUNNER_PROGRESS = re.compile(r"^\[(?P<label>[^\]]*?) · (?P<seconds>\d+)s elapsed · waiting for agent output\]$")
RUNNER_TICKET = re.compile(r"^\[\d+/(?:\d+|all)\] (?P<ticket>\S+)")
RUNNER_REVIEW = re.compile(r"^\[review \d+/\d+\] (?P<ticket>\S+)")
RUNNER_RECOVER = re.compile(r"^\[recover\] (?P<ticket>\S+)")
# Each mode talks to the model of the role with the same key. /planner, /worker, /reviewer and /soloist pick
# those models; /plan, /work, /review and /solo enter the mode, asking for a model only when none is set.
ROLE_TITLES = {"plan": "planner", "work": "worker", "review": "reviewer", "solo": "soloist"}
ROLE_COMMANDS = {"plan": "/planner", "work": "/worker", "review": "/reviewer", "solo": "/soloist"}
MODE_PLACEHOLDERS = {
    "plan": "Describe what to plan, or type / for commands",
    "work": "Describe what to plan, or type / for commands",
    "review": "Ask the reviewer to check something (it reads, never edits), or type / for commands",
    "solo": "Ask the soloist anything or tell it what to change, or type / for commands",
}
MODE_BADGES = {"plan": ORANGE, "work": CORAL, "review": NAVY, "solo": TEAL}
REVIEW_MODE_PROMPT = (
    "You are the reviewer. Review what is asked below by reading the project; run read-only checks if useful. "
    "Do not edit, create, move or delete files, and do not commit. Point out real problems with file and line, "
    "say how serious each is, and say plainly when something looks right.\n\nReview request:\n"
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Friendlier names for the activity events providers report.
ACTIVITY_NAMES = {
    "reasoning": "thinking",
    "agentMessage": "writing",
    "commandExecution": "running a command",
    "fileChange": "editing files",
    "mcpToolCall": "calling a tool",
    "webSearch": "searching the web",
    "Bash": "running a command",
    "Read": "reading files",
    "Glob": "searching files",
    "Grep": "searching files",
    "Edit": "editing files",
    "MultiEdit": "editing files",
    "Write": "writing a file",
    "WebFetch": "fetching a page",
    "WebSearch": "searching the web",
    "TodoWrite": "updating its plan",
    "Task": "running a subagent",
    "read_file": "reading files",
    "write_file": "writing a file",
    "edit": "editing files",
    "run_shell_command": "running a command",
}


def usage_key(profile):
    """Which quota a profile draws from: one per Claude login folder, one for Codex, one per local model."""
    engine = profile.get("engine")
    if engine == "claude":
        return ("claude", profile.get("claude_config_dir") or "default")
    if engine == "codex":
        return ("codex",)
    return ("local", profile.get("model") or engine)


def remaining(percent_used):
    return max(0, min(100, round(100 - percent_used)))


def left_color(left):
    """Teal while plenty is left, orange under half, coral under a fifth."""
    return TEAL if left > 50 else ORANGE if left > 20 else CORAL


def usage_bar(percent_used, width=6):
    """A thin bar showing how much of the window is left."""
    left = remaining(percent_used)
    filled = max(0, min(width, round(left / 100 * width)))
    color = left_color(left)
    bar = Text("━" * filled, style=color)
    bar.append("─" * (width - filled), style="#3b4652")
    return bar, color


def countdown(resets_at, now=None):
    """Seconds since the epoch -> "2h10m", "3d4h", "45m"; empty when unknown or past."""
    if not resets_at:
        return ""
    seconds = int(resets_at - (now if now is not None else time.time()))
    if seconds <= 0:
        return ""
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{max(minutes, 1)}m"


def pack_entries(entries, width, gap="  "):
    """Join Text entries on one line, starting a new line before an entry that would not fit, so no entry splits."""
    packed, used = Text(), 0
    for entry in entries:
        size = entry.cell_len
        if used and width and used + len(gap) + size > width:
            packed.append("\n")
            used = 0
        elif used:
            packed.append(gap)
            used += len(gap)
        packed.append_text(entry)
        used += size
    return packed


URL = re.compile(r"https?://[^\s<>()\[\]{}`'\"]*[^\s<>()\[\]{}`'\".,;:!?*]")


def link_markdown(source):
    """Turn bare URLs into Markdown autolinks so they render as clickable links; code is left alone."""
    parts = re.split(r"(```.*?(?:```|$)|`[^`\n]*`)", source, flags=re.S)
    for index in range(0, len(parts), 2):  # Even parts are outside code.

        def autolink(match):
            before = parts[index][max(0, match.start() - 2) : match.start()]
            return match[0] if before.endswith(("](", "<")) else f"<{match[0]}>"

        parts[index] = URL.sub(autolink, parts[index])
    return "".join(parts)


def markdown(source):
    from rich.markdown import Markdown

    return Markdown(link_markdown(source), hyperlinks=True)


def link_text(text):
    """Give each URL in a Text a link style, which the terminal shows as a hyperlink and Donut opens on click."""
    for match in URL.finditer(text.plain):
        text.stylize(Style(link=match[0], underline=True), match.start(), match.end())
    return text


class Transcript(RichLog, can_focus=False):
    """The conversation log, with mouse selection: drag to select, and the selection is copied on release."""

    ALLOW_SELECT = True

    def get_selection(self, selection):
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection):
        self._line_cache.clear()
        self.refresh()

    def on_resize(self, event):
        # Something around the transcript grew or shrank (the usage line after a turn, the reply area, a menu):
        # stay at the end unless the user is reading back.
        if getattr(self.app, "follow", False):
            self.call_after_refresh(lambda: self.scroll_end(animate=False))

    def on_mouse_scroll_up(self, event):
        self.app.follow = False  # Reading back: new output no longer pulls the view down.

    def on_mouse_scroll_down(self, event):
        def back_at_end():
            if self.scroll_y >= self.max_scroll_y - 1:
                self.app.follow = True

        self.call_after_refresh(back_at_end)

    def _render_line(self, y, scroll_x, width):
        from rich.segment import Segment
        from textual.strip import Strip

        if y >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        selection = self.text_selection
        key = (y + self._start_line, scroll_x, width, self._widest_line_width)
        if selection is None and key in self._line_cache:
            return self._line_cache[key]
        strip = self.lines[y]
        span = selection.get_span(y) if selection is not None else None
        if span is not None:
            start, end = span
            end = strip.cell_length if end == -1 else end
            # Only the selection's background, so selected text keeps its own colors and stays readable.
            highlight = Style(bgcolor=self.screen.get_component_rich_style("screen--selection").bgcolor or "#2f5f8a")
            end = max(start, min(end, strip.cell_length))
            before, middle, after = strip.crop(0, start), strip.crop(start, end), strip.crop(end, strip.cell_length)
            middle = Strip([Segment(text, (style or Style()) + highlight, control) for text, style, control in middle])
            strip = Strip.join([before, middle, after])
        # The offsets let Textual map a mouse position to a place in the text, which selection needs.
        line = strip.crop_extend(scroll_x, scroll_x + width, self.rich_style).apply_offsets(scroll_x, y)
        if selection is None:
            self._line_cache[key] = line
        return line


# Commands that only show something (or change only what is shown), so they run at once even mid-turn.
INSTANT_COMMANDS = {"/usage", "/help", "/copy", "/context", "/mcp", "/plugin", "/verbosity", "/routing", "/focus"}
# The two versions of the interface, asked about the first time Donut opens: (label, what it says beside it).
INTERFACES = {
    "simple": ("Simple mode", "I just want to use this like I would use Claude Code or Codex"),
    "complex": ("Complex mode", "I know what I'm doing. Show all complex features"),
}
# What simple mode leaves out: the roles, modes and tickets.
COMPLEX_COMMANDS = {
    "/plan",
    "/work",
    "/review",
    "/solo",
    "/planner",
    "/worker",
    "/reviewer",
    "/soloist",
    "/session",
}
SIMPLE_PLACEHOLDER = "Ask anything or say what to change, or type / for commands"
# Commands that talk to the current model's provider, so they need a model to have been picked.
NEEDS_MODEL = {"/plugin", "/mcp", "/context", "/login", "/native", "/effort"}


def session_class(engine):
    """The provider session class for an engine, or None when Donut has none."""
    if engine == "claude":
        return ClaudeSession
    if engine == "codex":
        return CodexSession
    if engine in ("ollama", "openai"):
        return QwenSession
    return None


def is_instant(text):
    words = text.split()
    return bool(words) and (words[0] in INSTANT_COMMANDS or words[:2] == ["/ponytail", "band"])


def model_name(profile, alias):
    return profile.get("model") or alias or "provider default"


COMMANDS = [
    ("/plan", "Plan mode: describe work and the planner writes tickets"),
    ("/work", "Run tickets with the worker, one fresh context each"),
    ("/review", "Review mode: ask the reviewer to check code; it reads, never edits"),
    ("/solo", "Solo mode: talk to the soloist directly, like Claude Code"),
    ("/planner", "Choose the planner's account and model"),
    ("/worker", "Choose the worker's account and model"),
    ("/reviewer", "Choose who reviews: same as planner, any model, or off"),
    ("/soloist", "Choose the soloist's account and model"),
    ("/model", "Choose the model for the current mode"),
    ("/harder", "Auto picked too small a model: move this conversation one model up"),
    ("/routing", "Show the models auto chooses from and what it picked"),
    ("/simple", "Simple mode: use Donut like Claude Code or Codex; it picks the model for you"),
    ("/complex", "Complex mode: show every feature, with the planner, worker, reviewer and tickets"),
    ("/resume", "Resume an earlier conversation in its mode, model and tickets session"),
    ("/session", "Switch this window's tickets folder, .donut/<session>/, or start a new one"),
    ("/rename", "Name this session; its tickets folder becomes .donut/<name>/"),
    ("/project", "Open another project in a new window"),
    ("/permissions", "Choose what the agent may do without asking (Shift+Tab cycles)"),
    ("/auto", "Turn auto mode on or off: the provider's reviewer approves safe actions"),
    ("/focus", "Focus view: show only each turn's final answer (/focus off restores)"),
    ("/verbosity", "How much of the work Donut shows you mid-turn; models think and answer the same"),
    ("/effort", "How hard the current mode's model thinks, in its provider's own levels"),
    ("/usage", "Show usage for each configured connection"),
    ("/plugin", "Browse the provider's plugin commands"),
    ("/mcp", "Show MCP server status"),
    ("/context", "Show context usage for this conversation"),
    ("/clear", "Start completely fresh: new conversation, empty screen; tickets are kept"),
    ("/new", "Same as /clear"),
    ("/copy", "Copy the last answer"),
    ("/login", "Sign in again to the current mode's provider account"),
    ("/native", "Open the provider's own client, then come back"),
    ("/help", "List commands and keys"),
    ("/exit", "Quit Donut"),
]


CLAUDE_TOOL_TITLES = {
    "Bash": ("Bash command", "Bash"),
    "Edit": ("Edit file", "edits"),
    "MultiEdit": ("Edit file", "edits"),
    "Write": ("Write file", "file writes"),
    "NotebookEdit": ("Edit notebook", "notebook edits"),
    "WebFetch": ("Fetch", "fetches"),
    "WebSearch": ("Web search", "web searches"),
}


def approval_title(name, details):
    """(panel title, short phrase for "don't ask again for …") for a Claude tool name or a Codex request method."""
    if "commandExecution" in name:
        return "Codex command", "commands"
    if "fileChange" in name:
        return "Codex file change", "file changes"
    if name in CLAUDE_TOOL_TITLES:
        return CLAUDE_TOOL_TITLES[name]
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return f"{server} · {tool}", tool or server
    return name, name


def _command_text(details):
    command = details.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command) if command is not None else ""


def _clip(text, width=160):
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


# Display only: every level gets the same model, thinking and answers; it changes what Donut shows mid-turn.
VERBOSITY = {
    "quiet": ("Quiet", "show only each turn's final answer"),
    "normal": ("Normal", "show the model's messages as it works (default)"),
    "verbose": ("Verbose", "also show every tool call and a line on each result"),
    "full": ("Full", "also show its thinking and each tool's output"),
}


def tool_summary(name, details):
    """One short line naming what a tool call does: its command, file, URL or query."""
    details = details if isinstance(details, dict) else {}
    summary = approval_summary(details)
    if not summary and isinstance(details.get("changes"), list) and details["changes"]:
        first = details["changes"][0]
        summary = first.get("path", "") if isinstance(first, dict) else ""
        if len(details["changes"]) > 1:
            summary += f" (+{len(details['changes']) - 1} more)"
    if not summary and details.get("tool"):
        summary = str(details.get("server", "")) + " · " + str(details["tool"])
    return summary


def approval_summary(details):
    """One short line for the transcript after a decision."""
    if "command" in details:
        return _clip(_command_text(details).splitlines()[0] if _command_text(details) else "", 100)
    for key in ("file_path", "notebook_path", "path", "url", "query", "grantRoot"):
        if details.get(key):
            return _clip(details[key], 100)
    return ""


def approval_details(details):
    """What the agent wants to do, shaped like Claude Code's permission prompt rather than raw JSON."""
    body = Text()

    def lines(text, prefix, style, limit):
        rows = str(text).splitlines() or [""]
        for row in rows[:limit]:
            body.append(prefix + _clip(row) + "\n", style=style)
        if len(rows) > limit:
            body.append(f"  … {len(rows) - limit} more lines\n", style=DIM)

    if "command" in details:
        command = _command_text(details)
        body.append("$ ", style=DIM)
        lines(command, "", f"bold {ORANGE}", 6)
        if details.get("description"):
            body.append(_clip(details["description"]) + "\n", style=DIM)
        if details.get("cwd"):
            body.append("in " + _clip(details["cwd"]) + "\n", style=DIM)
    path = details.get("file_path") or details.get("notebook_path") or details.get("path")
    if path:
        body.append(_clip(path) + "\n", style=f"bold {ORANGE}")
    edits = details.get("edits") if isinstance(details.get("edits"), list) else None
    if "old_string" in details or "new_string" in details:
        lines(details.get("old_string", ""), "- ", CORAL, 5)
        lines(details.get("new_string", ""), "+ ", TEAL, 5)
    elif edits:
        body.append(f"{len(edits)} edits\n", style=GRAY)
        for edit in edits[:2]:
            if isinstance(edit, dict):
                lines(edit.get("old_string", ""), "- ", CORAL, 3)
                lines(edit.get("new_string", ""), "+ ", TEAL, 3)
    elif "content" in details and path:
        lines(details["content"], "  ", GRAY, 8)
    for key, label in (("url", ""), ("query", ""), ("prompt", ""), ("grantRoot", "write access to "), ("reason", "")):
        if details.get(key) and not (key == "prompt" and "url" not in details):
            body.append(label + _clip(details[key]) + "\n", style=GRAY if key != "reason" else DIM)
    if not body.plain.strip():
        shown = 0
        for key, value in details.items():
            if shown == 6:
                break
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                body.append(f"{key}: ", style=DIM)
                body.append(_clip(value) + "\n", style=GRAY)
                shown += 1
        if not shown:
            body.append(_clip(json.dumps(details, ensure_ascii=False), 300) + "\n", style=GRAY)
    body.append("\nDo you want to proceed?", style=f"bold {GRAY}")
    return body


class Menu(Vertical):
    """An inline list under the transcript, driven from the prompt like Claude Code's menus.

    Focus never leaves the prompt: typing filters the rows, Up/Down move the highlight, Enter or Tab
    choose, and Escape closes. It serves slash-command completion, pickers and tool approvals.
    """

    DEFAULT_CSS = f"""
    Menu {{height: auto; max-height: 14; display: none; padding: 0 1; margin: 0 1;
           border: round {CORAL}; border-title-color: {ORANGE}; border-title-style: bold;
           border-subtitle-color: {DIM};}}
    Menu.-open {{display: block;}}
    Menu OptionList {{border: none; padding: 0; background: transparent; max-height: 12;}}
    Menu OptionList > .option-list--option-highlighted {{background: #3a2a10;}}
    Menu OptionList > .option-list--option-hover {{background: {NAVY};}}
    Menu #menu-empty {{color: {DIM}; display: none;}}
    Menu #menu-details {{display: none; height: auto; max-height: 16; padding: 0 1 1 1;}}
    Menu.-approve {{max-height: 26; border: round {ORANGE}; border-subtitle-color: {DIM};}}
    Menu.-approve #menu-details {{display: block;}}
    """

    class Chosen(Message):
        """row is the highlighted (label, value, description) or None when dismissed; key is how."""

        def __init__(self, row, key="enter"):
            super().__init__()
            self.row, self.key = row, key

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rows = []
        self.visible_rows = []
        self.kind = None

    def compose(self):
        yield Static(id="menu-details")
        options = OptionList(id="menu-options")
        options.can_focus = False
        yield options
        yield Static("No matches", id="menu-empty")

    @property
    def is_open(self):
        return self.has_class("-open")

    def open(self, kind, title, rows, term="", details=None, subtitle=""):
        """rows: (label, value), (label, value, description) or (label, value, description, command) tuples.

        command is shown as a third column: the typed command that does the same thing.

        kind is "slash", "pick" (typing filters) or "approve" (fixed rows, number keys choose, details shown).
        """
        self.kind = kind
        self.border_title = title
        self.border_subtitle = subtitle
        self.rows = [tuple(row) + ("",) * (4 - len(row)) for row in rows]
        self.set_class(kind == "approve", "-approve")
        self.query_one("#menu-details", Static).update(details or "")
        self.add_class("-open")
        self.filter(term)

    def close(self):
        self.kind = None
        self.remove_class("-open", "-approve")

    def filter(self, term):
        term = term.strip().casefold()
        starts = [row for row in self.rows if row[0].casefold().startswith(term)]
        contains = [row for row in self.rows if term in row[0].casefold() and row not in starts]
        self.visible_rows = starts + contains
        self.render_rows(0)
        self.query_one("#menu-empty").display = not self.visible_rows
        return bool(self.visible_rows)

    def render_rows(self, highlighted):
        """Redraw the rows; the highlighted one gets a pointer and brighter text, like Claude Code."""
        width = max((len(row[0]) for row in self.visible_rows), default=0)
        described = max((len(row[2]) for row in self.visible_rows), default=0)
        prompts = []
        for index, (label, _, description, command) in enumerate(self.visible_rows):
            chosen = index == highlighted
            prompt = Text("› " if chosen else "  ", style=f"bold {CORAL}")
            if self.kind == "approve":
                prompt.append(f"{index + 1}. ", style=ORANGE if chosen else DIM)
            prompt.append(label.ljust(width), style=f"bold {ORANGE}" if chosen else ORANGE)
            if description or command:
                prompt.append("   " + description.ljust(described), style="#ffffff" if chosen else DIM)
            if command:
                prompt.append("   " + command, style=f"bold {TEAL}" if chosen else TEAL)
            prompts.append(Option(prompt))
        options = self.query_one(OptionList)
        options.set_options(prompts)
        if prompts:
            options.highlighted = highlighted

    def move(self, step):
        if self.visible_rows:
            index = self.query_one(OptionList).highlighted or 0
            self.render_rows((index + step) % len(self.visible_rows))

    def current(self):
        index = self.query_one(OptionList).highlighted
        if index is None or index >= len(self.visible_rows):
            return None
        return self.visible_rows[index]

    def on_option_list_option_selected(self, event):
        event.stop()
        self.render_rows(event.option_index)
        self.post_message(self.Chosen(self.current(), "enter"))


def shell_candidates(line, project):
    """Names that could finish the last word of a !command: programs for the first word, else paths."""
    head, _, word = line.lstrip("!").lstrip().rpartition(" ")
    if not head.strip() and "/" not in word:
        names = {f for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isdir(d)
                 for f in os.listdir(d) if os.access(os.path.join(d, f), os.X_OK)}
        return word, sorted(n for n in names if n.startswith(word))
    folder, _, prefix = word.rpartition("/")
    dir_part = folder + "/" if (folder or word.startswith("/")) else ""
    try:
        entries = os.listdir(Path(project) / os.path.expanduser(dir_part or "."))
    except OSError:
        return word, []
    out = []
    for name in sorted(entries):
        if name.startswith(prefix) and (prefix.startswith(".") or not name.startswith(".")):
            full = dir_part + name
            out.append(full + ("/" if (Path(project) / os.path.expanduser(full)).is_dir() else ""))
    return word, out


class Draft(TextArea):
    """The prompt editor. Enter sends; Shift+Enter, Alt+Enter or Ctrl+J insert a newline.

    Plain terminals send the same bytes for Enter and Ctrl+Enter, so Enter has to be the send key to work
    everywhere. Ctrl+Enter still sends in terminals that distinguish it (kitty, WezTerm, Ghostty, Windows Terminal).
    """

    SEND_KEYS = ("enter", "ctrl+enter")
    NEWLINE_KEYS = ("shift+enter", "alt+enter", "ctrl+j")

    class Submitted(Message):
        pass

    def recall(self, step):
        """Step through sent messages (-1 older, +1 newer); False when the key should move the cursor instead."""
        app = self.app
        row = self.cursor_location[0]
        if step < 0 and row == 0 and app.history:
            if app.history_at is None:
                app.history_stash = self.text
                app.history_at = len(app.history) - 1
            else:
                app.history_at = max(0, app.history_at - 1)
        elif step > 0 and app.history_at is not None and row == self.document.line_count - 1:
            app.history_at += 1
            if app.history_at >= len(app.history):
                app.history_at = None
        else:
            return False
        app.recalled = True  # one-shot: the Changed event from this load must not open the slash menu
        self.load_text(app.history_stash if app.history_at is None else app.history[app.history_at])
        self.move_cursor(self.document.end)
        return True

    async def _on_key(self, event):
        menu = self.app.query_one(Menu)
        if menu.is_open and event.key in ("up", "down", "enter", "tab", "escape", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            if event.key in ("up", "down"):
                menu.move(-1 if event.key == "up" else 1)
            elif event.key == "escape":
                self.post_message(Menu.Chosen(None, "escape"))
            else:
                self.post_message(Menu.Chosen(menu.current(), event.key))
            return
        if menu.kind == "approve" and event.key.isdigit() and 1 <= int(event.key) <= len(menu.visible_rows):
            event.stop()
            event.prevent_default()
            menu.render_rows(int(event.key) - 1)
            self.post_message(Menu.Chosen(menu.current(), "enter"))
            return
        if event.key == "tab" and self.text.startswith("!") and self.selection.is_empty:
            event.stop()
            event.prevent_default()
            self.app.complete_shell(self)
            return
        if event.key in ("up", "down") and self.recall(-1 if event.key == "up" else 1):
            event.stop()
            event.prevent_default()
            return
        if event.key in self.SEND_KEYS:
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
        elif event.key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
        else:
            await super()._on_key(event)


class Donut(App):
    TITLE = "Donut"
    QUOTA_REFRESH_SECONDS = 300
    AUTO_REFRESH_QUOTAS = True  # Tests turn this off so no provider is started in the background.
    ASK_INTERFACE = True  # Ask simple or complex the first time Donut opens; tests turn this off.
    ENABLE_COMMAND_PALETTE = False  # The inline slash menu replaces Textual's palette.
    CSS = f"""
    Screen {{background: #101820;}}
    #transcript {{height: 1fr; padding: 0 1; background: #101820; scrollbar-size-vertical: 1;
                   scrollbar-background: #101820; scrollbar-color: {CORAL} 50%; scrollbar-color-hover: {ORANGE};}}
    #response {{height: auto; max-height: 12; overflow-x: hidden; overflow-y: auto; scrollbar-size-vertical: 0;
                 padding: 0 1;}}
    #thinking {{height: 1; padding: 0 2; display: none;}}
    #thinking.-busy {{display: block;}}
    #usage {{height: auto; max-height: 2; padding: 0 2;}}
    #band {{height: auto; padding: 0 2;}}
    .-focus #status, .-focus #usage, .-focus Footer {{display: none;}}
    #draft {{height: auto; min-height: 3; max-height: 10; margin: 0 1; border: round {CORAL} 60%;
             background: #101820; border-subtitle-align: right; border-subtitle-color: {GRAY};}}
    #draft:focus {{border: round {ORANGE};}}
    #draft.-shell {{border: round {TEAL};}}
    #status {{height: auto; max-height: 3; padding: 0 2;}}
    Footer {{background: #101820;}}
    Footer FooterKey .footer-key--key {{color: {ORANGE}; background: #101820; text-style: bold;}}
    Footer FooterKey .footer-key--description {{color: {DIM};}}
    """
    BINDINGS = [
        Binding("enter", "send", "Send"),
        # priority=True: the editor binds ctrl+c to copy, which would otherwise swallow the cancel key.
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear", "Clear view"),
        Binding("f2", "models", "Models"),
        Binding("f3", "modes", "Mode"),
        # priority=True: the editor would otherwise use shift+tab itself.
        Binding("shift+tab", "cycle_permission", "Permissions", priority=True),
    ]

    def __init__(self, project, alias=None, session=None):
        super().__init__()
        self.project = project
        # Each window works from its own tickets folder, .donut/<session>/, made when it is first needed.
        self.session_name = session or workspace.new_name(project)
        self.config = load()
        self.mode = "plan"  # plan, work, review or solo
        aliases = self.config.get("aliases", {})
        if alias and alias not in aliases:
            raise ValueError("Unknown model alias: " + alias)
        self.selected, self.profiles = {}, {}
        for role, key in (("plan", "planner"), ("work", "default_model")):
            saved = None if alias else role_choice(self.config, role)
            if saved:
                self.selected[role], self.profiles[role] = saved[0], copy.deepcopy(saved[1])
            else:
                name = alias or self.config.get(key) or next(iter(aliases), None)
                self.selected[role], self.profiles[role] = name, copy.deepcopy(aliases.get(name, {}))
        soloist = role_choice(self.config, "solo")  # No default: /solo asks the first time.
        self.selected["solo"], self.profiles["solo"] = (
            (soloist[0], copy.deepcopy(soloist[1])) if soloist else (None, {})
        )
        # Auto: the soloist's model is picked for each conversation from how difficult its first request is.
        saved_solo = (self.config.get("roles") or {}).get("solo") or {}
        self.auto = not soloist and saved_solo.get("alias") == routing.AUTO
        self.route = None  # {"index", "score", "harder"} once this conversation's model is picked.
        self.cancelled = False
        self.history = []  # Sent messages, oldest first; Up/Down in the prompt walk them.
        self.history_at = None
        self.history_stash = ""
        self.recalled = False
        self.steps = None  # The models auto chooses from, cheapest first; found on first use.
        # Simple or complex changes only what Donut shows and which defaults it starts with, never what a model
        # is told. Until the user has chosen (asked the first time Donut opens), everything is shown.
        self.interface = self.config.get("interface") if self.config.get("interface") in INTERFACES else "complex"
        if self.simple:
            self.mode = "solo"
        # Who reviews each finished ticket: "planner" follows the planner (the default), "off" disables review,
        # and "chosen" keeps the alias and model picked with /review whatever the planner becomes.
        roles = self.config.get("roles") or {}
        if self.config.get("reviewer") == "off":
            self.reviewer = "off"
        elif roles.get("review") or self.config.get("reviewer") in aliases:
            self.reviewer = "chosen"
            chosen = role_choice(self.config, "review")
            self.selected["review"], self.profiles["review"] = chosen[0], copy.deepcopy(chosen[1])
        else:
            self.reviewer = "planner"
        self.sync_reviewer()
        self.sessions = {}
        self.busy = False
        self.active = None
        self.child = None
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        self.last_answer = ""
        self.picking = None
        self.follow = True  # Keep the transcript at its end until the user scrolls up.
        self.background_tasks = {}  # task id -> (kind, description) for shells and agents models left running
        self.threads = {}  # "plan", "solo", "review" -> the session holding that conversation now
        # Every exchange in this window, whatever mode and model: each session is given what it has not seen.
        self.exchanges = []
        self.turn_tools = []
        self.after_tools = False
        level = self.config.get("verbosity")
        self.verbosity = level if level in VERBOSITY else "normal"  # What the user sees, never what models do.
        self.thinking_text = ""
        self.message_start = 0  # Where the turn's latest message starts in turn_text, for quiet mode.
        self.queued = []  # Messages typed during a turn, and background wake-ups waiting for the turn to end.
        self.shell_runs = []  # (command, exit code, output) from !command, given to the model with the next message.
        self.waiting = {}  # Background waits a model asked for: task -> command.
        self.always_allow = set()
        mode = self.config.get("permission_mode")
        self.permission = mode if mode in PERMISSION_MODES else "ask"
        self.previous_permission = "ask"
        self.activity = ""
        self.running = None  # (role, alias, profile) of the model doing the current turn
        self.started = time.monotonic()
        self.detail = ""
        self.quotas = {}  # usage_key -> [{"window", "percent", "resets_at"}]; [] means no plan limits apply
        self.session_profiles = {}
        self.ponytail_installed = {}  # usage_key -> whether Ponytail is installed for that login
        self.spent = 0.0  # Dollars the providers reported for this window's turns, shown on the input box.
        self.estimates = {}  # Response id -> estimated dollars of the turn in progress, until its cost is reported.

    def compose(self):
        yield Transcript(id="transcript", wrap=True, highlight=False, markup=False, auto_scroll=True)
        yield Static(id="response")
        yield Static(id="thinking")
        yield Menu(id="menu")
        yield Draft(
            id="draft",
            soft_wrap=True,
            show_line_numbers=False,
            placeholder=self.placeholder(),
        )
        yield Static(id="status")
        yield Static(id="band")
        yield Static(id="usage")
        yield Footer()

    def on_mount(self):
        driver = getattr(self, "_driver", None)
        if driver is not None:
            driver.write("\x1b[22;0t")  # Save the terminal's own title, restored when Donut exits.
        try:
            conversations.remember_project(self.project)
        except OSError:
            pass
        self.welcome()
        self.add_spent(None)
        self.status("Ready")
        if not self.config.get("aliases"):
            self.say(Text("No accounts configured. Exit and run donut setup.", style=CORAL))
        self.query_one(Draft).focus()
        if self.ASK_INTERFACE and self.config.get("aliases") and self.config.get("interface") not in INTERFACES:
            self.run_worker(self.first_open(), group="first-open")
        self.set_interval(0.1, self.tick)
        if self.AUTO_REFRESH_QUOTAS:
            self.refresh_quotas_soon(connect=True)
            self.set_interval(self.QUOTA_REFRESH_SECONDS, lambda: self.refresh_quotas_soon(connect=True))

    def refresh_quotas_soon(self, connect=False):
        self.run_worker(self.refresh_quotas(connect), group="quota", exclusive=True)

    async def refresh_quotas(self, connect=False):
        """Ask each account how much of its plan is left, without sending any prompt.

        connect=True starts a session for an account that has none yet; otherwise only open sessions are asked.
        """
        for key, (label, alias, profile) in self.usage_groups().items():
            if key[0] == "local":
                continue
            session = next(
                (s for (a, m), s in self.sessions.items() if usage_key(self.session_profiles.get((a, m), {})) == key),
                None,
            )
            try:
                if session is None and connect:
                    session = await self.session(alias, profile, track=False)
                if session is None:
                    continue
                windows = await session.quota()
            except Exception:
                continue  # Not logged in, offline, or the provider is not installed: the bar stays as it was.
            if windows is not None:
                self.quotas[key] = windows
                try:
                    self.status()
                except NoMatches:
                    return

    def on_resize(self, event):
        self.call_after_refresh(self.status)  # Re-pack the status line for the new width.

    def tick(self):
        if self.busy:
            try:
                self.render_thinking()
            except NoMatches:
                pass  # The timer can fire once more while the app shuts down.

    @property
    def lanes(self):
        """This window's tickets folder: .donut/<session>/ (swim_lanes/ for an older project's tickets)."""
        return workspace.lanes_dir(self.project, self.session_name)

    @property
    def role(self):
        """The model role the current mode talks to; each mode has its own."""
        return self.mode

    @property
    def purpose(self):
        """Solo and review keep conversations of their own, apart from the planner's, even on the same model."""
        return self.mode if self.mode in ("solo", "review") else None

    def session_key(self, alias, profile, purpose=None):
        return (f"{alias}@{purpose}" if purpose else alias, profile.get("model"))

    @property
    def simple(self):
        return self.interface == "simple"

    def check_action(self, action, parameters):
        """Simple mode has one mode, so the mode key is neither shown nor active."""
        return not (action == "modes" and self.simple)

    def placeholder(self):
        return SIMPLE_PLACEHOLDER if self.simple else MODE_PLACEHOLDERS[self.mode]

    def set_mode(self, mode):
        self.mode = mode
        try:
            self.query_one(Draft).placeholder = self.placeholder()
        except NoMatches:
            pass
        self.status()

    def solo_label(self):
        """What the soloist is set to, for the status line: its model, or auto and what auto picked."""
        alias, profile = self.selected.get("solo"), self.profiles.get("solo", {})
        if not self.auto:
            return model_name(profile, alias) if profile else "not set"
        if not self.route or not profile:
            return "auto"
        score = self.route.get("score")
        return f"auto → {model_name(profile, alias)}" + (f" · {score}/10" if score else "")

    def sync_reviewer(self):
        """Follow the planner while the reviewer is the default; a chosen reviewer never changes with it."""
        if self.reviewer == "off":
            self.selected["review"], self.profiles["review"] = None, {}
        elif self.reviewer == "planner":
            self.selected["review"] = self.selected.get("plan")
            self.profiles["review"] = copy.deepcopy(self.profiles.get("plan", {}))

    def start_turn(self, role, detail="", activity="thinking"):
        """Mark which model is now working, for the spinner and the status line."""
        alias = self.selected.get(role)
        self.running = (role, alias, self.profiles.get(role, {}))
        self.detail = detail
        self.activity = activity
        self.started = time.monotonic()
        self.status()

    def render_thinking(self):
        widget = self.query_one("#thinking", Static)
        widget.set_class(self.busy, "-busy")
        if not self.busy:
            return
        elapsed = int(time.monotonic() - self.started)
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        line = Text(frame + " ", style=f"bold {ORANGE}")
        if self.running:
            role, alias, profile = self.running
            line.append(model_name(profile, alias), style=f"bold {ORANGE}")
            if not self.simple:
                line.append(f"  {ROLE_TITLES.get(role, role)}", style=DIM)
            line.append(f"  is {self.activity or 'thinking'}", style=GRAY)
            if self.detail:
                detail = self.detail if len(self.detail) <= 40 else self.detail[:39] + "…"
                line.append(" · " + detail, style=TEAL)
        else:
            line.append(self.activity or "working", style=GRAY)
        line.append(f" · {elapsed}s", style=CORAL)
        line.append("   ctrl+c to cancel", style=DIM)
        widget.update(line)

    def welcome(self):
        """The logo and a short orientation, written at the top of the transcript when Donut opens."""
        for runs in logo_segments():
            line = Text()
            for text, color in runs:
                line.append(text, style=hex_color(color) if color else None)
            self.say(line)
        title = Text("  DONUT", style=f"bold {ORANGE}")
        title.append("  /  " + release_version(), style=CORAL)
        title.append("   " + str(self.project), style=DIM)
        if self.simple:
            self.say(title)
            self.say(Text("  Ask for what you need. Donut picks the model for each conversation.", style=GRAY))
            hints = (
                ("/harder", "stronger model"),
                ("/clear", "new conversation"),
                ("/", "commands"),
                ("Shift+Enter", "newline"),
            )
        else:
            title.append("   tickets " + workspace.relative(self.project, self.lanes), style=DIM)
            self.say(title)
            self.say(Text("  The planner writes the tickets. The worker takes them one at a time.", style=GRAY))
            hints = (
                ("/plan", "tickets"),
                ("/work", "build"),
                ("/review", "check"),
                ("/solo", "direct"),
                ("/", "commands"),
                ("!", "shell"),
                ("Shift+Enter", "newline"),
            )
        hint = Text("  ")
        for key, description in hints:
            hint.append(key, style=f"bold {ORANGE}")
            hint.append(" " + description + "   ", style=DIM)
        self.say(hint)
        self.say("")

    def status(self, activity=None):
        """Redraw the spinner, the role line and the usage bar. activity is what the running model is doing."""
        if activity is not None and activity not in ("Ready", "Thinking…"):
            self.activity = activity
        try:
            self.render_thinking()
            self.render_usage()
            self.render_band()
        except NoMatches:
            return
        # One entry per piece: the mode badge, every role and what it is set to (in every mode), then the
        # permission mode. The current mode's role is brighter and the role doing a turn gets a filled dot.
        entries = [
            Text(
                f" {self.mode.upper()} ",
                style=f"bold {'#ffffff' if self.mode == 'review' else '#101820'} on {MODE_BADGES[self.mode]}",
            )
        ]
        running_role = self.running[0] if self.busy and self.running else None
        # Simple mode shows one entry, the model in use, in place of the mode badge and the four roles.
        roles = {"solo": "model"} if self.simple else ROLE_TITLES
        if self.simple:
            entries = []
        for role, title in roles.items():
            alias, profile = self.selected.get(role), self.profiles.get(role, {})
            if role == "solo" and (self.auto or not profile):
                value = self.solo_label()
            elif not profile:
                value = "off" if role == "review" else "not set"
            elif role == "review" and self.reviewer == "planner":
                value = f"{model_name(profile, alias)} (= planner)"
            else:
                value = model_name(profile, alias)
            if profile.get("effort"):
                value += f" · {profile['effort']}"
            entry = Text()
            if role == running_role:
                entry.append("● ", style=f"bold {ORANGE}")
                entry.append(title + " ", style=f"bold {ORANGE}")
                entry.append(value, style=f"bold {ORANGE}")
            else:
                focus = not running_role and role == self.role
                entry.append("○ ", style=f"bold {ORANGE}" if focus else DIM)
                entry.append(title + " ", style=GRAY if focus else DIM)
                known = bool(profile) or (role == "solo" and self.auto)
                entry.append(value, style=f"bold {GRAY}" if focus and known else (GRAY if known else DIM))
            entries.append(entry)
        label = PERMISSION_MODES[self.permission][0].lower()
        permission = Text()
        if self.permission == "ask":
            permission.append(f"⏵ {label}", style=DIM)
        else:
            permission.append(f"⏵⏵ {label} on", style=f"bold {CORAL if self.permission == 'full' else ORANGE}")
        permission.append("  (shift+tab)", style=DIM)
        if self.queued:
            permission.append(f"   {len(self.queued)} queued", style=CORAL)
        running = self.background_summary()
        if running:
            permission.append(f"   ⏳ {running}", style=f"bold {TEAL}")
        if self.verbosity != "normal":
            permission.append(f"   {self.verbosity}", style=DIM)
        entries.append(permission)
        if not self.simple:
            session = Text("session ", style=DIM)
            session.append(self.session_label(), style=f"bold {ORANGE}")
            entries.append(session)
        if profile_name() != "default":
            named = Text("profile ", style=DIM)
            named.append(profile_name(), style=f"bold {TEAL}")
            entries.append(named)
        self.show_title()
        widget = self.query_one("#status", Static)
        widget.update(pack_entries(entries, widget.content_size.width or self.size.width - 4))

    def usage_groups(self):
        """One entry per quota: every configured Claude login and Codex, plus the local models in use.

        Maps usage_key -> (label, alias, profile).
        """
        groups = {}
        selected = {self.selected.get(role) for role in ("plan", "work")}
        for alias, profile in self.config.get("aliases", {}).items():
            key = usage_key(profile)
            if key[0] == "local" and alias not in selected:
                continue
            label = alias if key[0] != "local" else model_name(profile, alias).split(":")[0]
            if key not in groups or alias in selected:
                groups[key] = (label, alias, profile)
        for role in ("plan", "work", "review", "solo"):  # Models picked by hand may not be saved aliases.
            profile, alias = self.profiles.get(role, {}), self.selected.get(role)
            if profile and usage_key(profile) not in groups:
                key = usage_key(profile)
                label = alias if key[0] != "local" else model_name(profile, "").split(":")[0]
                groups[key] = (label, alias, profile)
        return groups

    def render_usage(self):
        """One entry per account, wrapped onto a second line rather than cut off when the window is narrow."""
        entries = []
        running = usage_key(self.running[2]) if self.busy and self.running else None
        for key, (label, _, _) in self.usage_groups().items():
            line = Text()
            entries.append(line)
            line.append(label, style=f"bold {ORANGE}" if key == running else GRAY)
            line.append(" ")
            if key[0] == "local":
                line.append("local", style=DIM)
                continue
            windows = self.quotas.get(key)
            if windows is None:
                line.append("──────", style="#3b4652")
                line.append(" –", style=DIM)
                continue
            if not windows:
                line.append("no plan limit", style=DIM)
                continue
            first, *rest = windows
            bar, color = usage_bar(first["percent"])
            line.append_text(bar)
            line.append(f" {remaining(first['percent'])}% left", style=color)
            if first["window"]:
                line.append(" " + first["window"], style=DIM)
            reset = countdown(first.get("resets_at"))
            if reset:
                line.append(f" ↻{reset}", style=DIM)
            for window in rest[:1]:
                left = remaining(window["percent"])
                line.append(" · ", style=DIM)
                line.append(f"{left}%", style=left_color(left))
                if window["window"]:
                    line.append(" " + window["window"], style=DIM)
        widget = self.query_one("#usage", Static)
        widget.update(pack_entries(entries, widget.content_size.width or self.size.width - 4, gap="   "))

    def say(self, text, style=None):
        # Lines measure as at most 80 columns on their own; expand them to the transcript's real width.
        if isinstance(text, str):
            text = Text(text, style=style or "")
        if isinstance(text, Text):
            text = link_text(text)
        # While the user reads back, new lines are added without pulling the view down.
        self.query_one(RichLog).write(text, expand=True, scroll_end=self.follow)

    async def choose(self, title, rows, kind="pick", details=None, subtitle=""):
        """Show rows in the inline menu and wait for a choice. Returns the chosen value, or None.

        A "pick" uses the prompt as its filter, so the prompt is cleared around it. An "approve" leaves
        whatever the user is drafting alone.
        """
        if not rows:
            return None
        menu = self.query_one(Menu)
        future = asyncio.get_running_loop().create_future()
        self.picking = future
        if kind == "pick":
            self.query_one(Draft).clear()
        menu.open(kind, title, rows, details=details, subtitle=subtitle)
        try:
            return await future
        finally:
            self.picking = None
            menu.close()
            if kind == "pick":
                self.query_one(Draft).clear()

    def slash_rows(self):
        rows = [
            (name, name, description)
            for name, description in COMMANDS
            if not (self.simple and name in COMPLEX_COMMANDS) and name != "/" + self.interface
        ]
        session = self.sessions.get(self.session_key(self.selected[self.role], self.profiles[self.role], self.purpose))
        if isinstance(session, ClaudeSession):
            for command in session.info.get("commands", []):
                name = command.get("name") if isinstance(command, dict) else command
                if isinstance(name, str) and "/" + name.lstrip("/") not in {row[0] for row in rows}:
                    description = command.get("description", "") if isinstance(command, dict) else ""
                    rows.append(("/" + name.lstrip("/"), "/" + name.lstrip("/"), (description or "")[:80]))
        # Installed plugins and skills (Ponytail's /ponytail-review, …), listed before any session is open.
        for command in plugins.commands(self.profiles.get(self.role, {}), self.project):
            if command["name"] not in {row[0] for row in rows}:
                rows.append((command["name"], command["name"], command["description"][:80]))
        return rows

    def on_menu_chosen(self, event):
        menu = self.query_one(Menu)
        draft = self.query_one(Draft)
        if menu.kind in ("pick", "approve"):
            if self.picking and not self.picking.done():
                self.picking.set_result(event.row[1] if event.row else None)
            return
        kind = menu.kind
        menu.close()
        if event.row is None:
            return
        command = event.row[1]
        if kind == "shell" or event.key == "tab":
            draft.load_text(command if kind == "shell" else command + " ")
            draft.move_cursor(draft.document.end)
            return
        draft.clear()
        self.submit(command)

    def event(self, kind, text, key=None, source=None):
        if kind == "spend":
            self.estimates[text["id"]] = text["dollars"]
            self.add_spent(None)
            return
        buffer = getattr(source, "wake_buffer", None)
        if kind == "wake_start":
            if self.busy:
                source.wake_buffer = []  # Another turn is on screen; show this one when it ends.
            else:
                self.begin_wake(source)
            return
        if kind == "wake_end":
            if buffer is not None:
                source.wake_buffer = None
                self.say(Text(f"  ⏰ {self.wake_title(source)} · woken by a background task", style=f"bold {TEAL}"))
                self.say(markdown("".join(buffer).strip() or "(no reply)"))
            elif source is not None and source is self.active:
                self.finish_wake(source)
            return
        if kind == "background":
            self.say(Text("  ⏰ " + str(text), style=TEAL))
            return
        if kind == "task":
            self.track_task(text)
            return
        if buffer is not None and kind == "text":
            buffer.append(text)
            return
        if kind in ("tool", "tool_result", "break", "text"):
            self.flush_thinking()
        if kind == "break":
            if self.turn_text.strip():
                self.turn_text += "\n\n"
                self.message_start = len(self.turn_text)
            return
        if kind == "thinking":
            if self.verbosity == "full":
                self.thinking_text += str(text)
            return
        if kind == "tool":
            if isinstance(text, dict):
                summary = tool_summary(text.get("name"), text.get("input"))
                self.turn_tools.append(f"{text.get('name', 'Tool')} {summary}".strip()[:160])
            if self.verbosity in ("verbose", "full") and isinstance(text, dict):
                self.commit_paragraphs(final=True)
                line = Text("  ⏺ " + str(text.get("name", "Tool")), style=f"bold {TEAL}")
                summary = tool_summary(text.get("name"), text.get("input"))
                if summary:
                    line.append("  " + summary, style=GRAY)
                self.say(line)
                self.after_tools = True
            return
        if kind == "tool_result":
            if self.verbosity in ("verbose", "full") and isinstance(text, dict):
                self.show_tool_result(text)
            return
        if kind == "text":
            self.turn_text += text
            self.activity = "writing"
            if self.verbosity != "quiet":
                self.commit_paragraphs()
            start = self.message_start if self.verbosity == "quiet" else self.shown
            self.show_pending(self.turn_text[max(start, self.shown) :].strip())
        elif kind == "error":
            self.say("Error: " + str(text), CORAL)
        elif kind == "usage":
            if key is not None and isinstance(text, list):
                self.quotas[key] = text
                self.status()
        elif kind in ("command", "command_output"):
            task = getattr(self.active, "task", None)
            if task is not None and isinstance(text, dict):
                if kind == "command":
                    task.command(text.get("id"), text.get("command", ""))
                else:
                    task.command_output(text.get("id"), text.get("output", ""))
        else:
            name = str(text)
            if name.startswith("mcp__"):
                name = "using " + name[5:].replace("__", " · ")
            self.status(ACTIVITY_NAMES.get(name, name if name.startswith("using ") else "using " + name))

    async def approve(self, name, details):
        """Ask about one tool call. Returns "yes", "always" (for the rest of this session) or "no"."""
        details = details if isinstance(details, dict) else {"input": details}
        title, short = approval_title(name, details)
        if title in self.always_allow:
            return "always"
        rows = [
            ("Yes", "yes"),
            (f"Yes, and don't ask again for {short} this session", "always"),
            ("No", "no"),
        ]
        decision = await self.choose(
            title, rows, kind="approve", details=approval_details(details), subtitle="Enter to choose · Esc to deny"
        )
        decision = decision or "no"
        if decision == "always":
            self.always_allow.add(title)
        summary = approval_summary(details)
        line = Text("  ✓ " if decision != "no" else "  ✗ ", style=TEAL if decision != "no" else CORAL)
        line.append(("Allowed " if decision != "no" else "Denied ") + title, style=GRAY)
        if summary:
            line.append("  " + summary, style=DIM)
        self.say(line)
        return decision

    async def set_permission(self, mode, announce=True):
        """Apply a permission mode to every open session through the provider's own mechanism, and remember it."""
        if mode != self.permission:
            self.previous_permission = self.permission
        self.permission = mode
        self.status("Ready" if not self.busy else "Thinking…")
        try:
            config = load()
            config["permission_mode"] = mode
            save(config)
            self.config["permission_mode"] = mode
        except (ValueError, OSError) as error:
            self.say(f"Permission mode applies to this session only: {error}", DIM)
        failures = set()
        for session in list(self.sessions.values()):
            try:
                await session.set_permission(mode)
            except Exception as error:
                failures.add(str(error))
        for error in sorted(failures):  # Once per reason, not once per open session.
            self.say(f"Could not change the provider's permission mode: {error}", CORAL)
        if announce:
            label, description = PERMISSION_MODES[mode]
            line = Text(f"  {label} mode", style=f"bold {ORANGE if mode != 'full' else CORAL}")
            line.append("  " + description + ".", style=GRAY)
            engine = self.profiles[self.role].get("engine")
            if mode == "ask" and engine in ("ollama", "openai"):
                line.append(" Qwen can't show approval prompts here, so it edits files without asking.", style=DIM)
            self.say(line)

    def action_cycle_permission(self):
        modes = PERMISSION_CYCLE
        following = modes[(modes.index(self.permission) + 1) % len(modes)] if self.permission in modes else "ask"
        self.run_worker(self.set_permission(following), group="permission")

    async def session(self, alias, profile, purpose=None, resume=None, conversation=None, track=True):
        """The persistent provider session for a model and purpose. Review-mode sessions are read-only.

        resume reopens a provider session or thread by its id (from /resume), replacing any open one.

        Each purpose (plan, solo, review) is one conversation whatever model holds it. When its model changes,
        the conversation carries over: in the same live session when the provider can switch models itself
        (Claude, or Codex, on the same login), otherwise by handing the new model the conversation so far.
        track=False is for sessions opened only to list models or read usage.
        """
        thread = purpose or "plan"
        key = self.session_key(alias, profile, purpose)
        if conversation is not None and key in self.sessions:
            await self.sessions.pop(key).close()
        previous = self.threads.get(thread) if track and conversation is None else None
        if previous is not None and (previous not in self.sessions.values() or self.sessions.get(key) is previous):
            previous = None
        if previous is not None and key not in self.sessions:
            switched = await self.switch_in_place(previous, alias, profile, key)
            if switched:
                return switched
        if key not in self.sessions:
            engine = profile.get("engine")
            cls = session_class(engine)
            if not cls:
                raise ValueError(
                    "Persistent sessions for this engine are not implemented yet. Use /native for its full interactive client."
                )
            holder = {}
            emit = lambda kind, data, key=usage_key(profile): self.event(kind, data, key, holder.get("session"))  # noqa: E731
            session = cls(self.project, profile, emit, self.approve)
            holder["session"] = session
            session.permission = self.permission
            session.read_only = purpose == "review"
            session.resume_id = resume
            session.conversation_id = conversation
            try:
                await session.connect()
            except BaseException:
                if hasattr(session, "process"):
                    await session.close()
                raise
            session.task = ponytail.Task(self.project, engine, profile.get("model"))
            self.sessions[key] = session
            self.session_profiles[key] = profile
        session = self.sessions[key]
        if previous is not None and session is not previous:
            # Another provider or login: give the new model the conversation so far, then retire the old one.
            old_key = next(k for k, v in self.sessions.items() if v is previous)
            old_name = model_name(self.session_profiles.get(old_key, {}), old_key[0].split("@")[0])
            self.sessions.pop(old_key, None)
            self.session_profiles.pop(old_key, None)
            try:
                await previous.close()
            except Exception:
                pass
            if self.exchanges:
                self.say(Text(f"  {model_name(profile, alias)} continues the conversation from {old_name}.", style=DIM))
        if track:
            session.donut_conversation = thread  # Not .thread: Codex sessions keep their own thread id there.
            self.threads[thread] = session
        return session

    async def switch_in_place(self, session, alias, profile, key):
        """Change the model of a live session on the same login, keeping the provider's own full context."""
        old_key = next(k for k, v in self.sessions.items() if v is session)
        old_profile = self.session_profiles.get(old_key, {})
        if usage_key(old_profile) != usage_key(profile) or profile.get("engine") not in ("claude", "codex"):
            return None
        if not hasattr(session, "switch_model"):
            return None
        try:
            await session.switch_model(profile.get("model"))
        except Exception as error:
            self.say(f"  Could not switch models in place ({error}); handing the conversation over instead.", DIM)
            return None
        self.sessions.pop(old_key)
        self.session_profiles.pop(old_key, None)
        self.sessions[key], self.session_profiles[key] = session, profile
        session.profile = dict(profile)
        self.say(
            Text(
                f"  {model_name(profile, alias)} continues the same conversation "
                f"(was {model_name(old_profile, old_key[0].split('@')[0])}).",
                style=DIM,
            )
        )
        return session

    def remember_exchange(self, session, text):
        """Record an exchange for the whole window, so every other mode and model can be given it."""
        tools = "; ".join(self.turn_tools[:30])
        answer = self.turn_text.strip() + (f"\n(Tools used: {tools})" if tools else "")
        self.exchanges.append(
            {"mode": self.role_of(session), "model": self.running_name(), "you": text, "answer": answer, "by": session}
        )
        session.donut_seen = len(self.exchanges)

    def running_name(self):
        if not self.running:
            return ""
        role, alias, profile = self.running
        return model_name(profile, alias)

    def context_for(self, session, limit=100_000):
        """What happened in this window that this session has not seen: other modes' and models' exchanges.

        A new session gets everything so far; one already in the conversation gets only what came after its
        last turn. Its own exchanges are never repeated, since the provider keeps those itself.
        """
        missed = [entry for entry in self.exchanges[getattr(session, "donut_seen", 0) :] if entry["by"] is not session]
        if not missed:
            return ""
        lines, size = [], 0
        for entry in reversed(missed):
            where = f"{entry['mode']} mode" + (f", {entry['model']}" if entry["model"] else "")
            line = f"[{where}]\nUser: {entry['you']}\nAssistant: {entry['answer']}"
            if size + len(line) > limit:
                lines.append("(earlier messages left out for length)")
                break
            lines.append(line)
            size += len(line)
        first = not getattr(session, "donut_seen", 0)
        lead = (
            "This conversation started before you joined it"
            if first
            else "Other modes and models worked in this Donut window since your last turn"
        )
        return (
            f"(Donut: {lead}. Here is what happened, oldest first. Treat it as shared context: you may be asked "
            "about it. The project on disk reflects the work already done.)\n<conversation>\n"
            + "\n\n".join(reversed(lines))
            + "\n</conversation>\n\n"
        )

    def has_ponytail(self, profile):
        """Whether Ponytail is installed and enabled for this login; checked once per login per Donut run."""
        key = usage_key(profile)
        if key not in self.ponytail_installed:
            self.ponytail_installed[key] = plugins.installed(profile, "ponytail", self.project)
        return self.ponytail_installed[key]

    def add_spent(self, session):
        """Add what a session's last turn cost to the amount on the input box. Only Claude reports a cost."""
        reported = (getattr(session, "usage", None) or {}).get("reported_cost_usd")
        if isinstance(reported, (int, float)):
            # Claude reports the session's running cost; a lower figure means a new provider process started over.
            previous = getattr(session, "spent_reported", 0.0)
            self.spent += reported - previous if reported >= previous else reported
            session.spent_reported = reported
            self.estimates.clear()  # The reported figure covers what was estimated.
        estimate = sum(self.estimates.values())
        try:
            # "~" marks a figure that includes an estimate for the turn in progress.
            self.query_one(Draft).border_subtitle = f"{'~' if estimate else ''}${self.spent + estimate:.2f} spent"
        except NoMatches:
            pass

    async def ask(self, text, prompt=None):
        """Send one turn to the active session, then book it on the conversation's Ponytail task row."""
        session = self.active
        started = time.monotonic()
        prompt = prompt if prompt is not None else text
        if self.shell_runs:
            prompt = background.shell_note(self.shell_runs) + prompt
            self.shell_runs = []
        prompt = self.context_for(session) + prompt
        waits = isinstance(session, (CodexSession, QwenSession))
        if waits and not getattr(session, "told_wait", False):
            # Claude Code wakes itself when a background shell ends; the others learn Donut's DONUT-WAIT line.
            prompt += background.WAIT_NOTE
            session.told_wait = True
        await session.query(prompt)
        self.add_spent(session)
        self.remember_exchange(session, text)
        if waits or getattr(session, "donut_waits", False):
            command = background.wait_request(self.turn_text)
            if command:
                await self.start_wait(session, command)
        task = getattr(session, "task", None)
        profile = self.session_profiles.get(next((k for k, v in self.sessions.items() if v is session), None), {})
        if task is None or not self.has_ponytail(profile):
            return
        task.turn(text, time.monotonic() - started, getattr(session, "usage", None))
        task.level = ponytail.level(profile, self.project) if profile.get("engine") == "claude" else task.level
        try:
            ponytail.record(task)
        except OSError as error:
            self.say(f"Ponytail ledger not written: {error}", DIM)
        self.render_band()

    focus_view = False  # /focus: only the transcript and the prompt.

    def render_band(self):
        """Ponytail's band for the current mode's conversation, when Ponytail is installed for its login."""
        widget = self.query_one("#band", Static)
        profile = self.profiles.get(self.role, {})
        if not profile or self.focus_view or self.config.get("ponytail_band") is False or not self.has_ponytail(profile):
            widget.display = False
            return
        session = self.sessions.get(self.session_key(self.selected[self.role], profile, self.purpose))
        task = getattr(session, "task", None)
        if task is None:
            task = ponytail.Task(self.project, profile.get("engine"), profile.get("model"))
        if profile.get("engine") == "claude":
            task.level = ponytail.level(profile, self.project)
        line = Text()
        for index, (label, value) in enumerate(task.band()):
            if index:
                line.append("  ·  ", style=DIM)
            line.append(label + " ", style=f"bold {ORANGE}" if index == 0 else DIM)
            tone = CORAL if value.endswith("failed") else TEAL if value.endswith("passed") else GRAY
            line.append(value, style=tone if label == "tests" else f"bold {GRAY}" if index == 0 else GRAY)
        widget.display = True
        widget.update(line)

    def on_text_area_changed(self, event):
        menu = self.query_one(Menu)
        text = event.text_area.text
        recalled, self.recalled = self.recalled, False
        event.text_area.set_class(text.startswith("!"), "-shell")
        if menu.kind == "pick":
            menu.filter(text)
        elif menu.kind == "approve":
            return
        elif text.startswith("/") and not any(c.isspace() for c in text) and not recalled:
            if not menu.is_open:
                menu.open("slash", "Commands", self.slash_rows())
            if not menu.filter(text):
                menu.close()
        elif menu.is_open:
            menu.close()

    def action_send(self):
        if len(self.screen_stack) > 1:
            return
        editor = self.query_one(Draft)
        text = editor.text.strip()
        if not text:
            return
        editor.clear()
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        self.history_at = None
        self.submit(text)

    def submit(self, text):
        """Run a message now, or, during a turn, queue it; commands that only show something run at once."""
        if not self.busy:
            self.cancelled = False  # Ctrl+C from here on cancels this message, even before its model is chosen.
            self.execute(text)
        elif is_instant(text):
            self.run_worker(self.instant(text), group="instant")
        else:
            # Like Claude Code: a message sent during a turn waits and runs as soon as the turn ends.
            self.queued.append(text)
            self.say(Text("  queued · " + text.splitlines()[0][:100], style=DIM))
            self.status("Thinking…")

    async def instant(self, text):
        """A display-only command during a turn: its result shows now and the turn carries on undisturbed."""
        try:
            await self.command(text)
        except Exception as error:
            self.say("Error: " + str(error), CORAL)

    def on_draft_submitted(self, event):
        self.action_send()

    def action_models(self):
        if not self.busy:
            self.execute("/model")

    def action_modes(self):
        """F3 cycles plan, work, review and solo."""
        if not self.busy and not self.simple:
            self.execute("/" + {"plan": "work", "work": "review", "review": "solo", "solo": "plan"}[self.mode])

    async def start_fresh(self):
        """/clear and /new: close every provider conversation and wipe the screen, as if Donut had just opened.

        Tickets, model choices and the mode are kept; nothing from the earlier conversation is carried over.
        """
        sessions, self.sessions = list(self.sessions.values()), {}
        self.threads, self.exchanges = {}, []
        self.forget_route()
        self.background_tasks = {}
        self.ponytail_installed = {}
        for session in sessions:
            await session.close()
        self.last_answer = ""
        self.query_one(RichLog).clear()
        self.welcome()
        self.say(Text("  New conversation. Existing tickets are kept.", style=GRAY))

    def watch_app_focus(self, focus):
        """Only the window in front blinks its cursor; the others hold it steady until they are active again."""
        try:
            self.query_one(Draft).cursor_blink = focus
        except NoMatches:
            pass

    def on_click(self, event):
        """Open a link under the mouse. Terminals also open these hyperlinks with Cmd- or Ctrl-click."""
        try:
            style = self.screen.get_style_at(event.screen_x, event.screen_y)
        except Exception:
            return
        if style.link:
            self.open_url(style.link)

    def action_clear(self):
        self.query_one(RichLog).clear()

    def copy_text(self, text):
        """Copy to the clipboard through the terminal (OSC 52) and this computer's own clipboard tool."""
        if not text:
            return False
        self.copy_to_clipboard(text)
        copy_native(text)
        return True

    def selected_text(self):
        """Text selected in the transcript or the prompt, if any."""
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        if text:
            return text
        try:
            return self.query_one(Draft).selected_text or None
        except NoMatches:
            return None

    def on_text_selected(self, event):
        """Copy a mouse selection as soon as it is made, like a terminal's copy on select."""
        text = self.selected_text()
        if text and self.copy_text(text):
            self.notify(f"Copied {len(text)} characters", timeout=2)

    async def action_cancel(self):
        selected = self.selected_text()
        if selected:
            # Ctrl+C with something selected copies it, as in any editor; with nothing selected it cancels.
            self.copy_text(selected)
            self.notify(f"Copied {len(selected)} characters", timeout=2)
            try:
                self.screen.clear_selection()
            except Exception:
                pass
            return
        menu = self.query_one(Menu)
        self.cancelled = True  # Seen by a step that runs before the turn itself, such as auto choosing a model.
        if self.picking and not self.picking.done():
            self.picking.set_result(None)
        elif menu.is_open:
            menu.close()
        typed = [item for item in self.queued if isinstance(item, str)]
        if typed:
            # Put queued messages back in the prompt so nothing typed is lost; background wake-ups stay queued.
            draft = self.query_one(Draft)
            draft.load_text("\n".join(typed + ([draft.text] if draft.text.strip() else [])))
            draft.move_cursor(draft.document.end)
            self.queued = [item for item in self.queued if not isinstance(item, str)]
        if self.active:
            await self.active.interrupt()
        if self.child and self.child.returncode is None:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(self.child.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL
                )
                await proc.wait()
            else:
                os.killpg(self.child.pid, signal.SIGTERM)

    async def action_quit(self):
        driver = getattr(self, "_driver", None)
        if driver is not None:
            driver.write("\x1b[23;0t")  # Put back the title the terminal had before Donut.
        await self.action_cancel()
        for session in self.sessions.values():
            await session.close()
        self.exit()

    @work(exclusive=True)
    async def execute(self, text):
        if isinstance(text, background.Wake):
            self.busy, self.running, self.started = True, None, time.monotonic()
            try:
                await self.wake_turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.say("Error: " + str(error), CORAL)
            finally:
                self.turn_finished()
            return
        if text.startswith("!"):
            self.busy, self.running, self.started = True, None, time.monotonic()
            self.status()
            try:
                await self.shell(text[1:].strip())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.say("Error: " + str(error), CORAL)
            finally:
                self.child = None
                self.turn_finished()
            return
        unset = not self.selected[self.role] and not (self.role == "solo" and self.auto)
        if unset and not text.startswith(("/help", "/exit", "/quit", "/permissions", "/auto", "/simple", "/complex")):
            self.say("Run donut setup to configure an account first.")
            return
        self.busy = True
        self.running = None
        self.activity = "working"
        self.started = time.monotonic()
        self.status()
        try:
            if text.startswith("/"):
                await self.command(text)
            elif self.mode == "solo":
                await self.solo(text)
            elif self.mode == "review":
                await self.review_chat(text)
            else:
                if self.mode == "work":
                    # Conversation goes to the planner (or the worker in /solo); /work only runs tickets.
                    self.set_mode("plan")
                    self.say("Switched to plan mode for this message.", DIM)
                await self.plan(text)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.say("Error: " + str(error), CORAL)
        finally:
            self.turn_finished()

    async def command(self, text):
        words = shlex.split(text)
        name, args = words[0], words[1:]
        if self.simple and name in COMPLEX_COMMANDS:
            self.say(f"  {name} is part of complex mode. /complex shows it.", DIM)
            return
        if name in NEEDS_MODEL and self.role == "solo" and self.auto and not self.profiles.get("solo"):
            self.say(f"  Send a message first: auto picks the model then, and {name} applies to it.", DIM)
            return
        if name in ("/exit", "/quit"):
            await self.action_quit()
        elif name in ("/simple", "/complex"):
            await self.set_interface(name[1:])
        elif name == "/harder":
            await self.harder()
        elif name == "/routing":
            await self.show_routing()
        elif name in ("/planner", "/worker", "/soloist"):
            role = {"/planner": "plan", "/worker": "work", "/soloist": "solo"}[name]
            await self.choose_model(role, args[0] if args else None)
        elif name == "/reviewer":
            await self.choose_reviewer(args[0] if args else None)
        elif name == "/solo":
            if not await self.ensure_role("solo"):
                return
            self.set_mode("solo")
            line = Text("  Solo mode", style=f"bold {TEAL}")
            line.append(f" · {self.solo_label()}", style=f"bold {ORANGE}")
            self.say(line)
            self.say(
                "  Messages go straight to the soloist, like Claude Code: no planner, tickets or review. "
                "/soloist changes its model; /plan goes back.",
                DIM,
            )
        elif name == "/review":
            if not await self.ensure_role("review"):
                return
            self.set_mode("review")
            line = Text("  Review mode", style=f"bold {ORANGE}")
            line.append(f" · {model_name(self.profiles['review'], self.selected['review'])}", style=f"bold {ORANGE}")
            self.say(line)
            self.say("  Ask the reviewer to check code or changes. It reads the project and never edits it.", DIM)
        elif name == "/plan":
            if not await self.ensure_role("plan"):
                return
            self.set_mode("plan")
            self.say("Planning mode. Describe what to plan.", ORANGE)
        elif name == "/work":
            if not await self.ensure_role("work"):
                return
            self.set_mode("work")
            if self.mode == "work":
                choice = (
                    " ".join(args)
                    if args
                    else await self.choose(
                        "Work tickets",
                        [
                            (label, value, description, "/work " + value)
                            for label, value, description in (
                                ("Next ticket", "next 1", "one ticket in a fresh context"),
                                ("Next 3 tickets", "next 3", "up to three, one fresh context each"),
                                ("All eligible tickets", "next all", "until the queue is empty or blocked"),
                                ("Next ticket + checkpoint", "next 1 checkpoint", "git commit after it"),
                                ("All tickets + checkpoints", "next all checkpoint", "git commit after each"),
                            )
                        ],
                        subtitle="or type /work next N for any number",
                    )
                )
                if choice:
                    await self.work_tickets(choice)
                # Back to planning, so the next message is a follow-up rather than refused.
                self.set_mode("plan")
        elif name == "/session":
            await self.choose_session(args[0] if args else None)
        elif name == "/project":
            # The path exactly as typed: shell-style splitting would eat a Windows path's backslashes.
            path = text.strip()[len(name) :].strip().strip("\"'")
            await self.open_project(path or None)
        elif name == "/resume":
            await self.resume_conversation(args[0] if args else None)
        elif name == "/model":
            if self.role == "review":
                await self.choose_reviewer()
            else:
                await self.choose_model(self.role)
        elif name == "/permissions":
            rows = [(label, mode, description) for mode, (label, description) in PERMISSION_MODES.items()]
            if args and args[0] in PERMISSION_MODES:
                chosen = args[0]
            else:
                current = PERMISSION_MODES[self.permission][0]
                chosen = await self.choose(f"Permission mode · now {current}", rows)
            if chosen:
                await self.set_permission(chosen)
        elif name == "/effort":
            await self.choose_effort(args[0] if args else None)
        elif name == "/focus":
            # Claude Code's /focus is unavailable in its headless mode, so Donut's Quiet level stands in for it.
            if args and args[0] not in ("on", "off"):
                self.say("  Use /focus [on|off].", DIM)
            else:
                self.focus_view = args[0] == "on" if args else not self.focus_view
                self.set_class(self.focus_view, "-focus")
                await self.set_verbosity("quiet" if self.focus_view else "normal")
        elif name == "/verbosity":
            if args and args[0] in VERBOSITY:
                chosen = args[0]
            else:
                rows = [(label, level, description) for level, (label, description) in VERBOSITY.items()]
                chosen = await self.choose(
                    f"What Donut shows while models work · now {VERBOSITY[self.verbosity][0]}",
                    rows,
                    subtitle="Display only: models think, work and answer exactly the same at every level",
                )
            if chosen:
                await self.set_verbosity(chosen)
        elif name == "/auto":
            if self.permission == "auto":
                fallback = self.previous_permission if self.previous_permission != "auto" else "ask"
                await self.set_permission(fallback)
            else:
                await self.set_permission("auto")
        elif name == "/usage":
            await self.usage()
        elif name in ("/clear", "/new"):
            await self.start_fresh()
        elif name == "/ponytail" and args[:1] == ["band"]:
            # Ponytail's band is a Claude Code UI module; in Donut the same switch shows or hides Donut's band.
            if args[1:2] == ["import"]:
                self.say("  /ponytail band import reads Claude Code's own session files; run it there (/native).", DIM)
                return
            shown = {"on": True, "off": False}.get(
                args[1] if len(args) > 1 else "", self.config.get("ponytail_band") is False
            )
            try:
                config = load()
                config["ponytail_band"] = shown
                save(config)
            except (ValueError, OSError) as error:
                self.say(f"Band setting applies to this session only: {error}", DIM)
            self.config["ponytail_band"] = shown
            self.say(f"  Ponytail band {'shown' if shown else 'hidden'}. /ponytail band switches it back.", DIM)
            self.status()
        elif name == "/copy":
            self.copy_text(self.last_answer)
            self.notify("Copied last response (terminal clipboard support required).")
        elif name == "/help" and self.simple:
            self.say(
                "/harder (a stronger model for this conversation) · /routing · /model · /resume · /clear (or /new) · /permissions [ask|accept_edits|auto|full] · /auto · /effort · /verbosity [quiet|normal|verbose|full] · /usage · /plugin · /mcp · /context · /copy · /project [PATH] · /rename · /login · /native · /complex (show every feature) · /exit\nDonut rates your first message in a conversation and picks the model for it; the model stays until /clear. !COMMAND runs a shell command in the project. Shift+Tab cycles Ask, Accept edits and Auto. Messages sent during a turn are queued."
            )
        elif name == "/help":
            self.say(
                "/plan · /work [next COUNT] [checkpoint] · /review · /solo · /resume · /session [NAME|new] · /project [PATH] · /planner · /worker · /reviewer [ALIAS|planner|off] · /soloist · /model · /permissions [ask|accept_edits|auto|full] · /auto · /verbosity [quiet|normal|verbose|full] · /usage · /plugin · /mcp · /context · /clear (or /new) · /ponytail band · /copy · /login · /native · /harder · /routing · /simple · /exit\n!COMMAND runs a shell command in the project; the model sees its output with your next message. Shift+Tab cycles Ask, Accept edits and Auto. Messages sent during a turn are queued. Installed plugin and skill commands (such as Ponytail's) are listed under / and forwarded to the selected session in its own syntax. Use /native for built-ins requiring the provider’s own UI."
            )
        elif name in ("/plugin", "/mcp", "/context"):
            session = await self.session(self.selected[self.role], self.profiles[self.role], self.purpose)
            provider = self.selected[self.role] or "provider"
            model = self.profiles[self.role].get("model") or ""
            if isinstance(session, ClaudeSession):
                if name == "/plugin":
                    data = session.info.get("commands", [])
                elif name == "/mcp":
                    await self.claude_mcp(session, provider)
                    return
                else:
                    for line in claude_context(await session.client.get_context_usage()):
                        self.say(line)
                    return
            elif isinstance(session, CodexSession):
                if name == "/mcp":
                    for line in render_mcp(mcp_servers(await session.request("mcpServerStatus/list", {})), provider):
                        self.say(line)
                    return
                if name == "/context":
                    for line in codex_context(getattr(session, "usage", None), model):
                        self.say(line)
                    return
                data = await session.request("plugin/list", {})
            else:
                self.say("Use /native for Qwen plugins, MCP and context controls.")
                return
            if name == "/plugin":
                if isinstance(session, ClaudeSession):
                    rows = [
                        (
                            item.get("name", "?") + " · " + (item.get("description") or "")[:100],
                            "/" + item.get("name", ""),
                        )
                        for item in data
                        if isinstance(item, dict)
                    ]
                    selected = await self.choose("Claude plugin commands · /native for management", rows)
                    if selected:
                        self.query_one(TextArea).load_text(selected + " ")
                else:
                    rows = []
                    for market in data.get("marketplaces", []):
                        for plugin in market.get("plugins", []):
                            state = "installed" if plugin.get("installed") else "not installed"
                            description = (plugin.get("description") or "")[:70]
                            rows.append((plugin.get("name", "?"), plugin, f"{state} · {description}".strip(" ·")))
                    selected = await self.choose("Codex plugins · /native for management", rows)
                    if selected:
                        line = Text("  " + selected.get("name", "?"), style=f"bold {ORANGE}")
                        line.append("  installed" if selected.get("installed") else "  not installed", style=GRAY)
                        if selected.get("description"):
                            line.append("  " + selected["description"], style=DIM)
                        self.say(line)
                        self.say("  Install or remove plugins with /native.", DIM)
        elif name == "/login":
            await self.login()
        elif name == "/native":
            await self.native()
        elif name == "/rename" and self.profiles.get(self.role, {}).get("engine") != "claude":
            # Only Claude Code has its own /rename; elsewhere Donut renames the session's tickets folder.
            if not args:
                self.say("  Use /rename NAME. The tickets folder becomes .donut/NAME/.", DIM)
                return
            self.rename_session(" ".join(args))
        else:
            self.active = await self.session(self.selected[self.role], self.profiles[self.role], self.purpose)
            self.start_turn(self.role)
            self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
            await self.ask(text, plugins.translate(text, plugins.commands(self.profiles[self.role], self.project)))
            self.answer()
            if name == "/rename":
                # Claude Code renamed its session (to the name given, or one it made up): the folder follows.
                renamed = re.search(r"Session renamed to:\s*(.+)", self.turn_text)
                if renamed:
                    self.rename_session(renamed[1].strip().strip("`*\"'"))

    def track_task(self, task):
        """Background shells and agents a model left running: shown in the status line until they end."""
        if not isinstance(task, dict) or not task.get("id"):
            return
        if task.get("state") == "running":
            self.background_tasks[task["id"]] = (task.get("kind", "shell"), task.get("description", ""))
        elif task["id"] in self.background_tasks:
            kind, description = self.background_tasks.pop(task["id"])
            ok = task.get("state") == "completed"
            line = Text(f"  {'✓' if ok else '✗'} Background {kind} {task.get('state')}", style=TEAL if ok else CORAL)
            if description:
                line.append("  " + _clip(description, 100), style=DIM)
            self.say(line)
        self.status()

    def background_summary(self):
        """ "2 shells running · 1 agent running" for the status line, or ""."""
        counts = {}
        for kind, _ in self.background_tasks.values():
            counts[kind] = counts.get(kind, 0) + 1
        parts = [f"{n} {kind}{'' if n == 1 else 's'} running" for kind, n in sorted(counts.items(), reverse=True)]
        if self.waiting:
            parts.append(f"{len(self.waiting)} waiting")
        return " · ".join(parts)

    def show_pending(self, pending):
        """The part of the answer still being written, above the prompt; the transcript stays at its end."""
        log = self.query_one(RichLog)
        response = self.query_one("#response", Static)
        response.update(markdown(pending) if pending else "")

        def settle():
            response.scroll_end(animate=False)  # A long paragraph shows its newest lines.
            if self.follow:
                log.scroll_end(animate=False)  # The reply area grew, so the transcript's last lines moved up.

        self.call_after_refresh(settle)

    def flush_thinking(self):
        """Write buffered thinking (Full verbosity) as one dim block before what comes next."""
        if not self.thinking_text.strip():
            self.thinking_text = ""
            return
        text = " ".join(self.thinking_text.split())
        self.thinking_text = ""
        self.commit_paragraphs(final=True)
        self.say(Text("  ✻ " + text, style=f"italic {DIM}"))

    def show_tool_result(self, result):
        output = str(result.get("output") or "").rstrip()
        rows = output.splitlines()
        if self.verbosity == "full" and rows:
            for index, row in enumerate(rows[:20]):
                self.say(Text("    ⎿ " if index == 0 else "      ", style=DIM) + Text(_clip(row), style=GRAY))
            if len(rows) > 20:
                self.say(Text(f"      … {len(rows) - 20} more lines", style=DIM))
            return
        mark = Text("    ⎿ ", style=CORAL if result.get("error") else DIM)
        if result.get("error"):
            mark.append(_clip(rows[0] if rows else "failed", 120), style=CORAL)
        else:
            mark.append(f"{len(rows)} line{'' if len(rows) == 1 else 's'}" if rows else "done", style=DIM)
        self.say(mark)

    async def set_verbosity(self, level):
        self.verbosity = level
        try:
            config = load()
            config["verbosity"] = level
            save(config)
            self.config["verbosity"] = level
        except (ValueError, OSError) as error:
            self.say(f"Verbosity applies to this session only: {error}", DIM)
        label, description = VERBOSITY[level]
        self.say(Text(f"  Verbosity · {label}", style=f"bold {ORANGE}") + Text(f"  {description}", style=DIM))
        self.say("  This changes only what Donut shows; the models still think and answer in full.", DIM)
        self.status()

    def commit_paragraphs(self, final=False):
        """Move finished paragraphs of the streaming answer into the transcript, like Claude Code.

        Only the paragraph still being written stays above the prompt, so a long turn of progress updates scrolls
        by in the transcript instead of piling up in one block. A paragraph ends at a blank line outside a code
        block; final=True also writes the rest.
        """
        pending = self.turn_text[self.shown :]
        cut = len(pending) if final else -1
        if not final:
            search = len(pending)
            while True:
                cut = pending.rfind("\n\n", 0, search)
                if cut == -1 or (self.turn_text[: self.shown] + pending[:cut]).count("```") % 2 == 0:
                    break
                search = cut
        if cut <= 0:
            return
        chunk = pending[:cut].strip()
        first = not self.turn_text[: self.shown].strip()
        self.shown += cut
        if chunk:
            if first or self.after_tools:
                self.say(Text(""))  # A blank line after the request, or after a run of tool calls.
            self.after_tools = False
            self.say(markdown(chunk))
            self.say(Text(""))

    def answer(self):
        self.flush_thinking()
        if self.verbosity == "quiet" and self.shown < self.message_start:
            self.shown = self.message_start  # Quiet: earlier messages of the turn are not shown, only the last.
        self.commit_paragraphs(final=True)
        self.query_one("#response", Static).update("")
        if self.follow:
            log = self.query_one(RichLog)
            self.call_after_refresh(lambda: log.scroll_end(animate=False))
        self.last_answer = self.turn_text

    async def solo(self, text):
        """Send the message to the soloist as is, in its own conversation, the way Claude Code would."""
        self.say(Text("> " + text, style=f"bold {TEAL}"))
        if self.auto and self.route is None and not await self.choose_route(text):
            return
        self.active = await self.session(self.selected["solo"], self.profiles["solo"], "solo")
        self.start_turn("solo")
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        await self.ask(text)
        self.answer()
        self.log_turn("solo", "solo", text)

    async def review_chat(self, text):
        """Ask the reviewer to check something, in a read-only conversation of its own."""
        self.active = await self.session(self.selected["review"], self.profiles["review"], "review")
        self.start_turn("review", "", "reviewing")
        self.say(Text("> " + text, style=f"bold {ORANGE}"))
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        await self.ask(text, REVIEW_MODE_PROMPT + text)
        self.answer()
        self.log_turn("review", "review", text)

    def log_turn(self, mode, role, text):
        """Save this exchange so /resume can reopen the conversation in the same mode and model."""
        session = self.active
        if session is None:
            return
        try:
            session.conversation_id = conversations.record_turn(
                getattr(session, "conversation_id", None),
                self.project,
                mode,
                role,
                self.selected.get(role),
                self.profiles.get(role, {}).get("model"),
                getattr(session, "provider_id", None),
                text,
                self.turn_text,
                session=self.session_name,
            )
        except OSError as error:
            self.say(f"Conversation not saved for /resume: {error}", DIM)

    def role_of(self, session):
        for key, value in self.sessions.items():
            if value is session:
                purpose = key[0].rpartition("@")[2] if "@" in key[0] else ""
                return purpose if purpose in ("solo", "review") else "plan"
        return self.role

    def wake_title(self, session):
        role = self.role_of(session)
        return f"{model_name(self.profiles.get(role, {}), self.selected.get(role))} ({ROLE_TITLES.get(role, role)})"

    def begin_wake(self, session):
        """Claude Code started a turn on its own (a background shell it left running finished): show it."""
        role = self.role_of(session)
        self.busy, self.active = True, session
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        self.say(Text(f"  ⏰ {self.wake_title(session)} · woken by a background task", style=f"bold {TEAL}"))
        self.start_turn(role, "", "continuing")

    def finish_wake(self, session):
        role = self.role_of(session)
        self.answer()
        self.log_turn(role if role in ("solo", "review") else "plan", role, "(woken by a background task)")
        self.turn_finished()

    def turn_finished(self):
        """Back to Ready after a turn, then run the next queued message or wake-up."""
        self.active = None
        self.busy = False
        self.running = None
        if self.AUTO_REFRESH_QUOTAS:
            self.refresh_quotas_soon()
        try:
            self.status("Ready")
            self.query_one(Draft).focus()
        except NoMatches:
            return  # The app is shutting down.
        if self.queued:
            self.cancelled = False
            self.call_after_refresh(self.execute, self.queued.pop(0))

    async def start_wait(self, session, command):
        """A model asked Donut to wake it when command exits. Ask first unless permissions are on Full access."""
        if self.permission != "full":
            decision = await self.approve("Bash", {"command": command, "description": "wait in the background"})
            if decision == "no":
                self.say("  Not waiting; the model was not told. Ask it to continue when you are ready.", DIM)
                return
        role = self.role_of(session)
        task = asyncio.create_task(self.wait_then_wake(session, role, command))
        self.waiting[task] = command
        task.add_done_callback(lambda done: (self.waiting.pop(done, None), self.status()))
        self.say(Text(f"  ⏳ Waiting on `{command}` · {self.wake_title(session)} continues when it exits", style=TEAL))
        self.status()

    async def wait_then_wake(self, session, role, command):
        try:
            code, output = await background.wait_for(command, self.project)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code, output = -1, f"Could not run it: {error}"
        wake = background.Wake(session, role, command, code, output, time.time())
        if self.busy:
            self.queued.append(wake)
            self.status()
        else:
            self.execute(wake)

    async def wake_turn(self, wake):
        """Give the model the result of the command it waited on, in its own conversation."""
        state = "timed out" if wake.code is None else f"exit {wake.code}"
        if wake.session not in self.sessions.values():
            self.say(Text(f"  ⏰ `{wake.command}` finished ({state}), but its conversation was closed.", style=TEAL))
            self.say(Text(background.tail(wake.output, 1500), style=DIM))
            return
        self.active = wake.session
        self.say(
            Text(
                f"  ⏰ `{wake.command}` finished ({state}) · waking {self.wake_title(wake.session)}",
                style=f"bold {TEAL}",
            )
        )
        self.start_turn(wake.role, "", "continuing")
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        await self.ask(f"(background) {wake.command}", wake.prompt)
        self.answer()
        self.log_turn(
            wake.role if wake.role in ("solo", "review") else "plan", wake.role, f"(background) {wake.command}"
        )

    def complete_shell(self, editor):
        """Tab in !mode: finish the last word, or extend it to the common prefix and list what is left."""
        word, found = shell_candidates(editor.text, self.project)
        if not found:
            return
        prefix = os.path.commonprefix(found)
        if len(prefix) > len(word):
            editor.insert(prefix[len(word):])
        elif len(found) > 1:
            stem = editor.text[: len(editor.text) - len(word)]
            rows = [(name, stem + name + ("" if name.endswith("/") else " ")) for name in found[:200]]
            self.query_one(Menu).open("shell", "Completions", rows)
        if len(found) == 1 and not prefix.endswith("/"):
            editor.insert(" ")

    async def shell(self, command):
        """!command: run it in the project and show its output; the model gets it with the next message."""
        if not command:
            self.say("  Type ! then a shell command, for example: !git status", DIM)
            return
        self.say(Text("! " + command, style=f"bold {TEAL}"))
        self.activity, self.detail = "running", command
        self.child = await background.start(command, self.project)
        lines = []
        async for raw in self.child.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n")).split("\r")[-1]
            lines.append(line)
            if len(lines) <= 2000:
                self.say(Text("  " + line, style=GRAY))
        code = await self.child.wait()
        self.child = None
        if len(lines) > 2000:
            self.say(Text(f"  … {len(lines) - 2000} more lines (the model gets the end of the output)", style=DIM))
        self.say(Text(f"  exit {code}", style=TEAL if code == 0 else CORAL))
        self.shell_runs.append((command, code, "\n".join(lines)))

    def session_label(self):
        return self.session_name or "swim_lanes"

    def window_title(self):
        """What the terminal's window and tab show, so windows are easy to tell apart and switch between."""
        return f"{self.session_label()} · {Path(self.project).name or self.project} · Donut"

    def show_title(self):
        title = self.window_title()
        if title == getattr(self, "shown_title", None):
            return
        self.shown_title = title
        driver = getattr(self, "_driver", None)
        if driver is not None:
            # OSC 0 sets the window and tab title; control characters are left out so the title cannot escape.
            driver.write("\x1b]0;" + "".join(c for c in title if c.isprintable()) + "\x07")

    def rename_session(self, name):
        """Name this window's session: its tickets folder becomes .donut/<name>/ (spaces and the like -> -)."""
        old = self.session_name
        try:
            new = workspace.rename(self.project, old, name)
        except (ValueError, OSError) as error:
            self.say(f"  Tickets folder not renamed: {error}", CORAL)
            return
        self.session_name = new
        try:
            conversations.rename_session(self.project, old, new, " ".join(name.split())[:80])
        except OSError:
            pass
        self.say(Text(f"  Tickets folder · {workspace.relative(self.project, self.lanes)}", style=f"bold {ORANGE}"))
        if old is None:
            self.say("  The older swim_lanes/ folder stays as it was.", DIM)
        self.status()

    async def choose_session(self, name=None):
        """Point this window at another tickets folder: an existing session, a new one, or older swim_lanes/."""
        if name is None:
            rows = [("New session", "", "start an empty tickets folder")]
            for session in workspace.sessions(self.project):
                counts = workspace.count(self.project, session)
                summary = " · ".join(
                    f"{counts[lane]} {lane.replace('_', ' ')}"
                    for lane in ("to_do", "in_progress", "done", "blocked")
                    if counts[lane]
                )
                current = "current · " if session == self.session_name else ""
                rows.append((session, session, current + (summary or "no tickets yet")))
            if (workspace.lanes_dir(self.project) / "to_do").is_dir():
                rows.append(("swim_lanes/", "-", "this project's tickets from before sessions"))
            name = await self.choose(f"Tickets session · now {workspace.relative(self.project, self.lanes)}", rows)
            if name is None:
                return
        if name in ("", "new"):
            name = workspace.new_name(self.project, taken={self.session_name})
        elif name in ("-", "swim_lanes", "swim_lanes/"):
            name = None
        elif not workspace.valid(name):
            raise ValueError(f"Invalid session name {name!r}: use letters, numbers, '.', '_' or '-'")
        self.session_name = name
        await asyncio.to_thread(workspace.create, self.project, name)
        folder = workspace.relative(self.project, self.lanes)
        self.say(Text(f"  Tickets session · {folder}", style=f"bold {ORANGE}"))
        self.say("  /plan writes tickets here and /work runs them from here.", DIM)
        self.status()

    async def open_project(self, path=None):
        """Open another project in a new terminal window, picked from the projects Donut has opened."""
        if path is None:
            current = str(Path(self.project).resolve())
            rows = []
            for project in conversations.recent_projects():
                if project == current:
                    continue
                sessions = workspace.sessions(project)
                detail = (
                    f"{len(sessions)} session{'' if len(sessions) == 1 else 's'}" if sessions else "no sessions yet"
                )
                rows.append((Path(project).name or project, project, f"{project} · {detail}"))
            if not rows:
                self.say("  No other projects yet. Use /project PATH to open one.", DIM)
                return
            path = await self.choose("Open a project in a new window", rows)
            if not path:
                return
        project = Path(path).expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"No such folder: {project}")
        where = await asyncio.to_thread(open_window, project)
        if where:
            self.say(Text(f"  Opened {project.name or project} in {where}.", style=f"bold {ORANGE}"))
        else:
            self.say("  No terminal app found to open a window with. Run this in a new terminal:", CORAL)
            self.say("  " + shlex.join(window_command(project)), GRAY)

    async def resume_conversation(self, record_id=None):
        """Reopen an earlier conversation: switch to its mode and model, then continue the provider's session."""
        records = conversations.for_project(self.project)
        if self.simple:
            records = [record for record in records if record.get("mode") == "solo"]
        if not records:
            self.say("No earlier conversations in this project yet.", DIM)
            return
        if record_id is None:
            rows = [
                (
                    (record.get("name") or record.get("title", "(untitled)"))[:56],
                    record["id"],
                    (f"{record.get('title', '')[:40]} · " if record.get("name") else "")
                    + f"{record.get('mode', '?')} · {record.get('model') or record.get('alias')} · "
                    f"{conversations.ago(record.get('updated'))} · {record.get('turns', 0)} turn"
                    f"{'' if record.get('turns') == 1 else 's'}",
                )
                for record in records[:40]
            ]
            record_id = await self.choose("Resume a conversation", rows)
            if not record_id:
                return
        record = next((r for r in records if r["id"] == record_id), None)
        if record is None:
            raise ValueError("That conversation is not in this project's history.")
        aliases = self.config.get("aliases", {})
        alias, role, mode = record.get("alias"), record.get("role"), record.get("mode")
        if alias not in aliases:
            raise ValueError(f"The account {alias} used by that conversation is no longer configured.")
        if mode not in MODE_PLACEHOLDERS or role not in ROLE_TITLES:
            raise ValueError("That conversation was saved in a mode this version cannot resume.")
        profile = dict(aliases[alias])
        if record.get("model"):
            profile["model"] = record["model"]
        # Match the conversation: its role's model, then its mode.
        self.selected[role], self.profiles[role] = alias, copy.deepcopy(profile)
        if role == "review":
            self.reviewer = "chosen"
        if role == "solo" and self.auto:
            # The resumed conversation keeps the model it had; auto stays on for the next new conversation.
            steps = await self.routing_steps()
            self.route = {"index": routing.position(steps, alias, profile), "score": None, "harder": 0}
        else:
            try:
                remember_role(role, alias, profile.get("model"))
            except (ValueError, OSError):
                pass
        self.sync_reviewer()
        self.set_mode(mode)
        if record.get("session") and workspace.valid(record["session"]):
            self.session_name = record["session"]
        purpose = mode if mode in ("solo", "review") else None
        self.status("resuming")
        resumed = await self.session(
            alias, profile, purpose, resume=record.get("provider_id"), conversation=record["id"]
        )
        # The saved recap joins the window's history, so other modes and models can be given it too.
        messages = record.get("messages", [])
        for you, model in zip(messages[::2], messages[1::2]):
            self.exchanges.append(
                {
                    "mode": mode,
                    "model": record.get("model", ""),
                    "you": you.get("text", ""),
                    "answer": model.get("text", ""),
                    "by": resumed,
                }
            )
        resumed.donut_seen = len(self.exchanges)
        line = Text("  Resumed", style=f"bold {ORANGE}")
        line.append(f" · {mode.upper()} · {model_name(profile, alias)}", style=f"bold {TEAL}")
        line.append(f" · {record.get('name') or record.get('title', '')}", style=GRAY)
        line.append(f" · tickets {workspace.relative(self.project, self.lanes)}", style=DIM)
        self.say(line)
        if not record.get("provider_id"):
            self.say("  The provider kept no session for it, so this continues in a fresh one.", DIM)
        for message in record.get("messages", [])[-6:]:
            text = " ".join(message.get("text", "").split())
            if message.get("from") == "you":
                self.say(Text("  > " + text[:200], style=ORANGE))
            else:
                self.say(Text("    " + text[:300] + ("…" if len(text) > 300 else ""), style=DIM))
        self.say("  Continue where you left off.", DIM)

    async def ensure_role(self, role):
        """Entering a mode asks for its model only when none is set. Returns whether one is set now."""
        if self.profiles.get(role) or (role == "solo" and self.auto):
            return True
        if role == "review":
            await self.choose_reviewer(allow_off=False)
        else:
            await self.choose_model(role)
        if self.profiles.get(role):
            return True
        self.say(f"No {ROLE_TITLES[role]} chosen; pick one with {ROLE_COMMANDS[role]}.", DIM)
        return False

    async def plan(self, text):
        # Install outside the display stream; existing project instructions are preserved.
        await asyncio.to_thread(install, self.project, Path.home(), quiet=True, session=self.session_name)
        before = ticket_snapshot(self.project, self.lanes)
        folder = workspace.relative(self.project, self.lanes)
        self.active = await self.session(self.selected["plan"], self.profiles["plan"])
        self.start_turn("plan")
        prompt = (
            "Read skills/donut-planner/SKILL.md. Plan only: research and write scoped tickets; do not implement or commit. "
            f"This session's tickets live in {folder}: write new tickets in {folder}to_do/ and leave every other "
            "tickets folder alone. Respect on_hold and blocked lanes and reserve their identities. Give a brief overview "
            "when finished; Donut lists created tickets. User request:\n" + text
        )
        self.say(Text("> " + text, style=f"bold {ORANGE}"))
        self.turn_text, self.shown, self.message_start, self.turn_tools = "", 0, 0, []
        await self.ask(text, prompt)
        self.answer()
        self.log_turn("plan", "plan", text)
        created = [path for key, path in ticket_snapshot(self.project, self.lanes).items() if key not in before]
        self.say("Tickets created: " + str(len(created)), CORAL)
        for path in sorted(created):
            self.say("  " + path.relative_to(self.project).as_posix(), GRAY)
        self.say("PLANNING COMPLETE · /work next 1", f"bold {ORANGE}")

    async def effort_options(self, role):
        """(what the provider calls it, [(level, description)], default level) for the role's model."""
        profile, alias = self.profiles.get(role, {}), self.selected.get(role)
        engine = profile.get("engine")
        if engine == "claude":
            session = await self.session(alias, profile, "review" if role == "review" else None, track=False)
            return effort.claude_options(getattr(session, "info", {}), profile.get("model"))
        if engine == "codex":
            session = await self.session(alias, profile, track=False)
            return effort.codex_options(await session.model_details(), profile.get("model"))
        return "Effort", [], None

    async def choose_effort(self, level=None):
        """/effort [LEVEL]: how hard the current mode's model thinks, in its provider's own terms."""
        role = self.role
        profile, alias = self.profiles.get(role, {}), self.selected.get(role)
        if not profile:
            self.say(f"No {ROLE_TITLES[role]} chosen; pick one with {ROLE_COMMANDS[role]}.", DIM)
            return
        name = model_name(profile, alias)
        self.status("checking effort levels")
        term, levels, default = await self.effort_options(role)
        if not levels:
            self.say(f"  {name} has no effort setting Donut can change.", DIM)
            return
        current = profile.get("effort") or default
        if level is None:
            rows = [
                (value, value, (description + (" · now" if value == current else "")).strip(" ·"))
                for value, description in levels
            ]
            level = await self.choose(f"{term} for {name} · now {current or 'default'}", rows)
            if level is None:
                return
        if level not in {value for value, _ in levels}:
            raise ValueError(f"{name} takes {term.lower()} " + ", ".join(value for value, _ in levels))
        self.profiles[role] = dict(profile, effort=level)
        session = self.sessions.get(self.session_key(alias, profile, self.purpose))
        if session is not None and hasattr(session, "set_effort"):
            await session.set_effort(level)
            self.session_profiles[self.session_key(alias, profile, self.purpose)] = self.profiles[role]
        try:
            remember_role(role, alias, profile.get("model"), level)
        except (ValueError, OSError) as error:
            self.say(f"Effort applies to this session only: {error}", DIM)
        if role == "plan":
            self.sync_reviewer()
        elif role == "review":
            self.reviewer = "chosen"  # Saved with its own effort, the reviewer no longer follows the planner.
        line = Text(f"  {term} · {level}", style=f"bold {ORANGE}")
        line.append(f"  {name}, {ROLE_TITLES[role]}", style=DIM)
        self.say(line)
        self.status()

    async def configured_mcp(self, profile):
        """What `claude mcp list` reports for this login: every server it is set up with, claude.ai connectors
        included, whether or not the running session loaded them."""
        env = dict(os.environ)
        if profile.get("claude_config_dir"):
            env["CLAUDE_CONFIG_DIR"] = str(Path(profile["claude_config_dir"]).expanduser())
        try:
            process = await asyncio.create_subprocess_exec(
                shutil.which("claude") or "claude",
                "mcp",
                "list",
                cwd=self.project,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(process.communicate(), 90)
        except (OSError, asyncio.TimeoutError):
            return []
        return parse_mcp_list(output.decode("utf-8", errors="replace"))

    async def claude_mcp(self, session, provider):
        """/mcp for Claude: the session's servers, and any the login has that the session started without.

        A running Claude session never picks up a server connected after it started, such as a claude.ai
        connector added since. When one is missing, Donut reopens the session on the same conversation so the
        new process loads it.
        """
        self.status("checking MCP servers")
        live = mcp_servers(await session.client.get_mcp_status())
        key = next((k for k, v in self.sessions.items() if v is session), None)
        configured = await self.configured_mcp(self.session_profiles.get(key, self.profiles.get(self.role, {})))
        loaded = {server["name"] for server in live}
        missing = [(name, status) for name, status in configured if name not in loaded]
        ready = [name for name, status in missing if is_connected(status)]
        if ready and self.active is None:
            self.say(
                Text(
                    f"  {', '.join(ready)} connected after this session started; reopening it on the same "
                    "conversation to load them.",
                    style=DIM,
                )
            )
            session = await self.reopen(session)
            live = mcp_servers(await session.client.get_mcp_status())
            loaded = {server["name"] for server in live}
            missing = [(name, status) for name, status in configured if name not in loaded]
        for line in render_mcp(live, provider):
            self.say(line)
        if missing:
            self.say(Text("  Set up for this login but not in this session:", style=GRAY))
            for name, status in missing:
                self.say(Text(f"    ○ {name}  {status}", style=DIM))
            if any(is_connected(status) for _, status in missing):
                self.say(Text("  Run /mcp again when the current turn ends to load them.", style=DIM))

    async def reopen(self, session):
        """Restart a Claude session's process on the same conversation, keeping Donut's own bookkeeping."""
        key = next(k for k, v in self.sessions.items() if v is session)
        alias_part, _ = key
        alias, _, purpose = alias_part.partition("@")
        profile = self.session_profiles.get(key, {})
        fresh = await self.session(
            alias,
            profile,
            purpose or None,
            resume=getattr(session, "provider_id", None),
            conversation=getattr(session, "conversation_id", None) or "",
        )
        fresh.conversation_id = getattr(session, "conversation_id", None)  # The same /resume record.
        fresh.donut_seen = getattr(session, "donut_seen", 0)
        fresh.task = getattr(session, "task", None) or getattr(fresh, "task", None)
        for entry in self.exchanges:
            if entry["by"] is session:
                entry["by"] = fresh  # Its own turns stay its own: they are in the resumed provider session.
        return fresh

    async def choose_reviewer(self, choice=None, allow_off=True):
        """Pick who reviews each finished ticket: the planner (default), any account and model, or off.

        The choice is saved, and it never changes the planner (nor does /model change a chosen reviewer).
        """
        aliases = self.config.get("aliases", {})
        if choice is None:
            planner = model_name(self.profiles.get("plan", {}), self.selected.get("plan"))
            rows = [("Same as planner (default)", "planner", f"follows the planner · now {planner}")]
            rows += await self.model_rows()
            if allow_off:
                rows.append(("Off", "off", "no review; the worker's result stands"))
            choice = await self.choose("Reviewer · checks each ticket and answers in review mode", rows)
            if not choice:
                return
        elif choice not in ("planner", "off"):
            if choice not in aliases:
                raise ValueError("Use /reviewer planner, /reviewer off, or /reviewer ALIAS with a configured alias.")
            choice = (choice, dict(aliases[choice]))
        try:
            if choice in ("planner", "off"):
                self.reviewer = choice
                forget_role("review", reviewer=choice)
            else:
                self.reviewer = "chosen"
                self.selected["review"], self.profiles["review"] = choice[0], copy.deepcopy(choice[1])
                remember_role("review", choice[0], choice[1].get("model"))
        except (ValueError, OSError) as error:
            self.say(f"Reviewer applies to this session only: {error}", DIM)
        self.sync_reviewer()
        if self.reviewer == "off":
            self.say("Review off. Finished tickets are not reviewed.", ORANGE)
        else:
            who = model_name(self.profiles["review"], self.selected["review"])
            self.say(Text(f"  Reviewer: {who}", style=f"bold {ORANGE}"))
            self.say("  It checks each finished ticket in a fresh context and sends issues back to the worker.", DIM)
        self.status()

    async def choose_model(self, role, alias=None):
        """Pick the account and model for one role (planner, worker or soloist). Only that role changes."""
        aliases = self.config.get("aliases", {})
        automatic = (routing.AUTO, None)
        if alias is not None:
            if role == "solo" and alias == routing.AUTO and alias not in aliases:
                selected = automatic
            elif alias not in aliases:
                raise ValueError(
                    f"Use {ROLE_COMMANDS[role]} ALIAS with a configured alias, or {ROLE_COMMANDS[role]} alone to pick."
                )
            else:
                selected = (alias, dict(aliases[alias]))
        else:
            rows = await self.model_rows()
            if role == "solo" and rows:
                rows.append(("auto  picks a model for each conversation from how hard its request is", automatic))
            title = "Model" if self.simple else "Account and model for the " + ROLE_TITLES[role]
            selected = await self.choose(title, rows) if rows else None
        if not selected:
            return
        if selected == automatic:
            self.set_auto()
            return
        if role == "solo":
            self.auto, self.route = False, None
        self.selected[role], self.profiles[role] = selected[0], copy.deepcopy(selected[1])
        try:
            remember_role(role, selected[0], selected[1].get("model"))
        except (ValueError, OSError) as error:
            self.say(f"Model choice applies to this session only: {error}", DIM)
        line = Text(f"  {ROLE_TITLES[role].capitalize()}: ", style=GRAY)
        line.append(model_name(self.profiles[role], self.selected[role]), style=f"bold {ORANGE}")
        self.say(line)
        if role == "plan" and self.reviewer == "planner":
            self.say("  The reviewer follows the planner; choose another with /reviewer.", DIM)
        self.sync_reviewer()
        self.status()

    async def model_rows(self, engines=None):
        """(label, (alias, profile)) for every model each configured account offers (of these engines only)."""
        rows = []
        for alias, profile in self.config["aliases"].items():
            if engines and profile.get("engine") not in engines:
                continue
            self.status("discovering models for " + alias)
            try:
                if profile["engine"] in ("claude", "codex"):
                    session = await self.session(alias, profile, track=False)
                    models = await session.models()
                elif profile["engine"] == "ollama":
                    proc = await asyncio.create_subprocess_exec("ollama", "list", stdout=asyncio.subprocess.PIPE)
                    output, _ = await proc.communicate()
                    if proc.returncode:
                        raise RuntimeError("Ollama model discovery failed")
                    models = [
                        {"id": line.split()[0], "name": line.split()[0]}
                        for line in output.decode().splitlines()[1:]
                        if line.split()
                    ]
                else:
                    from .catalog import endpoint_models

                    models = await asyncio.to_thread(endpoint_models, profile)
                for model in models:
                    rows.append((alias + "  " + model["name"], (alias, dict(profile, model=model["id"]))))
            except Exception as error:
                self.say(f"{alias}: model discovery failed: {error}")
        return rows

    def forget_route(self):
        """A new conversation: auto picks its model again from the next first message."""
        self.route = None
        if self.auto:
            self.selected["solo"], self.profiles["solo"] = None, {}

    def set_auto(self):
        """Make the soloist auto: each conversation's model is picked from its first request."""
        self.auto = True
        self.forget_route()
        try:
            remember_role("solo", routing.AUTO, "")
        except (ValueError, OSError) as error:
            self.say(f"Model choice applies to this session only: {error}", DIM)
        line = Text("  Soloist: " if not self.simple else "  Model: ", style=GRAY)
        line.append("auto", style=f"bold {ORANGE}")
        self.say(line)
        self.say(
            "  Donut rates the first message of each conversation from 1 to 10 and picks the model for it. "
            "The model stays for the conversation; /harder moves one model up.",
            DIM,
        )
        self.status()

    async def routing_steps(self):
        """The models auto chooses from, cheapest first: from the configuration, or from the accounts' models."""
        if self.steps is None:
            steps = routing.configured(self.config)
            if not steps:
                rows = await self.model_rows(engines=("claude", "codex"))
                steps = routing.build([(alias, profile, label.split("  ", 1)[-1]) for label, (alias, profile) in rows])
            self.steps = steps
        return self.steps

    async def one_shot(self, profile, prompt, limit=90):
        """Ask a model one question in a throwaway read-only session and return its answer.

        Nothing is shown in the transcript and the session takes no part in any conversation.
        """
        cls = session_class(profile.get("engine"))
        if not cls:
            raise ValueError("No session for this engine.")
        parts = []

        def emit(kind, data, *rest):
            if kind == "text":
                parts.append(data)

        async def deny(name, details):
            return "no"

        session = cls(self.project, profile, emit, deny)
        session.permission, session.read_only = "ask", True
        session.resume_id = session.conversation_id = None
        await session.connect()
        self.active = session  # So Ctrl+C can interrupt it.
        try:
            await asyncio.wait_for(session.query(prompt), limit)
            self.add_spent(session)
        finally:
            self.active = None
            await session.close()
        return "".join(parts)

    def use_step(self, index, score, harder=0):
        step = self.steps[index]
        self.route = {"index": index, "score": score, "harder": harder}
        self.selected["solo"], self.profiles["solo"] = step["alias"], copy.deepcopy(step["profile"])
        self.status()
        return step

    async def choose_route(self, text):
        """Rate the conversation's first request and fix its model: the first one whose limit covers the score.

        Returns False when the user cancelled while the request was being rated; nothing is sent then.
        """
        self.activity, self.detail = "choosing a model", ""
        self.status()
        steps = await self.routing_steps()
        if not steps:
            raise ValueError(
                'Auto needs a Claude or Codex account to choose from, or "routing": {"models": [...]} in the '
                "configuration. /model picks one model instead."
            )
        score, problem = None, ""
        self.activity = "choosing a model"
        self.status()
        # A classifier that fails (a wrong model ID, an expired login) hands over to the next strongest model.
        for alias, profile in routing.classifiers(self.config, steps):
            try:
                score = routing.parse_score(await self.one_shot(profile, routing.classifier_prompt(text)))
                problem = "" if score else "the request could not be rated"
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                problem = f"the request could not be rated ({str(error)[:80]})"
                if self.cancelled or isinstance(error, asyncio.TimeoutError):
                    break  # A slow network would be slow for every model.
        if self.cancelled:
            self.say("  Cancelled before a model was chosen; nothing was sent.", DIM)
            return False
        index = routing.pick(steps, score) if score else routing.fallback(steps)
        step = self.use_step(index, score)
        routing.log(
            "route",
            score=score,
            model=step["profile"].get("model"),
            alias=step["alias"],
            position=index + 1,
            of=len(steps),
            classifier=profile.get("model") or alias,
            fallback=score is None,
            length=len(text),
        )
        line = Text("  auto", style=f"bold {ORANGE}")
        line.append(f" · difficulty {score}/10" if score else f" · {problem}; using the middle model", style=GRAY)
        line.append(" → ", style=DIM)
        line.append(step["name"], style=f"bold {TEAL}")
        if index < len(steps) - 1:
            line.append("   /harder for a stronger model", style=DIM)
        self.say(line)
        return True

    async def harder(self):
        """/harder: move this conversation one model up the list. The conversation carries over."""
        if not self.auto:
            self.say("  /harder works when the model is auto. /model lets you choose auto.", DIM)
            return
        if self.route is None:
            self.say("  Send a message first: auto picks this conversation's model then.", DIM)
            return
        steps = await self.routing_steps()
        index = self.route.get("index")
        if index is None:
            self.say("  This conversation's model is not one auto chooses from. /model picks another.", DIM)
            return
        if index >= len(steps) - 1:
            self.say(f"  {steps[index]['name']} is already the strongest model auto chooses from.", DIM)
            return
        before = steps[index]
        step = self.use_step(index + 1, self.route.get("score"), self.route.get("harder", 0) + 1)
        routing.log(
            "harder",
            score=self.route.get("score"),
            model=step["profile"].get("model"),
            alias=step["alias"],
            position=index + 2,
            of=len(steps),
            was=before["profile"].get("model"),
        )
        line = Text("  harder", style=f"bold {ORANGE}")
        line.append(" → ", style=DIM)
        line.append(step["name"], style=f"bold {TEAL}")
        line.append(f"  (was {before['name']}). It continues this conversation from your next message.", style=DIM)
        self.say(line)

    async def show_routing(self):
        """/routing: the models auto chooses from, the scores each takes, and what this conversation got."""
        if self.active is not None and self.steps is None:
            self.say("  The model list is still being found; try again in a moment.", DIM)
            return
        steps = await self.routing_steps()
        if not steps:
            self.say(
                '  No models to choose from: add a Claude or Codex account, or "routing" in the configuration.', DIM
            )
            return
        self.say(Text("  Auto chooses from (cheapest first)", style=f"bold {ORANGE}"))
        current = (self.route or {}).get("index") if self.auto else None
        low = 1
        for index, step in enumerate(steps):
            line = Text("  ● " if index == current else "  ○ ", style=f"bold {ORANGE}" if index == current else DIM)
            line.append(
                f"{low}–{step['up_to']}".ljust(6) if low < step["up_to"] else f"{step['up_to']}".ljust(6), style=GRAY
            )
            line.append(step["name"], style=f"bold {TEAL}" if index == current else GRAY)
            line.append(f"  {step['alias']}", style=DIM)
            self.say(line)
            low = step["up_to"] + 1
        alias, profile = routing.classifier(self.config, steps)
        rated = Text("  Requests are rated by ", style=DIM)
        rated.append(model_name(profile, alias), style=GRAY)
        if profile.get("effort"):
            rated.append(f" at {profile['effort']} effort", style=DIM)
        self.say(rated)
        if not self.auto:
            self.say("  Auto is off: the model is the one you chose. /model lets you choose auto.", DIM)
        elif self.route and self.route.get("score"):
            self.say(f"  This conversation was rated {self.route['score']}/10.", DIM)

    async def first_open(self):
        """The first time Donut opens, ask which version of the interface to show. Asked once and saved."""
        rows = [(label, key, description) for key, (label, description) in INTERFACES.items()]
        chosen = await self.choose("How do you want to use Donut?", rows)
        if chosen:
            self.interface = chosen
            self.query_one(RichLog).clear()  # Nothing has been said yet: greet again in the chosen version.
            self.welcome()
            await self.set_interface(chosen)
        else:
            self.say("  Showing everything for now; Donut asks again next time. /simple and /complex switch.", DIM)

    async def set_interface(self, name):
        """/simple and /complex: which version of the interface is shown. Models are told nothing of it."""
        self.interface = name
        try:
            config = load()
            config["interface"] = name
            save(config)
        except (ValueError, OSError) as error:
            self.say(f"This applies to this session only: {error}", DIM)
        self.config["interface"] = name
        self.refresh_bindings()
        label, description = INTERFACES[name]
        line = Text("  " + label, style=f"bold {ORANGE}")
        if self.simple:
            line.append(
                "  One conversation; Donut picks the model for each request. /complex shows everything.", style=DIM
            )
            self.say(line)
            self.set_mode("solo")
            if not self.auto:
                self.set_auto()
        else:
            line.append("  Planner, worker, reviewer and tickets are shown. /simple hides them again.", style=DIM)
            self.say(line)
            self.set_mode(self.mode)
        self.status()

    async def usage(self):
        """How much of each plan is left, as the usage bar shows it but with every window and reset time."""
        if self.active is not None or self.child is not None:
            # A model is mid-turn, and a Claude session answers one request at a time: asking now would wait.
            self.say("  Latest figures; they refresh when the current turn ends.", DIM)
        else:
            self.status("checking usage")
            await self.refresh_quotas(connect=True)
        for key, (label, _, _) in self.usage_groups().items():
            line = Text("  " + label, style=f"bold {ORANGE}")
            windows = self.quotas.get(key)
            if key[0] == "local":
                line.append("   local model, no plan limits", style=DIM)
            elif windows is None:
                line.append("   usage unavailable (not signed in, or the provider did not report it)", style=DIM)
            elif not windows:
                line.append("   no plan limits (API billing)", style=DIM)
            self.say(line)
            for window in windows or []:
                left = remaining(window["percent"])
                row = Text("    ")
                row.append_text(usage_bar(window["percent"], width=20)[0])
                row.append(f"  {left}% left", style=left_color(left))
                row.append(f"  {window['window'] or 'window'}", style=GRAY)
                reset = countdown(window.get("resets_at"))
                if reset:
                    row.append(f"  resets in {reset}", style=DIM)
                self.say(row)

    async def work_tickets(self, choice):
        words = choice.split()
        no_pr = "no-pr" in words
        words = [word for word in words if word != "no-pr"]
        checkpoint = words[-1:] == ["checkpoint"]
        if checkpoint:
            words = words[:-1]
        if (
            len(words) != 2
            or words[0] != "next"
            or not (words[1] == "all" or words[1].isdecimal() and int(words[1]) > 0)
        ):
            raise ValueError("Use /work next COUNT [checkpoint] [no-pr] (COUNT is a positive integer or all).")
        if checkpoint:
            words.append("--checkpoint")
        if no_pr:
            words.append("--no-pr")
        alias = self.selected["work"]
        if not alias:
            raise ValueError("Run donut setup to configure a worker.")
        await asyncio.to_thread(install, self.project, Path.home(), quiet=True, session=self.session_name)
        with tempfile.TemporaryDirectory(prefix="donut-worker-") as temp:
            config = copy.deepcopy(self.config)
            config["aliases"][alias] = dict(self.profiles["work"], permission_mode=self.permission)
            if self.profiles.get("review"):
                # A dedicated alias, so a reviewer on the same account as the worker keeps its own model.
                config["aliases"]["donut-reviewer"] = copy.deepcopy(self.profiles["review"])
                config["reviewer"] = "donut-reviewer"
            else:
                config["reviewer"] = "off"
            if self.profiles.get("plan"):
                # Blocked or stuck tickets go back to the planner, with the same permission mode as the worker.
                config["aliases"]["donut-planner"] = dict(self.profiles["plan"], permission_mode=self.permission)
                config["recovery"] = "donut-planner"
            folder = Path(temp) / "donut"
            folder.mkdir()
            path = folder / "config.json"
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            env = dict(
                os.environ, XDG_CONFIG_HOME=temp, PYTHONIOENCODING="utf-8", NO_COLOR="1", DONUT_VERBOSITY=self.verbosity
            )
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "donut",
                "-p",
                str(self.project),
                "-m",
                alias,
                *(["--session", self.session_name] if self.session_name else []),
                "--work",
                *words,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_options(),
            )
            highlighter, ticket = Highlighter(), ""
            self.start_turn("work", "", "starting")
            async for raw in self.child.stdout:
                # Keep only what a terminal would finally show on this line: no escapes, no \r redraws.
                line = ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace").rstrip("\r\n")).split("\r")[-1]
                line = line.rstrip()
                if line and all("\u2800" <= c <= "\u28ff" or c.isspace() for c in line):
                    continue  # Stray spinner frames.
                progress = RUNNER_PROGRESS.match(line)
                if progress:
                    continue  # The spinner line already shows the worker and its elapsed time.
                started = RUNNER_TICKET.match(line)
                if started:
                    ticket = started["ticket"]
                    self.start_turn("work", ticket, "working")
                review = RUNNER_REVIEW.match(line)
                if review:
                    self.start_turn("review", review["ticket"], "reviewing")
                if line.startswith("Review: changes requested"):
                    self.start_turn("work", ticket, "addressing review feedback")
                recovering = RUNNER_RECOVER.match(line)
                if recovering:
                    self.start_turn("plan", recovering["ticket"], "recovering a blocked ticket")
                if line.startswith("[repair queue]"):
                    self.start_turn("plan", "", "repairing stuck tickets")
                self.say(highlighter.line(line))
            code = await self.child.wait()
            self.child = None
            if code == 0:
                self.say("WORK COMPLETE", f"bold {ORANGE}")
            else:
                self.say(f"Worker stopped with exit code {code}; inspect results above.", CORAL)

    async def login(self):
        profile = self.profiles[self.role]
        engine = profile["engine"]
        if engine not in ("claude", "codex"):
            return self.say(f"/login: {engine} has no sign-in; set its key in your shell.", DIM)
        env = os.environ.copy()
        if profile.get("claude_config_dir"):
            env["CLAUDE_CONFIG_DIR"] = str(Path(profile["claude_config_dir"]).expanduser())
        args = [shutil.which(engine) or engine] + (["auth", "login"] if engine == "claude" else ["login"])
        with self.suspend():
            code = subprocess.call(args, cwd=self.project, env=env)
        self.say("Signed in. Send your message again." if code == 0 else f"Login failed (exit {code}).")

    async def native(self):
        profile = self.profiles[self.role]
        engine = profile["engine"]
        executable = "qwen" if engine in ("ollama", "openai") else engine
        args = [shutil.which(executable) or executable]
        env = os.environ.copy()
        if profile.get("claude_config_dir"):
            env["CLAUDE_CONFIG_DIR"] = profile["claude_config_dir"]
        if profile.get("model"):
            args += ["--model", profile["model"]]
        if engine in ("ollama", "openai"):
            env["OPENAI_API_KEY"] = "ollama" if engine == "ollama" else env.get(profile["key_env"], "")
            args += [
                "--auth-type",
                "openai",
                "--openai-base-url",
                ollama_base_url() if engine == "ollama" else profile["base_url"],
            ]
        with self.suspend():
            code = subprocess.call(args, cwd=self.project, env=env)
        self.say(f"Returned from native {executable} (exit {code}).")


def run_terminal(project, alias=None, session=None):
    if not sys.stdin.isatty():
        raise ValueError("Interactive Donut requires a terminal. Use --plan REQUEST or --work next COUNT in scripts.")
    Donut(project, alias, session).run()
