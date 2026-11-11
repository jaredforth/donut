import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from donut.terminal import Donut, Draft, Menu, OptionList, RichLog, Static

# Never start real providers from the background usage refresh during tests.
Donut.AUTO_REFRESH_QUOTAS = False
Donut.ASK_INTERFACE = False  # The first-open question has its own test.


def setUpModule():
    """Saved /model, /review and permission choices go to a throwaway config folder, never the real one."""
    import os

    global _config_home, _previous_config_home, _previous_state_home
    _config_home = tempfile.TemporaryDirectory()
    _previous_config_home = os.environ.get("XDG_CONFIG_HOME")
    _previous_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_CONFIG_HOME"] = _config_home.name
    # The /resume conversation log goes here too, never to the real state folder.
    os.environ["XDG_STATE_HOME"] = os.path.join(_config_home.name, "state")
    # Plugins and skills are read from empty provider folders, never the real ~/.claude or ~/.codex.
    global _provider_homes
    _provider_homes = patch.dict(
        os.environ,
        {
            "CLAUDE_CONFIG_DIR": os.path.join(_config_home.name, "claude"),
            "CODEX_HOME": os.path.join(_config_home.name, "codex"),
        },
    )
    _provider_homes.start()


def tearDownModule():
    import os

    _provider_homes.stop()
    if _previous_config_home is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = _previous_config_home
    if _previous_state_home is None:
        os.environ.pop("XDG_STATE_HOME", None)
    else:
        os.environ["XDG_STATE_HOME"] = _previous_state_home
    _config_home.cleanup()


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_commands_and_mode_picker(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "aliases": {
                    "claude": {"engine": "claude", "model": "sonnet"},
                    "claude-second": {"engine": "claude", "model": "sonnet"},
                },
                "planner": "claude",
                "default_model": "claude-second",
            }
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.selected["plan"], "claude")
                self.assertEqual(app.selected["work"], "claude-second")
                app.execute("/help")
                await pilot.pause()
                self.assertFalse(app.busy)
                said, say = [], app.say
                app.say = lambda *args, **kwargs: (said.append(str(args[0])), say(*args, **kwargs))[1]
                app.execute("/help this isn't a quotation")  # An apostrophe must not stop the command.
                await pilot.pause()
                app.say = say
                self.assertTrue(said)
                self.assertFalse([line for line in said if "No closing quotation" in line])
                app.execute("/work")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(menu.kind, "pick")
                self.assertEqual(len(app.screen_stack), 1)  # Inline, not a new screen.
                self.assertIs(app.focused, app.query_one(Draft))
                self.assertEqual(
                    [row[3] for row in menu.visible_rows],
                    [
                        "/work next 1",
                        "/work next 3",
                        "/work next all",
                        "/work next 1 checkpoint",
                        "/work next all checkpoint",
                    ],
                )
                rendered = str(menu.query_one(OptionList).get_option_at_index(1).prompt)
                self.assertIn("Next 3 tickets", rendered)
                self.assertTrue(rendered.rstrip().endswith("/work next 3"))
                self.assertIn("/work next N", menu.border_subtitle)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(menu.is_open)
                self.assertFalse(app.busy)
                self.assertEqual(app.mode, "plan")  # /work returns to planning so follow-ups are accepted.

    async def test_model_picker_keeps_account_and_model_together(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"aliases": {"claude": {"engine": "claude"}, "claude-second": {"engine": "claude"}}}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            fake = AsyncMock()
            fake.models.return_value = [{"id": "sonnet", "name": "Sonnet 5.5"}]
            app.session = AsyncMock(return_value=fake)
            async with app.run_test() as pilot:
                app.execute("/model")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertEqual(
                    [row[0] for row in menu.visible_rows], ["claude  Sonnet 5.5", "claude-second  Sonnet 5.5"]
                )
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(app.selected["plan"], "claude-second")
                self.assertEqual(app.profiles["plan"]["model"], "sonnet")
                self.assertEqual(app.selected["work"], "claude")


class DraftTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_enter_sends_and_newline_keys_insert(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            sent = []
            app.execute = lambda text: sent.append(text)
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                draft.insert("first")
                await pilot.press("ctrl+j")
                draft.insert("second")
                await pilot.press("shift+enter")
                draft.insert("third")
                await pilot.pause()
                self.assertEqual(draft.text, "first\nsecond\nthird")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(sent, ["first\nsecond\nthird"])
                self.assertEqual(draft.text, "")

    async def test_up_down_walk_history(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.execute = lambda text: None
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                for word in ("one", "two"):
                    draft.insert(word)
                    await pilot.press("enter")
                    await pilot.pause()
                draft.insert("draft")
                for key, want in (("up", "two"), ("up", "one"), ("up", "one"), ("down", "two"), ("down", "draft")):
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(draft.text, want)
                draft.load_text("a\nb")
                draft.move_cursor((1, 0))
                await pilot.press("up")
                await pilot.pause()
                self.assertEqual(draft.cursor_location[0], 0)
                self.assertEqual(draft.text, "a\nb")

    async def test_recalled_slash_entry_does_not_open_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.execute = lambda text: None
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                for word in ("one", "/focus "):  # trailing space keeps the menu closed so Enter sends
                    draft.insert(word)
                    await pilot.press("enter")
                    await pilot.pause()
                await pilot.press("up")
                await pilot.pause()
                self.assertEqual(draft.text, "/focus")
                self.assertFalse(app.query_one(Menu).is_open)
                await pilot.press("up")
                await pilot.pause()
                self.assertEqual(draft.text, "one")

    async def test_slash_opens_menu_after_recall(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.execute = lambda text: None
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                draft.insert("one")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("up")
                await pilot.pause()
                draft.clear()
                draft.insert("/")
                await pilot.pause()
                self.assertTrue(app.query_one(Menu).is_open)

    async def test_ctrl_c_reaches_cancel_while_editing(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.action_cancel = AsyncMock()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(Draft).insert("draft")
                await pilot.press("ctrl+c")
                await pilot.pause()
            app.action_cancel.assert_awaited()

    async def test_welcome_logo_opens_the_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                lines = [strip.text for strip in app.query_one(RichLog).lines]
                self.assertIn("_.------", lines[0])
                self.assertTrue(any("do the thing." in line for line in lines[:3]))
                self.assertTrue(any("DONUT" in line for line in lines))

    async def test_the_input_box_shows_what_the_turns_cost(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                draft = app.query_one(Draft)
                self.assertEqual(draft.border_subtitle, "$0.00 spent")
                first, second = SimpleNamespace(usage={"reported_cost_usd": 0.25}), SimpleNamespace(usage={})
                app.add_spent(first)
                app.add_spent(second)  # No cost reported (Codex, Ollama): nothing is added.
                first.usage = {"reported_cost_usd": 0.75}  # A running total: only the difference counts.
                app.add_spent(first)
                first.usage = {"reported_cost_usd": 0.10}  # A new provider process starts over.
                app.add_spent(first)
                self.assertEqual(draft.border_subtitle, "$0.85 spent")
                # During a turn each response adds an estimate; the same response reported twice counts once.
                app.event("spend", {"id": "a", "dollars": 0.10})
                app.event("spend", {"id": "a", "dollars": 0.10})
                app.event("spend", {"id": "b", "dollars": 0.05})
                self.assertEqual(draft.border_subtitle, "~$1.00 spent")
                first.usage = {"reported_cost_usd": 0.30}  # The turn ends: the reported cost replaces the estimate.
                app.add_spent(first)
                self.assertEqual(draft.border_subtitle, "$1.05 spent")


class SlashMenuTests(unittest.IsolatedAsyncioTestCase):
    async def run_app(self, temp):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        with patch("donut.terminal.load", return_value=config):
            app = Donut(Path(temp))
        self.sent = []
        app.execute = lambda text: self.sent.append(text)
        return app

    async def test_typing_slash_filters_inline_and_enter_runs_the_top_match(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/")
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(menu.visible_rows[0][0], "/plan")
                await pilot.press("m", "o")
                await pilot.pause()
                self.assertEqual([row[0] for row in menu.visible_rows], ["/model"])
                self.assertEqual(app.query_one(OptionList).highlighted, 0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(self.sent, ["/model"])
                self.assertFalse(menu.is_open)
                self.assertEqual(app.query_one(Draft).text, "")

    async def test_arrows_move_tab_completes_and_escape_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                draft = app.query_one(Draft)
                menu = app.query_one(Menu)
                await pilot.press("/", "c")
                await pilot.pause()
                self.assertEqual([row[0] for row in menu.visible_rows][:3], ["/context", "/clear", "/copy"])
                await pilot.press("down", "tab")
                await pilot.pause()
                self.assertEqual(draft.text, "/clear ")
                self.assertFalse(menu.is_open)
                self.assertEqual(self.sent, [])
                draft.clear()
                await pilot.press("/", "p")
                await pilot.pause()
                self.assertTrue(menu.is_open)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(menu.is_open)
                self.assertEqual(draft.text, "/p")

    async def test_arguments_close_the_menu_so_enter_sends_the_line(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/", "w", "o", "r", "k", "space", "n")
                await pilot.pause()
                self.assertFalse(app.query_one(Menu).is_open)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(self.sent, ["/work n"])

    async def test_unknown_command_hides_the_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.press("/", "z", "z")
                await pilot.pause()
                self.assertFalse(app.query_one(Menu).is_open)

    async def test_tool_approval_is_an_inline_panel_with_number_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls -la", "description": "List files"}))
                await pilot.pause()
                menu = app.query_one(Menu)
                self.assertTrue(menu.is_open)
                self.assertEqual(menu.kind, "approve")
                self.assertEqual(menu.border_title, "Bash command")
                self.assertEqual([row[1] for row in menu.visible_rows], ["yes", "always", "no"])
                details = menu.query_one("#menu-details").render()
                self.assertIn("$ ls -la", str(details))
                self.assertNotIn("{", str(details))  # Not a raw JSON dump.
                await pilot.press("3")
                await pilot.pause()
                self.assertEqual(await answer, "no")
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "ls"}))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(await answer, "yes")
                answer = asyncio.ensure_future(app.approve("Bash", {"command": "pwd"}))
                await pilot.pause()
                await pilot.press("2")
                await pilot.pause()
                self.assertEqual(await answer, "always")
                self.assertEqual(await app.approve("Bash", {"command": "whoami"}), "always")  # No second prompt.
                self.assertFalse(menu.is_open)
                lines = [strip.text for strip in app.query_one(RichLog).lines]
                self.assertTrue(any("Denied Bash command" in line and "ls -la" in line for line in lines))

    async def test_escape_denies_and_keeps_the_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            app = await self.run_app(temp)
            async with app.run_test() as pilot:
                draft = app.query_one(Draft)
                draft.insert("half-typed follow-up")
                answer = asyncio.ensure_future(app.approve("Write", {"file_path": "a.txt", "content": "x"}))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(await answer, "no")
                self.assertEqual(draft.text, "half-typed follow-up")


class FakeSession:
    def __init__(self, app, delay=0.3):
        self.app, self.delay, self.calls, self.modes = app, delay, [], []

    async def query(self, text):
        self.calls.append(text)
        self.app.event("text", f"answer {len(self.calls)}")
        await asyncio.sleep(self.delay)

    async def set_permission(self, mode):
        self.modes.append(mode)

    async def interrupt(self):
        pass

    async def close(self):
        pass


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
        self.saved = []
        patches = [
            patch("donut.terminal.load", return_value=config),
            patch("donut.terminal.save", side_effect=lambda c: self.saved.append(dict(c))),
            patch("donut.terminal.install"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(Path(self.temp.name))
        self.fake = FakeSession(self.app)

        async def session(alias, profile, *args, **kwargs):
            self.app.sessions[(alias, profile.get("model"))] = self.fake
            return self.fake

        self.app.session = session

    async def wait_for(self, pilot, condition, timeout=5):
        for _ in range(int(timeout / 0.02)):
            if condition():
                return
            await pilot.pause(0.02)
        self.fail("condition not reached")

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")

    async def test_follow_ups_reach_the_same_session(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await pilot.pause(0.5)
            await self.send(pilot, "second")
            await pilot.pause(0.5)
            self.assertEqual(len(self.fake.calls), 2)
            self.assertTrue(self.fake.calls[1].endswith("second"))

    async def test_display_only_commands_run_at_once_during_a_turn(self):
        self.fake.delay = 3
        async with self.app.run_test() as pilot:
            await self.send(pilot, "a long task")
            await self.wait_for(pilot, lambda: self.app.active is not None)
            await self.send(pilot, "/help")
            await self.send(pilot, "/usage")
            await self.send(pilot, "and then this")
            await pilot.pause(0.3)
            body = "\n".join(strip.text for strip in self.app.query_one(RichLog).lines)
            self.assertIn("/plan · /work", body)  # /help answered mid-turn.
            self.assertIn("Latest figures; they refresh when the current turn ends.", body)
            self.assertEqual(self.app.queued, ["and then this"])  # A message to the model still waits.
            self.assertTrue(self.app.busy)  # The turn carries on.
            self.assertEqual(len(self.fake.calls), 1)
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 2, timeout=8)

    async def test_messages_sent_during_a_turn_are_queued_then_run(self):
        self.fake.delay = 3
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 1)
            self.assertTrue(self.app.busy)
            await self.send(pilot, "follow-up while busy")
            await self.wait_for(pilot, lambda: self.app.queued == ["follow-up while busy"])
            self.assertEqual(self.app.query_one(Draft).text, "")
            self.fake.delay = 0
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 2 and not self.app.busy, timeout=8)
            self.assertTrue(self.fake.calls[1].endswith("follow-up while busy"))
            self.assertEqual(self.app.queued, [])

    async def test_cancel_returns_queued_messages_to_the_prompt(self):
        self.fake.delay = 3
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await self.wait_for(pilot, lambda: len(self.fake.calls) == 1)
            await self.send(pilot, "keep me")
            await self.wait_for(pilot, lambda: self.app.queued == ["keep me"])
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            self.assertEqual(self.app.queued, [])
            self.assertEqual(self.app.query_one(Draft).text, "keep me")

    async def test_free_text_in_work_mode_goes_to_the_planner(self):
        async with self.app.run_test() as pilot:
            self.app.mode = "work"
            await self.send(pilot, "what about tests?")
            await pilot.pause(0.5)
            self.assertEqual(len(self.fake.calls), 1)
            self.assertEqual(self.app.mode, "plan")

    async def test_permissions_picker_auto_toggle_and_shift_tab(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "first")
            await pilot.pause(0.5)
            self.assertEqual(self.app.permission, "ask")
            await self.send(pilot, "/permissions")
            await pilot.pause()
            menu = self.app.query_one(Menu)
            self.assertEqual([row[1] for row in menu.visible_rows], ["ask", "accept_edits", "auto", "full"])
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            self.assertEqual(self.app.permission, "auto")
            self.assertEqual(self.fake.modes, ["auto"])
            self.assertEqual(self.saved[-1]["permission_mode"], "auto")
            await self.send(pilot, "/auto")
            await pilot.pause()
            self.assertEqual(self.app.permission, "ask")
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(self.app.permission, "accept_edits")
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(self.app.permission, "auto")
            self.assertEqual(self.fake.modes, ["auto", "ask", "accept_edits", "auto"])
            status = str(self.app.query_one("#status").render())
            self.assertIn("auto on", status)

    async def test_saved_permission_mode_is_restored(self):
        config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude", "permission_mode": "auto"}
        with patch("donut.terminal.load", return_value=config):
            app = Donut(Path(self.temp.name))
        self.assertEqual(app.permission, "auto")


class WorkOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_worker_of_a_named_profile_finds_its_configuration(self):
        import os

        seen = {}

        async def fake_exec(*args, **kwargs):
            home = Path(kwargs["env"]["XDG_CONFIG_HOME"])
            seen["files"] = sorted(str(path.relative_to(home)) for path in home.rglob("*.json"))
            raise OSError("stop here")

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"DONUT_PROFILE": "personal"}):
            config = {"aliases": {"local": {"engine": "ollama", "model": "qwen3"}}, "default_model": "local"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("donut.terminal.asyncio.create_subprocess_exec", side_effect=fake_exec):
                    with self.assertRaises(OSError):
                        await app.work_tickets("next 1")
        self.assertEqual(seen["files"], [str(Path("donut/profiles/personal.json"))])

    async def test_progress_updates_the_status_bar_and_output_is_highlighted(self):
        import sys

        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "runner.py"
            script.write_text(
                "import sys\n"
                "lines = ['Preparing local model\u2026', '\u280b', '\u2819', '\u2839',"
                " '[1/1] 047_sandbox-wall.md \u2014 fresh context',"
                " '[qwen3 \u00b7 Connected \u00b7 preparing project context]',"
                " '[Working \u00b7 15s elapsed \u00b7 waiting for agent output]',"
                " '[Working \u00b7 30s elapsed \u00b7 waiting for agent output]',"
                " 'Tests: 12 passed', '\\x1b[32mcolored\\x1b[0m']\n"
                "print('\\n'.join(lines))\n"
            )
            config = {"aliases": {"local": {"engine": "ollama", "model": "qwen3"}}, "default_model": "local"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            turns = []
            original_start = app.start_turn

            def start_turn(role, detail="", activity="thinking"):
                turns.append((role, detail))
                original_start(role, detail, activity)

            app.start_turn = start_turn
            launch = asyncio.create_subprocess_exec

            async def fake_exec(*args, **kwargs):
                return await launch(sys.executable, str(script), **kwargs)

            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("donut.terminal.asyncio.create_subprocess_exec", side_effect=fake_exec):
                    await app.work_tickets("next 1")
                lines = [strip.text.rstrip() for strip in app.query_one(RichLog).lines]
            body = "\n".join(lines)
            self.assertNotIn("elapsed", body)
            self.assertFalse(any(line.strip() in ("\u280b", "\u2819", "\u2839") for line in lines))
            self.assertIn(("work", "047_sandbox-wall.md"), turns)
            self.assertIn("Tests: 12 passed", body)
            self.assertIn("colored", body)
            self.assertNotIn("\x1b", body)


class ActiveModelAndUsageTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {
            "aliases": {
                "claude-second": {"engine": "claude", "model": "claude-opus-5-5", "claude_config_dir": "/x"},
                "codex": {"engine": "codex", "model": "gpt-6"},
                "local": {"engine": "ollama", "model": "qwen3.6:35b"},
                "other-local": {"engine": "ollama", "model": "llama"},
            },
            "planner": "claude-second",
            "default_model": "local",
        }
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_spinner_and_status_name_the_running_model(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                idle = str(app.query_one("#status").render())
                self.assertIn("planner claude-opus-5-5", idle)
                self.assertIn("worker qwen3.6:35b", idle)
                self.assertFalse(app.query_one("#thinking").has_class("-busy"))
                app.busy = True
                app.start_turn("work", "047_wall.md", "working")
                app.event("activity", "Bash")
                await pilot.pause(0.2)
                spinner = str(app.query_one("#thinking").render())
                self.assertIn("qwen3.6:35b", spinner)
                self.assertIn("worker", spinner)
                self.assertIn("is running a command", spinner)
                self.assertIn("047_wall.md", spinner)
                self.assertIn("ctrl+c to cancel", spinner)
                self.assertTrue(app.query_one("#thinking").has_class("-busy"))
                status = app.query_one("#status").render()
                bold_spans = [str(status)[span.start : span.end] for span in status.spans if "bold" in str(span.style)]
                self.assertIn("qwen3.6:35b", bold_spans)
                self.assertNotIn("claude-opus-5-5", bold_spans)

    async def test_spinner_timer_tolerates_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.busy = True
                app.start_turn("plan")
            # The 0.1s timer can still fire once the widgets are gone.
            app.tick()
            app.status("writing")

    async def test_usage_bar_shows_each_account_once(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            async with app.run_test() as pilot:
                await pilot.pause()
                bar = str(app.query_one("#usage").render())
                self.assertIn("claude-second", bar)
                self.assertIn("codex", bar)
                self.assertIn("qwen3.6 local", bar)
                self.assertNotIn("llama", bar)  # Only local models in use are listed.
                app.event(
                    "usage", [{"window": "5h", "percent": 18.0}, {"window": "7d", "percent": 30.0}], ("claude", "/x")
                )
                app.event("usage", [{"window": "5h", "percent": 91.0}], ("codex",))
                await pilot.pause()
                bar = str(app.query_one("#usage").render())
                self.assertIn("82% left 5h · 70% 7d", bar)
                self.assertIn("9% left 5h", bar)
                self.assertIn("━", bar)


class UsageCommandTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp):
        config = {
            "aliases": {
                "claude": {"engine": "claude"},
                "codex": {"engine": "codex"},
                "api": {"engine": "claude", "claude_config_dir": "/api"},
            },
            "planner": "claude",
        }
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    async def test_usage_command_shows_what_is_left_for_each_account(self):
        import time

        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            soon = time.time() + 2 * 3600 + 600
            answers = {
                "claude": [{"window": "5h", "percent": 18.0, "resets_at": soon}, {"window": "7d", "percent": 30.0}],
                "codex": None,
                "api": [],
            }

            async def session(alias, profile, *args, **kwargs):
                fake = AsyncMock()
                fake.quota.return_value = answers[alias]
                app.sessions[(alias, profile.get("model"))] = fake
                app.session_profiles[(alias, profile.get("model"))] = profile
                return fake

            app.session = session
            async with app.run_test() as pilot:
                await pilot.pause()
                app.execute("/usage")
                await pilot.pause(0.3)
                body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
                bar = str(app.query_one("#usage").render())
            self.assertNotIn("Error", body)
            self.assertIn("82% left  5h  resets in 2h", body)
            self.assertIn("70% left  7d", body)
            self.assertIn("codex   usage unavailable", body)
            self.assertIn("api   no plan limits (API billing)", body)
            self.assertIn("82% left 5h ↻2h", bar)
            self.assertIn("api no plan limit", bar)

    async def test_refresh_without_connect_only_asks_open_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            app.session = AsyncMock(side_effect=AssertionError("must not start a session"))
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.refresh_quotas(connect=False)
                self.assertEqual(app.quotas, {})


class QuotaFormattingTests(unittest.TestCase):
    def test_countdown_and_remaining(self):
        from donut.terminal import countdown, remaining

        self.assertEqual(countdown(1000 + 2 * 3600 + 600, now=1000), "2h10m")
        self.assertEqual(countdown(1000 + 3 * 86400 + 4 * 3600, now=1000), "3d4h")
        self.assertEqual(countdown(1000 + 30, now=1000), "1m")
        self.assertEqual(countdown(500, now=1000), "")
        self.assertEqual(countdown(None), "")
        self.assertEqual((remaining(18.4), remaining(120), remaining(-5)), (82, 0, 100))


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    def app(self, temp, **extra):
        config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "claude-opus-5-5"},
                "codex": {"engine": "codex", "model": "gpt-6.1"},
                "local": {"engine": "ollama", "model": "qwen3.6"},
            },
            "planner": "claude",
            "default_model": "local",
            **extra,
        }
        self.saved = []
        patcher = patch("donut.terminal.save", side_effect=lambda c: self.saved.append(dict(c)))
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch("donut.terminal.load", return_value=config):
            return Donut(Path(temp))

    def fake_models(self, app):
        fake = AsyncMock()
        fake.models.return_value = [{"id": "sonnet-5-5", "name": "Sonnet 5.5"}, {"id": "opus-5-5", "name": "Opus 5.5"}]
        app.session = AsyncMock(return_value=fake)

    def saved_roles(self):
        from donut.config import load

        return load().get("roles", {}), load().get("reviewer")

    async def pick(self, pilot, app, command, label):
        app.execute(command)
        await pilot.pause()
        rows = [row[0] for row in app.query_one(Menu).visible_rows]
        for _ in range(rows.index(label)):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    def roles(self, app):
        return (
            (app.selected["plan"], app.profiles["plan"].get("model")),
            (app.selected["review"], app.profiles["review"].get("model")) if app.profiles["review"] else None,
        )

    async def test_reviewer_follows_the_planner_until_chosen_then_they_stay_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp)
            self.fake_models(app)
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIn("reviewer claude-opus-5-5 (= planner)", str(app.query_one("#status").render()))
                # Default: changing the planner moves the reviewer with it.
                await self.pick(pilot, app, "/planner", "claude  Sonnet 5.5")
                self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("claude", "sonnet-5-5")))
                # Choosing a reviewer, down to the model, leaves the planner alone.
                await self.pick(pilot, app, "/reviewer", "codex  Opus 5.5")
                self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("codex", "opus-5-5")))
                self.assertIn("reviewer opus-5-5", str(app.query_one("#status").render()))
                # And changing the planner now leaves the chosen reviewer alone.
                await self.pick(pilot, app, "/planner", "claude  Opus 5.5")
                self.assertEqual(self.roles(app), (("claude", "opus-5-5"), ("codex", "opus-5-5")))
                roles, reviewer = self.saved_roles()
                self.assertEqual(roles["plan"], {"alias": "claude", "model": "opus-5-5"})
                self.assertEqual(roles["review"], {"alias": "codex", "model": "opus-5-5"})
                app.execute("/reviewer off")
                await pilot.pause()
                self.assertIn("reviewer off", str(app.query_one("#status").render()))
                self.assertEqual(self.saved_roles()[1], "off")
                app.execute("/reviewer planner")
                await pilot.pause()
                self.assertEqual(self.roles(app)[1], ("claude", "opus-5-5"))
                self.assertEqual(self.saved_roles(), ({"plan": {"alias": "claude", "model": "opus-5-5"}}, None))

    async def test_choices_stick_across_restarts(self):
        from donut.config import load, remember_role, save

        with tempfile.TemporaryDirectory() as temp:
            base = {
                "aliases": {"claude": {"engine": "claude", "model": "claude-opus-5-5"}, "codex": {"engine": "codex"}},
                "planner": "claude",
                "default_model": "claude",
            }
            save(base)
            remember_role("plan", "claude", "sonnet-5-5")
            remember_role("review", "codex", "gpt-6.1")
            with patch("donut.terminal.load", side_effect=load):
                app = Donut(Path(temp))
            self.assertEqual(self.roles(app), (("claude", "sonnet-5-5"), ("codex", "gpt-6.1")))
            self.assertEqual(app.reviewer, "chosen")

    async def test_work_passes_the_reviewer_and_the_spinner_follows_review_rounds(self):
        import sys

        with tempfile.TemporaryDirectory() as temp:
            app = self.app(temp, reviewer="codex")
            script = Path(temp) / "runner.py"
            script.write_text(
                "import json, os, pathlib, time\n"
                "config = json.loads((pathlib.Path(os.environ['XDG_CONFIG_HOME']) / 'donut/config.json').read_text())\n"
                "print('CONFIG', config.get('reviewer'), config['aliases'].get(config.get('reviewer'), {}).get('model'), flush=True)\n"
                "print('[1/1] 001_jump.md \\u2014 fresh context', flush=True)\n"
                "print('[review 1/3] 001_jump.md \\u2014 gpt-6.1, fresh context', flush=True)\n"
                "print('RECOVERY', config.get('recovery'), config['aliases'].get(config.get('recovery'), {}).get('model'), flush=True)\n"
                "print('[recover] 001_jump.md \\u2014 claude-opus-5-5, fresh context', flush=True)\n"
                "time.sleep(0.6)\n"
                "print('VERDICT: APPROVED', flush=True)\n"
                "print('Review: approved', flush=True)\n"
            )
            turns = []
            original = app.start_turn
            app.start_turn = lambda role, detail="", activity="thinking": (
                turns.append((role, activity)),
                original(role, detail, activity),
            )
            launch = asyncio.create_subprocess_exec

            async def fake_exec(*args, **kwargs):
                return await launch(sys.executable, str(script), **kwargs)

            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("donut.terminal.asyncio.create_subprocess_exec", side_effect=fake_exec):
                    await app.work_tickets("next 1")
                lines = [strip.text.rstrip() for strip in app.query_one(RichLog).lines]
            self.assertIn("CONFIG donut-reviewer gpt-6.1", lines)
            self.assertIn(("review", "reviewing"), turns)
            self.assertIn("RECOVERY donut-planner claude-opus-5-5", lines)
            self.assertIn(("plan", "recovering a blocked ticket"), turns)
            self.assertLess(turns.index(("work", "working")), turns.index(("review", "reviewing")))


class ModeAndRoleTests(unittest.IsolatedAsyncioTestCase):
    """/planner, /worker, /reviewer and /soloist pick models; /plan, /work, /review and /solo enter the mode."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "claude-opus-5-5"},
                "local": {"engine": "ollama", "model": "qwen3.6"},
            },
            "planner": "claude",
            "default_model": "local",
        }
        for item in (patch("donut.terminal.load", return_value=config), patch("donut.terminal.install")):
            item.start()
            self.addCleanup(item.stop)
        self.app = Donut(Path(self.temp.name))
        self.sessions, self.purposes = {}, []

        async def session(alias, profile, purpose=None, **kwargs):
            self.purposes.append(purpose)
            key = self.app.session_key(alias, profile, purpose)
            if key not in self.sessions:
                fake = FakeSession(self.app, delay=0)
                fake.models = AsyncMock(return_value=[{"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"}])
                self.sessions[key] = fake
            return self.sessions[key]

        self.app.session = session

    async def send(self, pilot, text):
        draft = self.app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        await pilot.pause(0.2)

    def menu_open(self):
        return self.app.query_one(Menu).is_open

    async def test_solo_asks_for_a_soloist_once_then_talks_to_it_directly(self):
        from donut.config import load

        async with self.app.run_test() as pilot:
            self.assertEqual(self.app.profiles["solo"], {})
            await self.send(pilot, "/solo")
            self.assertTrue(self.menu_open())  # No soloist yet, so it asks.
            self.assertIn("soloist", self.app.query_one(Menu).border_title)
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(self.app.mode, "solo")
            self.assertEqual(self.app.profiles["solo"]["model"], "claude-sonnet-5-5")
            self.assertEqual(self.app.profiles["work"]["model"], "qwen3.6")  # The worker is untouched.
            self.assertEqual(load()["roles"]["solo"], {"alias": "claude", "model": "claude-sonnet-5-5"})
            status = str(self.app.query_one("#status").render())
            self.assertIn("SOLO", status)
            self.assertIn("soloist claude-sonnet-5-5", status)
            self.assertIn("planner claude-opus-5-5", status)  # Every role stays visible in solo mode.
            await self.send(pilot, "rename jump() to leap()")
            self.assertEqual(self.sessions[("claude@solo", "claude-sonnet-5-5")].calls, ["rename jump() to leap()"])
            await self.send(pilot, "/plan")
            self.assertEqual(self.app.mode, "plan")
            self.assertFalse(self.menu_open())  # A planner is set, so /plan does not ask.
            await self.send(pilot, "/solo")
            self.assertFalse(self.menu_open())  # Chosen once; not asked again.
            self.assertEqual(self.app.mode, "solo")

    async def test_role_commands_only_change_their_own_role(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/worker")
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(self.app.profiles["work"]["model"], "claude-sonnet-5-5")
            self.assertEqual(self.app.profiles["plan"]["model"], "claude-opus-5-5")
            self.assertEqual(self.app.mode, "plan")  # Picking a model does not change the mode.
            await self.send(pilot, "/soloist local")
            self.assertEqual(self.app.profiles["solo"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["work"]["model"], "claude-sonnet-5-5")
            await self.send(pilot, "/planner local")
            self.assertEqual(self.app.profiles["plan"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["solo"]["model"], "qwen3.6")
            self.assertEqual(self.app.profiles["review"]["model"], "qwen3.6")  # The default reviewer follows.

    async def test_review_mode_talks_to_the_reviewer_read_only(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/review")
            self.assertFalse(self.menu_open())  # The reviewer defaults to the planner, so it is set.
            self.assertEqual(self.app.mode, "review")
            self.assertIn("REVIEW", str(self.app.query_one("#status").render()))
            await self.send(pilot, "check the wall jump for off-by-one errors")
            review = self.sessions[("claude@review", "claude-opus-5-5")]
            self.assertTrue(review.calls[0].startswith("You are the reviewer."))
            self.assertTrue(review.calls[0].endswith("check the wall jump for off-by-one errors"))
            self.assertIn("review", self.purposes)

    async def test_every_role_is_shown_in_every_mode(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/soloist local")
            for mode in ("plan", "work", "review", "solo"):
                self.app.set_mode(mode)
                await pilot.pause()
                status = str(self.app.query_one("#status").render())
                for expected in (
                    mode.upper(),
                    "planner claude-opus-5-5",
                    "worker qwen3.6",
                    "reviewer claude-opus-5-5 (= planner)",
                    "soloist qwen3.6",
                ):
                    self.assertIn(expected, status, mode)
            await self.send(pilot, "/reviewer off")
            self.assertIn("reviewer off", str(self.app.query_one("#status").render()))

    async def test_an_unset_soloist_says_so(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("soloist not set", str(self.app.query_one("#status").render()))

    async def test_review_asks_for_a_reviewer_when_review_is_off(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/reviewer off")
            await self.send(pilot, "/review")
            self.assertTrue(self.menu_open())
            self.assertNotIn("off", [row[1] for row in self.app.query_one(Menu).visible_rows])
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertEqual(self.app.mode, "plan")

    async def test_f3_cycles_plan_work_review_and_solo(self):
        async with self.app.run_test() as pilot:
            await self.send(pilot, "/soloist claude")
            for expected in ("work", "review", "solo", "plan"):
                if self.menu_open():
                    await pilot.press("escape")  # Close the work picker.
                    await pilot.pause(0.1)
                await pilot.press("f3")
                await pilot.pause(0.2)
                self.assertEqual(self.app.mode, expected)
                if expected == "work":
                    await pilot.press("escape")
                    await pilot.pause(0.1)
                    self.app.set_mode("work")


class ReadOnlySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_mode_sessions_are_read_only(self):
        created = []

        class Recorder:
            def __init__(self, project, profile, emit, approve):
                created.append(self)

            async def connect(self):
                self.connected_read_only = self.read_only

        with tempfile.TemporaryDirectory() as temp:
            config = {"aliases": {"claude": {"engine": "claude"}}, "planner": "claude"}
            with patch("donut.terminal.load", return_value=config):
                app = Donut(Path(temp))
            with patch("donut.terminal.ClaudeSession", Recorder):
                await app.session("claude", {"engine": "claude"}, "review")
                await app.session("claude", {"engine": "claude"}, "solo")
        self.assertEqual([session.connected_read_only for session in created], [True, False])


class PackEntriesTests(unittest.TestCase):
    def test_lines_break_only_between_entries(self):
        from rich.text import Text
        from donut.terminal import pack_entries

        entries = [
            Text("PLAN"),
            Text("planner claude-opus-5-5"),
            Text("reviewer claude-opus-5-5 (= planner)"),
            Text("ask"),
        ]
        packed = pack_entries(entries, 40).plain.split("\n")
        self.assertEqual(packed, ["PLAN  planner claude-opus-5-5", "reviewer claude-opus-5-5 (= planner)", "ask"])
        self.assertEqual(pack_entries(entries, 200).plain.count("\n"), 0)


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.temp.name, "state")})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.config = {
            "aliases": {
                "claude": {"engine": "claude", "model": "claude-opus-5-5"},
                "codex": {"engine": "codex", "model": "gpt-6.1"},
            },
            "planner": "claude",
            "default_model": "codex",
            "roles": {"solo": {"alias": "codex", "model": "gpt-6.1"}},
        }
        for item in (patch("donut.terminal.load", return_value=self.config), patch("donut.terminal.install")):
            item.start()
            self.addCleanup(item.stop)
        self.opened = []

    def make_app(self):
        app = Donut(Path(self.temp.name))
        counter = iter(range(1, 100))

        async def session(alias, profile, purpose=None, resume=None, conversation=None, **kwargs):
            fake = FakeSession(app, delay=0)
            fake.provider_id = resume or f"provider-{next(counter)}"
            fake.conversation_id = conversation
            self.opened.append((alias, profile.get("model"), purpose, resume))
            key = app.session_key(alias, profile, purpose)
            if conversation is None and key in app.sessions:
                return app.sessions[key]
            app.sessions[key] = fake
            return fake

        app.session = session
        return app

    async def send(self, pilot, app, text):
        draft = app.query_one(Draft)
        draft.clear()
        draft.insert(text)
        await pilot.press("enter")
        await pilot.pause(0.2)

    async def test_conversations_are_saved_with_their_mode_and_model(self):
        from donut import conversations

        app = self.make_app()
        async with app.run_test() as pilot:
            await self.send(pilot, app, "plan the wall jump")
            await self.send(pilot, app, "/solo")
            await self.send(pilot, app, "fix the typo in README")
            await self.send(pilot, app, "and the other one")
        records = conversations.for_project(self.temp.name)
        self.assertEqual(
            [(r["mode"], r["model"], r["title"], r["turns"]) for r in records],
            [
                ("solo", "gpt-6.1", "fix the typo in README", 2),
                ("plan", "claude-opus-5-5", "plan the wall jump", 1),
            ],
        )
        self.assertEqual(records[0]["provider_id"], "provider-2")
        self.assertEqual(records[0]["messages"][-2]["text"], "and the other one")

    async def test_resume_switches_mode_and_model_and_reopens_the_provider_session(self):
        from donut import conversations

        first = conversations.record_turn(
            None,
            self.temp.name,
            "solo",
            "solo",
            "claude",
            "claude-sonnet-5-5",
            "claude-sid",
            "rename jump",
            "Renamed it.",
        )
        conversations.record_turn(
            None, self.temp.name, "plan", "plan", "codex", "gpt-6.1", "thread-9", "plan sounds", "Wrote 2 tickets."
        )
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual((app.mode, app.profiles["solo"]["model"]), ("plan", "gpt-6.1"))
            app.execute("/resume")
            await pilot.pause()
            menu = app.query_one(Menu)
            rows = menu.visible_rows
            self.assertEqual([row[0] for row in rows], ["plan sounds", "rename jump"])  # Newest first.
            self.assertTrue(rows[1][2].startswith("solo · claude-sonnet-5-5 · "))
            self.assertTrue(rows[1][2].endswith("1 turn"))
            await pilot.press("down", "enter")
            await pilot.pause(0.2)
            self.assertEqual(app.mode, "solo")
            self.assertEqual((app.selected["solo"], app.profiles["solo"]["model"]), ("claude", "claude-sonnet-5-5"))
            self.assertIn(("claude", "claude-sonnet-5-5", "solo", "claude-sid"), self.opened)
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("Resumed · SOLO · claude-sonnet-5-5 · rename jump", body)
            self.assertIn("> rename jump", body)
            self.assertIn("Renamed it.", body)
            await self.send(pilot, app, "now rename land too")
        record = conversations.get(first)
        self.assertEqual(record["turns"], 2)  # The resumed conversation keeps growing, not a new one.
        from donut.config import load

        self.assertEqual(load()["roles"]["solo"], {"alias": "claude", "model": "claude-sonnet-5-5"})

    async def test_clear_and_new_start_completely_fresh(self):
        from donut import conversations

        for command in ("/clear", "/new"):
            app = self.make_app()
            async with app.run_test() as pilot:
                await self.send(pilot, app, f"plan the wall jump before {command}")
                self.assertTrue(app.sessions)
                await self.send(pilot, app, command)
                body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
                self.assertNotIn("wall jump", body)  # Nothing from the earlier conversation is left on screen.
                self.assertIn("do the thing.", body)  # The screen looks as it did when Donut opened.
                self.assertEqual((app.sessions, app.last_answer), ({}, ""))
                await self.send(pilot, app, "plan sounds")
        titles = [record["title"] for record in conversations.for_project(self.temp.name)]
        self.assertEqual(titles.count("plan sounds"), 2)  # Each fresh start became its own conversation.
        self.assertEqual(len(titles), 4)

    async def test_each_window_plans_and_works_in_its_own_tickets_session(self):
        from donut import conversations, workspace

        app = self.make_app()
        self.assertRegex(app.session_name, r"^\d{8}-\d{4}")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            folder = f".donut/{app.session_name}/"
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("tickets " + folder, body)
            await self.send(pilot, app, "plan the wall jump")
            planner = next(iter(app.sessions.values()))
            self.assertIn(folder + "to_do/", planner.calls[-1])
            self.assertEqual(conversations.for_project(self.temp.name)[0]["session"], app.session_name)
            launched = []

            async def capture(*args, **kwargs):
                launched.append(args)
                raise RuntimeError("not starting a worker in tests")

            with patch("donut.terminal.asyncio.create_subprocess_exec", capture):
                await self.send(pilot, app, "/work next 1")
            self.assertEqual(launched[0][launched[0].index("--session") + 1], app.session_name)
            first = app.session_name
            workspace.create(app.project, first)  # What the (patched out) install does on a real /plan.
            await self.send(pilot, app, "/session new")
            self.assertNotEqual(app.session_name, first)
            self.assertTrue((Path(self.temp.name) / ".donut" / app.session_name / "to_do").is_dir())
            await self.send(pilot, app, "/session")
            menu = app.query_one(Menu)
            self.assertEqual([row[0] for row in menu.visible_rows][0], "New session")
            self.assertIn(first, [row[0] for row in menu.visible_rows])
            await pilot.press("escape")
            await pilot.pause()
            await self.send(pilot, app, f"/session {first}")
            self.assertEqual(app.session_name, first)
            self.assertEqual(workspace.relative(app.project, app.lanes), f".donut/{first}/")

    async def test_resume_switches_back_to_the_conversations_tickets_session(self):
        from donut import conversations

        conversations.record_turn(
            None, self.temp.name, "plan", "plan", "claude", "claude-opus-5-5", "sid", "jump", "ok", session="older"
        )
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.execute("/resume")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertEqual(app.session_name, "older")

    async def test_project_opens_another_project_in_a_new_window(self):
        from donut import conversations

        other = Path(self.temp.name) / "other-app"
        other.mkdir()
        conversations.remember_project(other)
        app = self.make_app()
        with patch("donut.terminal.open_window", return_value="a new Terminal window") as opened:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self.send(pilot, app, "/project")
                menu = app.query_one(Menu)
                self.assertEqual([row[0] for row in menu.visible_rows], ["other-app"])  # Not this project.
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertEqual(opened.call_args.args[0], other.resolve())
                body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
                self.assertIn("Opened other-app in a new Terminal window.", body)
                opened.return_value = None
                await self.send(pilot, app, f"/project {other}")  # Typed as is, backslashes and all on Windows.
                body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
                self.assertIn("No terminal app found", body)
                self.assertIn(str(other.resolve()), body)
                with patch.object(app, "open_project", new_callable=AsyncMock) as open_project:
                    await self.send(pilot, app, r"/project C:\Users\me\my app")
                open_project.assert_called_with(r"C:\Users\me\my app")  # Backslashes and spaces kept.

    async def test_streamed_updates_become_separate_paragraphs_in_the_transcript(self):
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.turn_text, app.shown = "", 0
            app.verbosity = "verbose"  # The level that keeps every message as it is written.
            for piece in ("Status: found the job.", "\n\nConfirmed: it is slow.", "\n\n```sql\nSELECT 1\n\nFROM t\n"):
                app.event("text", piece)
            await pilot.pause()
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("Status: found the job.", body)
            self.assertIn("Confirmed: it is slow.", body)
            self.assertNotIn("SELECT", body)  # A code block is never split at its blank line.
            self.assertIn("SELECT 1", app.turn_text[app.shown :])  # Still being written, above the prompt.
            app.answer()
            await pilot.pause()
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("FROM t", body)
            self.assertEqual(body.count("Status: found the job."), 1)  # Committed once, not again at the end.
            self.assertTrue(app.last_answer.startswith("Status: found the job.\n\nConfirmed"))
            self.assertEqual(app.shown, len(app.turn_text))

    async def test_links_are_clickable(self):
        from donut.terminal import link_markdown, markdown

        self.assertEqual(
            link_markdown("See https://github.com/x/y/pull/42. Also [PR](https://a.b/c) and <https://d.e>"),
            "See <https://github.com/x/y/pull/42>. Also [PR](https://a.b/c) and <https://d.e>",
        )
        code = "Run `curl https://a.b/c` then\n```\nhttps://in.code/block\n```\n"
        self.assertEqual(link_markdown(code), code)  # Code stays exactly as written.
        self.assertTrue(markdown("x").hyperlinks)
        app = self.make_app()
        with patch.object(app, "open_url") as opened:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                log = app.query_one(RichLog)
                log.clear()
                app.say("PR: https://github.com/example/project/pull/42")
                app.say(markdown("Merged https://github.com/x/y/pull/2 today."))
                await pilot.pause()
                await pilot.click(RichLog, offset=(12, 0))  # Inside the first URL (the log has 1 column of padding).
                await pilot.pause()
                opened.assert_called_with("https://github.com/example/project/pull/42")
                await pilot.click(RichLog, offset=(14, 1))
                await pilot.pause()
                opened.assert_called_with("https://github.com/x/y/pull/2")
                calls = opened.call_count
                await pilot.click(RichLog, offset=(2, 0))  # "PR:" is not a link.
                await pilot.pause()
                self.assertEqual(opened.call_count, calls)

    async def test_selected_text_is_copied(self):
        app = self.make_app()
        with patch("donut.terminal.copy_native") as native, patch.object(app, "copy_to_clipboard") as terminal:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                log = app.query_one(RichLog)
                log.clear()
                app.say("Output line one")
                app.say("PR https://github.com/x/y/pull/9")
                await pilot.pause()
                await pilot.mouse_down(RichLog, offset=(1, 0))
                await pilot.hover(RichLog, offset=(7, 1))
                await pilot.mouse_up(RichLog, offset=(7, 1))
                await pilot.pause()
                native.assert_called_with("Output line one\nPR http")  # Copied as soon as the mouse is released.
                terminal.assert_called_with("Output line one\nPR http")
                native.reset_mock()
                await pilot.press("ctrl+c")  # With a selection, Ctrl+C copies instead of cancelling.
                await pilot.pause()
                native.assert_called_with("Output line one\nPR http")

    async def test_bang_runs_a_shell_command_and_the_model_sees_it(self):
        import sys

        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, app, f'!"{sys.executable}" -c "print(\'hello from the shell\')"')
            await pilot.pause(0.5)
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("hello from the shell", body)
            self.assertIn("exit 0", body)
            self.assertEqual(self.opened, [])  # No model was involved.
            await self.send(pilot, app, "what did that print?")
            planner = next(iter(app.sessions.values()))
            self.assertIn("Shell commands I ran in the project before this message", planner.calls[-1])
            self.assertIn("hello from the shell", planner.calls[-1])
            self.assertTrue(planner.calls[-1].rstrip().endswith("what did that print?"))
            await self.send(pilot, app, "and now?")
            self.assertNotIn("Shell commands I ran", planner.calls[-1])  # Given once.

    async def test_shell_output_leaves_the_transcript_at_its_end(self):
        import sys

        app = self.make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self.send(pilot, app, f'!"{sys.executable}" -c "[print(i) for i in range(200)]"')
            log = app.query_one(RichLog)
            for _ in range(100):
                await pilot.pause(0.05)
                if not app.busy and "exit 0" in log.lines[-1].text:
                    break
            await pilot.pause(0.1)
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            # When the turn ends, the usage line refreshes and can grow, which shrinks the transcript.
            app.query_one("#usage", Static).update("claude 5h\ncodex 5h\nqwen\nmore")
            await pilot.pause(0.2)
            self.assertGreater(log.max_scroll_y, 0)
            self.assertEqual(log.scroll_y, log.max_scroll_y)  # The exit line is still in view.

    async def test_a_model_waiting_on_a_command_is_woken_when_it_exits(self):
        import sys

        app = self.make_app()
        replies = iter(
            [f'Started the build.\n\nDONUT-WAIT: "{sys.executable}" -c "print(\'build ok\')"', "The build passed."]
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.permission = "full"
            await self.send(pilot, app, "build it")
            planner = next(iter(app.sessions.values()))
            planner.donut_waits = True
            original = planner.query

            async def query(text):
                planner.calls.append(text)
                app.event("text", next(replies))

            planner.query = query
            await self.send(pilot, app, "build it again")
            for _ in range(100):
                if any("build ok" in call for call in planner.calls):
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            wake = planner.calls[-1]
            self.assertIn("exited with code 0", wake)
            self.assertIn("build ok", wake)
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("waking", body)
            self.assertIn("The build passed.", body)
            self.assertFalse(app.busy)
            planner.query = original

    async def test_a_turn_claude_starts_itself_is_shown(self):
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "start the deploy")
            session = next(iter(app.sessions.values()))
            app.event("background", "Background command finished", None, session)
            app.event("wake_start", "", None, session)
            self.assertTrue(app.busy)
            app.event("text", "The deploy finished; all checks green.", None, session)
            app.event("wake_end", "", None, session)
            await pilot.pause()
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("woken by a background task", body)
            self.assertIn("The deploy finished; all checks green.", body)
            self.assertFalse(app.busy)

    async def test_verbosity_changes_what_is_shown_never_what_is_sent(self):
        def turn(app):
            app.turn_text, app.shown, app.message_start = "", 0, 0
            app.event("thinking", "The user wants the status. ")
            app.event("thinking", "Check git first.")
            app.event("text", "Status: reading the repo.")
            app.event("tool", {"id": "t1", "name": "Bash", "input": {"command": "git status --short"}})
            app.event("tool_result", {"id": "t1", "output": " M a.py\n M b.py", "error": False})
            app.event("break", "")
            app.event("text", "Two files changed.")
            app.answer()

        seen = {}
        for level in ("quiet", "normal", "verbose", "full"):
            app = self.make_app()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self.send(pilot, app, f"/verbosity {level}")
                log = app.query_one(RichLog)
                log.clear()
                turn(app)
                await pilot.pause()
                seen[level] = "\n".join(strip.text for strip in log.lines)
                self.assertEqual(app.turn_text, "Status: reading the repo.\n\nTwo files changed.")  # Unchanged.
        self.assertNotIn("Status: reading", seen["quiet"])
        self.assertIn("Two files changed.", seen["quiet"])
        self.assertNotIn("Status: reading", seen["normal"])  # A progress note: shown while it works, not kept.
        self.assertIn("Two files changed.", seen["normal"])
        self.assertIn("Status: reading", seen["verbose"])
        self.assertNotIn("⏺ Bash", seen["normal"])
        self.assertNotIn("Check git", seen["normal"])
        self.assertIn("⏺ Bash  git status --short", seen["verbose"])
        self.assertIn("⎿ 2 lines", seen["verbose"])
        self.assertNotIn("M a.py", seen["verbose"])
        self.assertIn("✻ The user wants the status. Check git first.", seen["full"])
        self.assertIn("M a.py", seen["full"])
        from donut.config import load

        self.assertEqual(load().get("verbosity"), "full")  # Saved for next time.

    async def test_focus_toggles_quiet_instead_of_reaching_the_provider(self):
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self.send(pilot, app, "/focus")
            self.assertEqual(app.verbosity, "quiet")
            self.assertFalse(app.query_one("#status").display)
            self.assertFalse(app.query_one("#band").display)
            await self.send(pilot, app, "/focus")
            self.assertEqual(app.verbosity, "normal")
            await self.send(pilot, app, "/focus on")
            self.assertEqual(app.verbosity, "quiet")

    async def test_the_transcript_stays_at_the_bottom_while_a_long_reply_streams(self):
        app = self.make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(RichLog)
            for number in range(80):
                app.say(f"earlier line {number}")
            await pilot.pause()
            app.turn_text, app.shown, app.message_start = "", 0, 0
            for number in range(10):
                app.event("text", f"word {number} " * 30 + "\n")  # One long paragraph, still being written.
                await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            response = app.query_one("#response")
            self.assertEqual(response.scroll_y, response.max_scroll_y)  # Shows the newest lines.
            app.answer()
            await pilot.pause(0.2)
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            # Scrolled up to read: new output does not pull the view back down.
            log.scroll_relative(y=-10, animate=False)
            log.on_mouse_scroll_up(None)  # What a mouse wheel scroll up calls.
            await pilot.pause()
            self.assertFalse(app.follow)
            where = log.scroll_y
            app.say("a new line")
            await pilot.pause(0.2)
            self.assertEqual(log.scroll_y, where)
            # Back at the end, however the view got there: new output is followed again.
            log.scroll_end(animate=False)
            await pilot.pause(0.2)
            self.assertTrue(app.follow)
            app.say("another line")
            await pilot.pause(0.2)
            self.assertEqual(log.scroll_y, log.max_scroll_y)

    async def test_renaming_the_session_renames_its_tickets_folder(self):
        from donut import conversations, workspace

        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            first = app.session_name
            await self.send(pilot, app, "plan the wall jump")
            workspace.create(app.project, first)  # What the (patched out) install does on a real /plan.
            planner = next(iter(app.sessions.values()))

            async def query(text):
                planner.calls.append(text)
                app.event("text", "Session renamed to: fix login bug")

            planner.query = query
            # The planner is Claude: its own /rename names the session, and the folder follows.
            await self.send(pilot, app, "/rename")
            self.assertEqual(planner.calls[-1], "/rename")
            self.assertEqual(app.session_name, "fix-login-bug")
            self.assertTrue((Path(self.temp.name) / ".donut" / "fix-login-bug" / "to_do").is_dir())
            self.assertFalse((Path(self.temp.name) / ".donut" / first).exists())
            self.assertEqual(conversations.for_project(self.temp.name)[0]["session"], "fix-login-bug")
            # /resume lists the conversation by its new name, with its first message beside it.
            await self.send(pilot, app, "/resume")
            row = app.query_one(Menu).visible_rows[0]
            self.assertEqual(row[0], "fix login bug")
            self.assertTrue(row[2].startswith("plan the wall jump · plan · "))
            await pilot.press("escape")
            await pilot.pause()
            # The soloist is Codex, which has no /rename: Donut names the folder itself.
            await self.send(pilot, app, "/solo")
            await self.send(pilot, app, "/rename Wall jump: v2")
            self.assertEqual(app.session_name, "Wall-jump-v2")
            self.assertEqual(len(planner.calls), 2)  # No model was asked.

    async def test_the_session_name_titles_the_window_and_shows_in_the_status_line(self):
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            written = []
            original = app._driver.write
            app._driver.write = lambda data: (written.append(data), original(data))
            app.rename_session("fix login bug")
            await pilot.pause()
            project = Path(self.temp.name).name
            self.assertIn((t := f"fix-login-bug · {project} · Donut", f"\x1b]1;{t}\x07\x1b]2;{t}\x07")[1], written)
            status = str(app.query_one("#status").render())
            self.assertIn("session fix-login-bug", status)
            written.clear()
            app.status()
            self.assertEqual(written, [])  # Only written when it changes.
            app.session_name = "evil\x07\x1b]0;x"
            app.show_title()
            self.assertNotIn("\x1b]0;x", written[-1])  # Control characters never reach the terminal.
            self.assertEqual(written[-1].count("\x07"), 2)  # Only the two terminators, none inside a title.
            self.assertEqual(written[-1].count("\x1b"), 2)

    async def test_next_and_prev_list_this_sessions_tickets(self):
        from donut import workspace

        app = self.make_app()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            lanes = workspace.create(app.project, app.session_name)
            ticket = "# {title}\n\nAbout {title}.\n\n**Slug:** `{slug}`\n**Depends on:** None\n"
            (lanes / "done" / "001_a.md").write_text(ticket.format(title="Old one", slug="a"))
            (lanes / "done" / "002_b.md").write_text(
                ticket.format(title="Latest one", slug="b") + "PR: https://github.com/o/r/pull/7\n"
            )
            (lanes / "to_do" / "003_c.md").write_text(ticket.format(title="Coming up", slug="c"))

            async def shown(command):
                app.query_one(RichLog).clear()
                app.execute(command)
                await pilot.pause(0.3)
                return "\n".join(strip.text for strip in app.query_one(RichLog).lines)

            body = await shown("/prev 1")
            self.assertIn("Latest one", body)
            self.assertIn("https://github.com/o/r/pull/7", body)
            self.assertNotIn("Old one", body)
            self.assertNotIn("Coming up", body)
            body = await shown("/prev")
            self.assertLess(body.index("Latest one"), body.index("Old one"))  # Latest first.
            body = await shown("/next")
            self.assertIn("Coming up", body)
            self.assertNotIn("Latest one", body)
            self.assertIn("COUNT is a positive number", await shown("/next zero"))

    async def test_the_first_chat_message_names_the_window(self):
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            written = []
            original = app._driver.write
            app._driver.write = lambda data: (written.append(data), original(data))
            project = Path(self.temp.name).name
            for text in ("/foo", "   ", "!ls"):
                app.name_task(text)
            self.assertEqual(app.task_title, "")
            folder = app.session_name
            app.name_task("fix the login bug on mobile")
            self.assertIn((t := f"fix the login bug on mobile · {project} · Donut", f"\x1b]1;{t}\x07\x1b]2;{t}\x07")[1], written)
            written.clear()
            app.name_task("something else")
            self.assertEqual(written, [])
            self.assertEqual(app.session_name, folder)
            app.rename_session("x y")
            app.show_title()
            t = f"x-y · {project} · Donut"
            self.assertEqual(written[-1], f"\x1b]1;{t}\x07\x1b]2;{t}\x07")

    async def test_running_background_shells_show_in_the_status_line(self):
        app = self.make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.event("task", {"id": "t1", "state": "running", "kind": "shell", "description": "Poll the build"})
            app.event("task", {"id": "t2", "state": "running", "kind": "shell", "description": "Watch CI"})
            app.event("task", {"id": "a1", "state": "running", "kind": "agent", "description": "Explore"})
            await pilot.pause()
            self.assertIn("⏳ 2 shells running · 1 agent running", str(app.query_one("#status").render()))
            # Each one has its own line above the prompt, with what it is and how long it has run.
            running = app.query_one("#background")
            self.assertTrue(running.has_class("-running"))
            shown = str(running.render())
            self.assertIn("⏳ shell running  Poll the build · 0s", shown)
            self.assertIn("Still working in the background", shown)  # Between turns, it says the work goes on.
            # A turn that ends with no words says why, so it does not look like a hang.
            app.turn_text, app.shown, app.message_start = "", 0, 0
            app.answer()
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("No reply yet · 2 shells running · 1 agent running", body)
            self.assertIn("⏳ agent running  Explore", shown)
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertEqual(body.count("Background shell started  Poll the build"), 1)
            app.event("task", {"id": "t1", "state": "completed"})
            app.event("task", {"id": "t1", "state": "completed"})  # Reported twice: counted once.
            app.event("task", {"id": "a1", "state": "killed"})
            await pilot.pause()
            self.assertIn("⏳ 1 shell running", str(app.query_one("#status").render()))
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertEqual(body.count("Background shell completed  Poll the build"), 1)
            self.assertIn("Background agent killed  Explore", body)
            self.assertNotIn("Poll the build", str(running.render()))
            app.event("task", {"id": "t2", "state": "completed"})
            await pilot.pause()
            self.assertFalse(running.has_class("-running"))  # Nothing left: the line goes away.
            app.turn_text, app.shown, app.message_start = "", 0, 0
            app.answer()
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("The model ended its turn without a reply.", body)

    async def test_the_cursor_blinks_only_in_the_active_window(self):
        from textual import events

        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            draft = app.query_one(Draft)
            self.assertTrue(draft.cursor_blink)
            app.post_message(events.AppBlur())  # The terminal reports another window came to the front.
            await pilot.pause()
            self.assertFalse(draft.cursor_blink)
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertTrue(draft.cursor_blink)

    async def test_resume_with_nothing_saved(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.execute("/resume")
            await pilot.pause()
            self.assertFalse(app.query_one(Menu).is_open)
            body = "\n".join(strip.text for strip in app.query_one(RichLog).lines)
            self.assertIn("No earlier conversations in this project yet.", body)
