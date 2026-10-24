"""Regression tests for basecamp-tui. Stdlib only: python3 -m unittest discover tests

The script is loaded as a module; the `basecamp` CLI, curses and the editor are faked.
"""
import base64
import curses
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "basecamp-tui"
_loader = importlib.machinery.SourceFileLoader("basecamp_tui", str(SCRIPT))
_spec = importlib.util.spec_from_loader("basecamp_tui", _loader)
bt = importlib.util.module_from_spec(_spec)
_loader.exec_module(bt)


# ---------------------------------------------------------------- fakes

def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def gid(rid):
    return base64.urlsafe_b64encode(f"gid://bc3/Recording/{rid}".encode()).decode().rstrip("=")


def notif(nid, path="todos/100", bucket=10, read=False, **kw):
    n = {"id": nid, "title": f"Item {nid}", "bucket_name": "Proj", "type": "Comment",
         "app_url": f"https://app.basecamp.com/1/buckets/{bucket}/{path}",
         "read_at": ago(hours=1) if read else None, "readable_sgid": f"sg{nid}",
         "unread_url": f"https://3.basecampapi.com/1/my/unreads/{nid}.json",
         "created_at": ago(hours=2), "content_excerpt": f"excerpt {nid}"}
    n.update(kw)
    return n


class FakeBC:
    """Stands in for bt.bc: routes on the longest matching arg prefix, records calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        best = None
        for prefix in self.routes:
            if args[:len(prefix)] == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            return None
        res = self.routes[best]
        if isinstance(res, Exception):
            raise res
        return res(*args) if callable(res) else res

    def called(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class FakeScr:
    def __init__(self, h=30, w=100, keys=()):
        self.h, self.w = h, w
        self.keys = list(keys)
        self.rows = {}
        self.cursor = None
        self.idle = 0

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, s, attr=0):
        if not (0 <= y < self.h and 0 <= x < self.w):
            raise curses.error
        row = self.rows.setdefault(y, [" "] * self.w)
        for i, ch in enumerate(s):
            if x + i >= self.w:
                raise curses.error
            row[x + i] = ch

    def text(self):
        return "\n".join("".join(self.rows.get(y, [])).rstrip() for y in range(self.h))

    def erase(self):
        self.rows = {}

    def get_wch(self):
        """An exhausted queue reads like a timeout; a loop that keeps reading fails loudly."""
        if not self.keys:
            self.idle += 1
            if self.idle > 100:
                raise RuntimeError("FakeScr ran out of keys")
            raise curses.error
        k = self.keys.pop(0)
        if isinstance(k, Exception):
            raise k
        return k

    def move(self, y, x):
        self.cursor = (y, x)

    def refresh(self):
        pass

    def clear(self):
        self.rows = {}

    def timeout(self, t):
        pass

    def keypad(self, on):
        pass


CURSES_NOOPS = ["start_color", "use_default_colors", "init_pair", "curs_set",
                "def_prog_mode", "endwin", "reset_prog_mode"]


class SyncApp(bt.App):
    """Background work runs inline; results still go through the queue + drain()."""

    def bg(self, fn, *args, tag=None):
        try:
            self.results.put((tag, args, fn(*args), None))
        except Exception as e:  # noqa: BLE001
            self.results.put((tag, args, None, e))


class Base(unittest.TestCase):
    def setUp(self):
        for name in CURSES_NOOPS:
            p = mock.patch.object(bt.curses, name, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(bt.curses, "color_pair", lambda i: i << 8)
        p.start()
        self.addCleanup(p.stop)
        self.fake = FakeBC()
        p = mock.patch.object(bt, "bc", self.fake)
        p.start()
        self.addCleanup(p.stop)
        out = mock.patch.object(bt.sys, "stdout", new=FakeStdout())
        self.stdout = out.start()
        self.addCleanup(out.stop)
        bt._projects.clear()
        bt._people.clear()
        self.addCleanup(bt._projects.clear)
        self.addCleanup(bt._people.clear)

    def app(self, items, h=30, w=100, keys=(), notes_content=""):
        self.fake.routes[("notifications", "list")] = lambda *a: (
            {"unreads": [n for n in items if bt.unread(n)], "reads": [n for n in items if not bt.unread(n)]}
            if a[3] == "1" else {})
        self.fake.routes[("api", "get", "/my/notes.json")] = lambda *a: {
            "content": notes_content, "app_url": "https://app.basecamp.com/1/notes"}
        scr = FakeScr(h, w, keys)
        app = SyncApp(scr)
        app.drain()
        return app


class FakeStdout:
    def __init__(self):
        self.out = []

    def write(self, s):
        self.out.append(s)

    def flush(self):
        pass


# ---------------------------------------------------------------- CLI wrapper

class TestBC(unittest.TestCase):
    def run_bc(self, stdout="", stderr="", code=0, args=("show", "1")):
        proc = subprocess.CompletedProcess([], code, stdout, stderr)
        with mock.patch.object(bt.subprocess, "run", return_value=proc) as run:
            return bt.bc(*args), run

    def test_unwraps_data_and_appends_json_flag(self):
        res, run = self.run_bc(json.dumps({"ok": True, "data": {"id": 5}}))
        self.assertEqual(res, {"id": 5})
        self.assertEqual(run.call_args[0][0][-1], "--json")

    def test_plain_payload_and_empty_output(self):
        self.assertEqual(self.run_bc(json.dumps([1, 2]))[0], [1, 2])
        self.assertEqual(self.run_bc("")[0], {})

    def test_non_json_output_raises_stderr(self):
        with self.assertRaisesRegex(bt.BCError, "boom"):
            self.run_bc("not json", "boom")

    def test_ok_false_raises_error_message(self):
        with self.assertRaisesRegex(bt.BCError, "nope"):
            self.run_bc(json.dumps({"ok": False, "error": "nope"}))

    def test_nonzero_exit_falls_back_to_stderr_then_code(self):
        with self.assertRaisesRegex(bt.BCError, "bad auth"):
            self.run_bc("", "bad auth", 1)
        with self.assertRaisesRegex(bt.BCError, "exit 3"):
            self.run_bc("", "", 3)

    def test_api_empty_body_is_success(self):
        out = json.dumps({"ok": False, "error": "failed: unexpected end of JSON input"})
        self.assertIsNone(self.run_bc(out, code=1, args=("api", "put", "/x"))[0])

    def test_empty_body_on_non_api_command_is_still_an_error(self):
        # `comment` on a question returns an empty body and does NOT post; never swallow it
        out = json.dumps({"ok": False, "error": "failed to comment: unexpected end of JSON input"})
        with self.assertRaises(bt.BCError):
            self.run_bc(out, code=1, args=("comment", "1", "hi"))

    def test_api_helpers(self):
        self.assertEqual(bt.api_path("https://3.basecampapi.com/4538402/my/unreads/9.json"), "/my/unreads/9.json")
        fake = FakeBC({("notifications", "list"): None})
        with mock.patch.object(bt, "bc", fake):
            self.assertEqual(bt.fetch_page(2), {})
            bt.mark_read(["a", "b"])
            bt.mark_unread({"unread_url": "https://3.basecampapi.com/1/my/unreads/7.json"})
        self.assertEqual(fake.calls[0], ("notifications", "list", "--page", "2"))
        self.assertEqual(json.loads(fake.calls[1][4]), {"readables": ["a", "b"]})
        self.assertEqual(fake.calls[2][:3], ("api", "post", "/my/unreads/7.json"))


# ---------------------------------------------------------------- targets

class TestParseTarget(unittest.TestCase):
    def test_todo_with_comment_anchor(self):
        n = notif(1, "todos/100#__recording_555")
        self.assertEqual(bt.parse_target(n), {"bucket": "10", "kind": "todos", "id": "100", "focus": "555"})

    def test_card_kind_has_a_slash(self):
        t = bt.parse_target(notif(1, "card_tables/cards/77@88"))
        self.assertEqual((t["kind"], t["id"], t["focus"]), ("card_tables/cards", "77", "88"))

    def test_unparseable_and_checkin_reminder(self):
        self.assertIsNone(bt.parse_target({"app_url": "https://app.basecamp.com/my/notes"}))
        n = {"app_url": "https://app.basecamp.com/1/questions/answers/entries/3"}
        self.assertEqual(bt.parse_target(n)["kind"], "checkin")

    def test_pinned_target_wins(self):
        n = notif(1, _target={"kind": "x"})
        self.assertEqual(bt.parse_target(n), {"kind": "x"})

    def test_answer_notification_targets_the_answer_not_the_question(self):
        # regression: replies to "Answer to …" went to the question and failed
        n = notif(1, "questions/10370826216", readable_identifier=gid(10439128014))
        t = bt.parse_target(n)
        self.assertEqual((t["kind"], t["id"]), ("question_answers", "10439128014"))

    def test_question_notification_stays_on_question(self):
        n = notif(1, "questions/42", readable_identifier=gid(42))
        self.assertEqual(bt.parse_target(n)["kind"], "questions")
        self.assertEqual(bt.parse_target(notif(2, "questions/42"))["id"], "42")

    def test_readable_identifier_garbage(self):
        self.assertIsNone(bt.readable_recording_id({"readable_identifier": "!!!"}))
        self.assertIsNone(bt.readable_recording_id({"readable_identifier": base64.b64encode(b"\xff\xfe").decode()}))
        self.assertIsNone(bt.readable_recording_id({"readable_identifier": gid("x")}))

    def test_item_kind(self):
        self.assertEqual(bt.item_kind(notif(1, "todos/1")), "todo")
        self.assertEqual(bt.item_kind(notif(1, "card_tables/cards/1")), "card")
        self.assertIsNone(bt.item_kind(notif(1, "messages/1")))
        self.assertIsNone(bt.item_kind({"app_url": ""}))


class TestResolveCheckin(Base):
    def test_matches_question_by_title_and_pins_it(self):
        self.fake.routes.update({
            ("projects", "list"): [{"name": "Proj", "id": 10}],
            ("checkins", "questions"): [{"id": 1, "title": "Other"}, {"id": 2, "title": "What's up?"}],
        })
        n = {"bucket_name": "Proj", "title": "What's up?"}
        t = bt.resolve_checkin(n)
        self.assertEqual((t["bucket"], t["id"], t["checkin"]), ("10", "2", True))
        self.assertIs(n["_target"], t)

    def test_falls_back_to_first_question(self):
        self.fake.routes.update({("projects", "list"): [{"name": "Proj", "id": 10}],
                                 ("checkins", "questions"): [{"id": 1, "title": "Other"}]})
        self.assertEqual(bt.resolve_checkin({"bucket_name": "Proj", "title": "?"})["id"], "1")

    def test_errors(self):
        self.fake.routes[("projects", "list")] = [{"name": "Proj", "id": 10}]
        with self.assertRaisesRegex(bt.BCError, "no project"):
            bt.resolve_checkin({"bucket_name": "Nope"})
        self.fake.routes[("checkins", "questions")] = []
        with self.assertRaisesRegex(bt.BCError, "no check-in"):
            bt.resolve_checkin({"bucket_name": "Proj"})

    def test_project_created_after_startup_is_found(self):
        projects = [{"name": "Old", "id": 1}]
        self.fake.routes.update({("projects", "list"): lambda *a: list(projects),
                                 ("checkins", "questions"): [{"id": 9, "title": "q"}]})
        bt.resolve_checkin({"bucket_name": "Old", "title": "q"})
        projects.append({"name": "New", "id": 2})
        self.assertEqual(bt.resolve_checkin({"bucket_name": "New", "title": "q"})["bucket"], "2")


# ---------------------------------------------------------------- thread loading

class TestLoadThread(Base):
    def test_note(self):
        n = bt.note_item("buy milk", "u")
        self.assertEqual(bt.load_thread(n)[1], ("body", "buy milk"))

    def test_no_target_uses_excerpt(self):
        self.assertEqual(bt.load_thread({"app_url": "", "content_excerpt": "hi"}), [("dim", "hi")])
        self.assertEqual(bt.load_thread({"app_url": ""}), [("dim", "(no preview)")])

    def test_chat_lines_sorted_with_focus_and_files(self):
        self.fake.routes[("api", "get")] = [
            {"id": 2, "created_at": "2026-01-02", "creator": {"name": "B"}, "content": "second",
             "attachments": [{"filename": "a.png"}]},
            {"id": 1, "created_at": "2026-01-01", "creator": {"name": "A"}, "content": "<p>first</p>"},
        ]
        blocks = bt.load_thread(notif(1, "chats/5@2"))
        self.assertEqual(blocks[0][0], "author")
        self.assertEqual(blocks[1], ("body", "first"))
        self.assertTrue(blocks[2][1].startswith("▶ B"))
        self.assertEqual(blocks[3], ("body", "second\n[📎 a.png]"))

    def test_empty_chat(self):
        self.assertEqual(bt.load_thread(notif(1, "chats/5")), [("dim", "(empty chat)")])

    def test_todo_with_comments_and_focus(self):
        self.fake.routes.update({
            ("show",): {"type": "Todo", "title": "Fix it", "description": "<p>details</p>",
                        "creator": {"name": "Ori"}, "created_at": ago(minutes=5), "due_on": "2026-11-01",
                        "assignees": [{"name": "Ann"}], "completed": True, "comments_count": 2},
            ("comments", "list"): [{"id": 7, "creator": {"name": "Ann"}, "content": "done?"},
                                   {"id": 8, "creator": {"name": "Ori"}, "content": "yes"}, "junk"],
        })
        blocks = bt.load_thread(notif(1, "todos/100#__recording_8"))
        self.assertEqual(blocks[0], ("title", "Fix it"))
        self.assertIn("due 2026-11-01", blocks[1][1])
        self.assertIn("→ Ann", blocks[1][1])
        self.assertIn("✓ completed", blocks[1][1])
        self.assertEqual(blocks[2], ("body", "details"))
        self.assertIn(("focus", "▶ Ori · "), blocks)

    def test_body_equal_to_title_is_not_repeated_and_no_comments(self):
        self.fake.routes[("show",)] = {"type": "Message", "subject": "Hi", "content": "Hi", "comments_count": 0}
        self.assertEqual(len(bt.load_thread(notif(1, "messages/3"))), 2)

    def test_comment_errors_are_ignored(self):
        self.fake.routes.update({("show",): {"type": "Message", "comments_count": 1},
                                 ("comments", "list"): bt.BCError("x")})
        self.assertEqual(len(bt.load_thread(notif(1, "messages/3"))), 2)

    def test_question_lists_latest_answers(self):
        answers = [{"id": i, "created_at": f"2026-01-{i:02d}", "creator": {"name": f"P{i}"}, "content": f"a{i}"}
                   for i in range(1, 20)]
        self.fake.routes.update({("show",): {"type": "Question", "title": "Q"},
                                 ("api", "get"): answers})
        blocks = bt.load_thread(notif(1, "questions/4"))
        bodies = [t for s, t in blocks if s == "body"]
        self.assertEqual(len(bodies), 15)
        self.assertEqual(bodies[-1], "a19")

    def test_question_answers_error(self):
        self.fake.routes.update({("show",): {"type": "Question", "title": "Q"}, ("api", "get"): bt.BCError("x")})
        self.assertEqual(len(bt.load_thread(notif(1, "questions/4"))), 2)

    def test_show_failure(self):
        self.fake.routes[("show",)] = bt.BCError("gone")
        blocks = bt.load_thread(notif(1, "messages/3"))
        self.assertEqual(blocks[-1], ("err", "(could not load: gone)"))

    def test_checkin_reminder_resolves_question(self):
        self.fake.routes.update({("projects", "list"): [{"name": "Proj", "id": 10}],
                                 ("checkins", "questions"): [{"id": 4, "title": "Item 1"}],
                                 ("show",): {"type": "Question", "title": "Q"}, ("api", "get"): []})
        n = notif(1, app_url="https://app.basecamp.com/1/questions/answers/entries/3")
        self.assertEqual(bt.load_thread(n)[0], ("title", "Q"))
        self.assertEqual(self.fake.called("show")[0][1], "4")


# ---------------------------------------------------------------- mentions

PEOPLE = [{"name": "Jane Smith", "attachable_sgid": "J"}, {"name": "Jane Doe", "attachable_sgid": "D"},
          {"name": "Bob Stone", "attachable_sgid": "B"}, {"name": "Rob <Admin>", "attachable_sgid": "R&"}]


class TestMentions(Base):
    def setUp(self):
        super().setUp()
        self.fake.routes[("people", "pingable")] = PEOPLE

    def test_people_is_cached(self):
        bt.people()
        bt.people()
        self.assertEqual(len(self.fake.called("people")), 1)

    def test_match_tiers(self):
        self.assertEqual([p["name"] for p in bt.match_person("jane.smith")], ["Jane Smith"])
        self.assertEqual(len(bt.match_person("jane")), 2)
        self.assertEqual([p["name"] for p in bt.match_person("bo")], ["Bob Stone"])
        self.assertEqual([p["name"] for p in bt.match_person("ston")], ["Bob Stone"])
        self.assertEqual(bt.match_person("zed"), [])

    def test_resolve_markdown_mentions(self):
        out, problems = bt.resolve_mentions("hey @Bob. and @jane.smith, mail a@b.com")
        self.assertEqual(problems, [])
        self.assertEqual(out, "hey [@Bob Stone](mention:B). and [@Jane Smith](mention:J), mail a@b.com")

    def test_raw_tag_for_checkins_is_escaped(self):
        out, _ = bt.resolve_mentions("@rob", raw_tag=True)
        self.assertIn('sgid="R&amp;"', out)
        self.assertIn("@Rob &lt;Admin&gt;</bc-attachment>", out)

    def test_problems(self):
        out, problems = bt.resolve_mentions("@jane @zed")
        self.assertEqual(out, "@jane @zed")
        self.assertIn("@jane is ambiguous: @Jane.Smith, @Jane.Doe", problems)
        self.assertIn("@zed matches nobody", problems)


# ---------------------------------------------------------------- claude sessions

class TestSessions(Base):
    def test_start_session_returns_id(self):
        proc = subprocess.CompletedProcess([], 0, "ok backgrounded · abc123\n", "")
        with mock.patch.object(bt.subprocess, "run", return_value=proc) as run:
            self.assertEqual(bt.start_session("name", "prompt"), "abc123")
        self.assertIn("--bg", run.call_args[0][0])

    def test_start_session_errors(self):
        for proc, msg in ((subprocess.CompletedProcess([], 1, "", "not trusted\nmore"), "not trusted"),
                          (subprocess.CompletedProcess([], 2, "", ""), "exit 2")):
            with mock.patch.object(bt.subprocess, "run", return_value=proc):
                with self.assertRaisesRegex(RuntimeError, msg):
                    bt.start_session("n", "p")

    def test_title_strips_notification_noise(self):
        n = notif(1, "messages/3", title="@mentioned you in: Re: Launch plan for Q4", bucket_name="Marketing")
        self.assertEqual(bt.session_title(n, None, "summarise"), "Launch plan for Q4 · summarise")

    def test_title_prefers_loaded_title_and_adds_project_when_short(self):
        n = notif(1, "messages/3", bucket_name="Ops")
        self.assertEqual(bt.session_title(n, [("title", "Updates")], "go"), "Updates (Ops) · go")

    def test_title_for_chat_and_tiny_subject(self):
        self.assertEqual(bt.session_title(notif(1, "chats/5", bucket_name="Dev chat"), None, "hi"),
                         "Dev chat · hi")
        self.assertEqual(bt.session_title(notif(1, "messages/3", title="ok", bucket_name="HQ"), None, "x"),
                         "HQ · x")


# ---------------------------------------------------------------- notes

class TestNotes(Base):
    def pad(self, content):
        store = {"content": content}
        self.fake.routes[("api", "get", "/my/notes.json")] = lambda *a: dict(store)
        self.fake.routes[("api", "put", "/my/notes.json")] = lambda *a: store.update(json.loads(a[4]))
        return store

    def test_note_texts(self):
        content = '<p>other</p><p dir="auto">📥 Inbox</p><ul><li>one</li><li>a &amp; b<br>c</li></ul>'
        self.assertEqual(bt.note_texts(content), ["one", "a & b\nc"])
        self.assertEqual(bt.note_texts(None), [])

    def test_add_keeps_rest_of_pad(self):
        store = self.pad("<p>mine</p>")
        bt.edit_notes(lambda ns: ns + ["x < y"])
        self.assertTrue(store["content"].startswith("<p>mine</p>"))
        self.assertEqual(bt.note_texts(store["content"]), ["x < y"])
        bt.edit_notes(lambda ns: ns + ["two"])
        self.assertEqual(bt.note_texts(store["content"]), ["x < y", "two"])

    def test_remove_last_note_drops_block(self):
        store = self.pad("<p>mine</p>")
        bt.edit_notes(lambda ns: ["only"])
        bt.remove_note("only")
        bt.remove_note("missing")
        self.assertEqual(store["content"], "<p>mine</p>")

    def test_remove_note_with_blank_lines_and_trailing_spaces(self):
        # the pad round-trip collapses blank lines / trailing spaces; the note must still clear
        store = self.pad("")
        text = "first line  \n\n\n\nsecond"
        bt.edit_notes(lambda ns: ns + [text])
        bt.remove_note(text)
        self.assertEqual(bt.note_texts(store["content"]), [])

    def test_note_item(self):
        n = bt.note_item("title\nbody", "url", 3)
        self.assertTrue(bt.is_note(n))
        self.assertEqual(n["title"], "title")
        self.assertFalse(bt.is_note(None))
        self.assertNotEqual(n["id"], bt.note_item("title\nbody", "url", 4)["id"])


# ---------------------------------------------------------------- done / archive

class TestResolveAction(Base):
    def test_todo_done_and_undo(self):
        self.fake.routes[("show",)] = {"title": "Ship"}
        desc, do, undo = bt.resolve_action(notif(1, "todos/100"), "done")
        self.assertEqual(desc, "completed “Ship”")
        do()
        undo()
        self.assertEqual(self.fake.calls[-2:], [("done", "100", "--project", "10"), ("reopen", "100", "--project", "10")])

    def test_todo_already_done_or_archived(self):
        self.fake.routes[("show",)] = {"title": "Ship", "completed": True, "status": "archived"}
        self.assertEqual(bt.resolve_action(notif(1), "done")[1:], (None, None))
        self.assertEqual(bt.resolve_action(notif(1), "archive")[0], "already archived: Ship")

    def test_todo_archive(self):
        self.fake.routes[("show",)] = {"title": "Ship"}
        desc, do, undo = bt.resolve_action(notif(1), "archive")
        do()
        undo()
        self.assertEqual([c[:2] for c in self.fake.calls[-2:]], [("recordings", "archive"), ("recordings", "restore")])

    def board(self, col_id=5, lists=None):
        self.fake.routes.update({
            ("show",): {"title": "Card", "parent": {"id": col_id, "title": "Doing"}},
            ("api", "get", "/buckets/10/card_tables/columns/5.json"): {"parent": {"id": 3, "title": "Board"}},
            ("api", "get", "/buckets/10/card_tables/3.json"): {"lists": lists if lists is not None else [
                {"id": 5, "title": "Doing", "type": "Kanban::Column"},
                {"id": 6, "title": "Done", "type": "Kanban::DoneColumn"},
                {"id": 7, "title": "Not now", "type": "Kanban::NotNowColumn"}]},
        })

    def test_card_moves_to_done_and_back(self):
        self.board()
        desc, do, undo = bt.resolve_action(notif(1, "card_tables/cards/9"), "done")
        self.assertEqual(desc, "moved “Card” Doing → Done")
        do()
        undo()
        self.assertEqual(self.fake.calls[-2:], [("cards", "move", "9", "--to", "6", "--in", "10"),
                                                ("cards", "move", "9", "--to", "5", "--in", "10")])

    def test_card_already_there_or_no_column(self):
        self.board()
        self.fake.routes[("show",)] = {"title": "Card", "parent": {"id": 7}}
        self.fake.routes[("api", "get", "/buckets/10/card_tables/columns/7.json")] = {"parent": {"id": 3}}
        self.assertEqual(bt.resolve_action(notif(1, "card_tables/cards/9"), "archive")[1], None)
        self.board(lists=[])
        with self.assertRaisesRegex(bt.BCError, "no Not now column"):
            bt.resolve_action(notif(1, "card_tables/cards/9"), "archive")


# ---------------------------------------------------------------- text utils

class TestText(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(bt.html_to_text(""), "")
        self.assertEqual(bt.html_to_text("a &amp; b "), "a & b")
        s = ('<div>Hi <a href="https://x.io">link</a> <a href="https://y.io">https://y.io</a></div>'
             "<ul><li>one<ul><li>two</li></ul></li></ul><img src=x>"
             '<bc-attachment content-type="application/vnd.basecamp.mention"><img> Jane\n Smith </bc-attachment>'
             '<bc-attachment filename="f.pdf"><figure>ignored</figure></bc-attachment>')
        out = bt.html_to_text(s)
        self.assertIn("Hi link <https://x.io> https://y.io\n", out)
        self.assertNotIn("https://y.io <", out)
        self.assertIn("• one", out)
        self.assertIn("  • two", out)
        self.assertIn("[image]@Jane Smith[📎 f.pdf]", out)
        self.assertNotIn("ignored", out)
        self.assertNotIn("\n\n\n", bt.html_to_text("<p>a</p><p></p><p></p><p>b</p>"))

    def test_widths_and_clip(self):
        self.assertEqual(bt.swidth("ab"), 2)
        self.assertEqual(bt.swidth("界"), 2)
        self.assertEqual(bt.swidth("é"), 1)
        self.assertEqual(bt.clip("a\tb界c", 4), ("a b", 3))
        self.assertEqual(bt.clip("👍️", 5)[0], "👍")

    def test_cw_without_libc(self):
        with mock.patch.object(bt, "_wcwidth", None):
            self.assertEqual(bt.cw("a"), 1)
            self.assertEqual(bt.cw("界"), 2)
            self.assertEqual(bt.cw("́"), 0)

    def test_wrap(self):
        self.assertEqual(bt.wrap("one two three", 7), ["one two", "three"])
        self.assertEqual(bt.wrap("a\n\nb", 5), ["a", "", "b"])
        self.assertEqual(bt.wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_wrap_never_hangs_on_tiny_widths(self):
        # a 2-column-wide char can't fit in 1 column; a zero/negative width can fit nothing
        for width in (1, 0, -3):
            lines = bt.wrap("界 abc", width)
            self.assertIn("abc", "".join(lines))

    def test_age(self):
        self.assertEqual(bt.age(None), "")
        self.assertEqual(bt.age("garbage"), "")
        self.assertEqual(bt.age(ago(minutes=5)), "5m")
        self.assertEqual(bt.age(ago(hours=3)), "3h")
        self.assertEqual(bt.age(ago(days=2)), "2d")
        self.assertEqual(bt.age(ago(minutes=-5)), "0m")
        self.assertRegex(bt.age("2020-03-01T10:00:00Z"), r"01 Mar")

    def test_age_without_timezone_does_not_crash(self):
        naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        self.assertEqual(bt.age(naive), "2h")

    def test_unread(self):
        self.assertTrue(bt.unread({"read_at": None}))
        self.assertTrue(bt.unread({"read_at": "0001-01-01T00:00:00Z"}))
        self.assertFalse(bt.unread({"read_at": "2026-01-01"}))


# ---------------------------------------------------------------- the app

class TestLoading(Base):
    def test_loads_pages_rewrites_api_links_and_appends_notes(self):
        pages = {"1": {"unreads": [notif(1, app_url="https://3.basecampapi.com/1/x")], "reads": [notif(2, read=True)]},
                 "2": {"reads": [notif(2, read=True), notif(3, read=True)]}, "3": {"reads": [notif(3, read=True)]}}
        self.fake.routes[("notifications", "list")] = lambda *a: pages.get(a[3], {})
        self.fake.routes[("api", "get", "/my/notes.json")] = {
            "content": "<p>📥 Inbox</p><ul><li>n1</li></ul>", "app_url": "https://notes"}
        app = SyncApp(FakeScr())
        app.drain()
        self.assertFalse(app.loading)
        self.assertEqual([n["id"] for n in app.items][:3], [1, 2, 3])
        self.assertEqual(app.items[0]["app_url"], "https://app.basecamp.com/1/x")
        self.assertTrue(bt.is_note(app.items[-1]))
        self.assertEqual(app.items[-1]["app_url"], "https://notes")
        self.assertEqual(len(self.fake.called("notifications")), 3)

    def test_load_failures_are_flashed(self):
        self.fake.routes[("notifications", "list")] = bt.BCError("offline")
        app = SyncApp(FakeScr())
        app.drain()
        self.assertIn("load failed: offline", app.status)

    def test_notes_failure_is_flashed(self):
        self.fake.routes[("api", "get", "/my/notes.json")] = bt.BCError("nope")
        app = SyncApp(FakeScr())
        app.drain()
        self.assertIn("couldn't load My notes", app.status)

    def test_reload_keeps_selection(self):
        app = self.app([notif(1), notif(2), notif(3)])
        app.cur = 1
        app.results.put(("loaded", (), [notif(9), notif(2)], None))
        app.drain()
        self.assertEqual(app.selected()["id"], 2)

    def test_real_bg_thread_delivers_results(self):
        app = self.app([])
        bt.App.bg(app, lambda x: x * 2, 21, tag="t1")
        bt.App.bg(app, lambda: 1 / 0, tag="t2")
        got = {}
        deadline = time.time() + 5
        while len(got) < 2 and time.time() < deadline:
            try:
                tag, _, res, err = app.results.get(timeout=0.1)
                got[tag] = res if err is None else err
            except Exception:  # noqa: BLE001
                pass
        self.assertEqual(got["t1"], 42)
        self.assertIsInstance(got["t2"], ZeroDivisionError)


class TestMarkReadAndUndo(Base):
    def test_e_marks_read_and_u_restores(self):
        app = self.app([notif(1), notif(2), notif(3)])
        app.key("e")
        app.drain()
        self.assertEqual([n["id"] for n in app.view()], [2, 3])
        self.assertEqual(json.loads(self.fake.called("api", "put", "/my/unreads.json")[0][4]), {"readables": ["sg1"]})
        self.assertIn("marked 1 read · 2 left", app.status)
        app.key("u")
        app.drain()
        self.assertEqual(app.selected()["id"], 1)
        self.assertEqual(len(self.fake.called("api", "post")), 1)
        app.key("u")
        self.assertEqual(app.status, "nothing to undo")

    def test_count_and_dd(self):
        app = self.app([notif(i) for i in range(1, 6)])
        app.key("3")
        app.key("e")
        self.assertEqual([n["id"] for n in app.view()], [4, 5])
        app.key("d")
        app.key("d")
        self.assertEqual([n["id"] for n in app.view()], [5])

    def test_visual_range(self):
        app = self.app([notif(i) for i in range(1, 6)])
        app.key("j")
        app.key("V")
        app.key("j")
        app.key("j")
        app.key("d")
        self.assertEqual([n["id"] for n in app.view()], [1, 5])
        self.assertIsNone(app.visual)

    def test_mark_read_failure_restores(self):
        app = self.app([notif(1), notif(2)])
        self.fake.routes[("api", "put", "/my/unreads.json")] = bt.BCError("500")
        app.key("e")
        app.drain()
        self.assertEqual(len(app.view()), 2)
        self.assertEqual(app.undo, [])
        self.assertIn("mark read failed, restored", app.status)

    def test_mark_read_failure_with_empty_undo_entry_does_not_crash(self):
        # D on an already-read todo leaves an undo entry with an empty batch
        app = self.app([notif(1), notif(2)])
        app.undo.append({"batch": [], "revert": lambda: None, "busy": False})
        self.fake.routes[("api", "put", "/my/unreads.json")] = bt.BCError("500")
        app.key("e")
        app.drain()
        self.assertEqual(len(app.undo), 1)

    def test_mark_read_failure_drops_mixed_note_entry(self):
        app = self.app([notif(1)], notes_content="<p>📥 Inbox</p><ul><li>n</li></ul>")
        app.show_all = True
        app.cur = 1  # the note sorts last; select it, then V up over the real item
        app.key("V")
        app.key("k")
        self.fake.routes[("api", "put", "/my/unreads.json")] = bt.BCError("500")
        app.key("e")
        app.drain()
        self.assertEqual(app.undo, [])

    def test_already_read_just_moves(self):
        app = self.app([notif(1, read=True), notif(2, read=True)])
        app.key("\t")
        app.key("e")
        self.assertEqual(app.cur, 1)

    def test_show_all_advances_past_marked(self):
        app = self.app([notif(1), notif(2)])
        app.key("\t")
        app.key("e")
        self.assertEqual(app.cur, 1)
        self.assertEqual(len(app.view()), 2)

    def test_marking_last_item_in_preview_returns_to_list(self):
        app = self.app([notif(1)])
        self.fake.routes[("show",)] = {"title": "x"}
        app.key("l")
        app.key("e")
        self.assertEqual(app.focus, "list")

    def test_mark_read_in_preview_jumps_to_next_focus(self):
        app = self.app([notif(1), notif(2)])
        self.fake.routes[("show",)] = {"title": "x"}
        app.key("l")
        app.key("e")
        self.assertEqual(app.focus, "preview")

    def test_mark_read_on_empty_view(self):
        app = self.app([])
        app.do_mark_read()
        app.move(1)
        self.assertEqual(app.cur, 0)

    def test_marking_a_note_clears_it_from_the_pad(self):
        store = {"content": "<p>📥 Inbox</p><ul><li>n1</li><li>n2</li></ul>"}
        self.fake.routes[("notifications", "list")] = {}
        self.fake.routes[("api", "get", "/my/notes.json")] = lambda *a: dict(store)
        self.fake.routes[("api", "put", "/my/notes.json")] = lambda *a: store.update(json.loads(a[4]))
        app = SyncApp(FakeScr())
        app.drain()
        app.key("e")
        app.drain()
        self.assertEqual(bt.note_texts(store["content"]), ["n2"])
        self.assertIn("cleared 1 note", app.status)
        app.key("u")
        app.drain()
        self.assertEqual(bt.note_texts(store["content"]), ["n2", "n1"])
        self.assertIn("reverted", app.status)

    def test_note_clear_failure_restores(self):
        self.fake.routes[("notifications", "list")] = {"unreads": [notif(1)]}
        self.fake.routes[("api", "get", "/my/notes.json")] = {"content": "<p>📥 Inbox</p><ul><li>n1</li></ul>"}
        app = SyncApp(FakeScr())
        app.drain()
        self.fake.routes[("api", "get", "/my/notes.json")] = bt.BCError("down")
        app.key("V")
        app.key("j")
        app.key("e")
        self.assertIn("marked 1 read + cleared 1 note", app.status)
        app.drain()
        self.assertIn("couldn't clear the note", app.status)
        self.assertEqual(len(app.view()), 1)

    def test_undo_server_failure_is_flashed(self):
        app = self.app([notif(1)])
        app.key("e")
        self.fake.routes[("api", "post")] = bt.BCError("x")
        app.key("u")
        app.drain()
        self.assertIn("undo failed on server", app.status)


class TestItemActions(Base):
    def setUp(self):
        super().setUp()
        self.fake.routes[("show",)] = {"title": "Ship"}

    def test_done_on_unread_todo_marks_read_and_undo_reopens(self):
        app = self.app([notif(1, "todos/100"), notif(2)])
        app.key("D")
        self.assertTrue(app.undo[-1]["busy"])
        app.drain()
        self.assertIn("completed “Ship” · u to undo", app.status)
        self.assertEqual(len(self.fake.called("done")), 1)
        app.key("u")
        app.drain()
        self.assertEqual(len(self.fake.called("reopen")), 1)
        self.assertEqual(app.status, "reverted ✓")

    def test_undo_while_busy(self):
        app = self.app([notif(1, "todos/100")])
        app.do_item_action.__func__  # noqa: B018
        app.undo.append({"batch": [], "revert": None, "busy": True})
        app.key("u")
        self.assertIn("still applying", app.status)

    def test_done_then_undo_on_already_read_todo(self):
        # regression: u after D on a read item crashed (IndexError on the empty batch)
        app = self.app([notif(1, "todos/100", read=True)])
        app.key("\t")
        app.key("D")
        app.drain()
        app.key("u")
        app.drain()
        self.assertEqual(len(self.fake.called("reopen")), 1)

    def test_noop_action_on_read_item_leaves_no_undo(self):
        self.fake.routes[("show",)] = {"title": "Ship", "completed": True}
        app = self.app([notif(1, "todos/100", read=True)])
        app.key("\t")
        app.key("D")
        app.drain()
        self.assertEqual(app.undo, [])
        self.assertEqual(app.status, "already completed: Ship")

    def test_failure_restores_unread(self):
        self.fake.routes[("show",)] = bt.BCError("forbidden")
        app = self.app([notif(1, "todos/100")])
        app.key("D")
        app.drain()
        self.assertIn("failed, left as is: forbidden", app.status)
        self.assertEqual(len(app.view()), 1)
        self.assertEqual(app.undo, [])

    def test_archive_asks_first(self):
        app = self.app([notif(1, "todos/100")], keys=["n"])
        app.key("X")
        self.assertEqual(app.status, "left as is")
        app.scr.keys = ["y"]
        app.key("X")
        app.drain()
        self.assertEqual(len(self.fake.called("recordings", "archive")), 1)

    def test_only_todos_and_cards(self):
        app = self.app([notif(1, "messages/3")])
        app.key("D")
        self.assertIn("todos and cards only", app.status)

    def test_done_on_note_clears_it(self):
        self.fake.routes[("notifications", "list")] = {}
        app = self.app([], notes_content="<p>📥 Inbox</p><ul><li>n1</li></ul>")
        app.key("D")
        self.assertEqual(app.view(), [])


class TestReply(Base):
    def reply(self, n, text="thanks", routes=None):
        self.fake.routes.update(routes or {})
        self.fake.routes.setdefault(("people", "pingable"), PEOPLE)
        app = self.app([n])
        with mock.patch.object(SyncApp, "compose", side_effect=text if isinstance(text, list) else [text]):
            app.key("r")
        app.drain()
        return app

    def test_comment_on_todo(self):
        app = self.reply(notif(1, "todos/100"))
        self.assertEqual(self.fake.called("comment")[0], ("comment", "100", "thanks", "--in", "10"))
        self.assertEqual(app.status, "reply sent ✓")

    def test_reply_to_answer_notification_comments_on_the_answer(self):
        # regression for "failed to comment on items 10370826216: unexpected end of JSON input"
        n = notif(1, "questions/10370826216", bucket=49137574, readable_identifier=gid(10439128014))
        self.reply(n)
        self.assertEqual(self.fake.called("comment")[0][:2], ("comment", "10439128014"))

    def test_reply_to_a_question_answers_it(self):
        # questions take answers, not comments; commenting fails with an empty-body error
        n = notif(1, "questions/42", readable_identifier=gid(42), type="Question")
        self.reply(n, "@jane.smith on it")
        self.assertEqual(self.fake.called("comment"), [])
        call = self.fake.called("checkins", "answer", "create")[0]
        self.assertEqual(call[3], "42")
        self.assertIn("<bc-attachment", call[4])

    def test_chat_post(self):
        self.reply(notif(1, "chats/5"))
        self.assertEqual(self.fake.called("chat")[0], ("chat", "post", "thanks", "--in", "10", "--room", "5"))

    def test_checkin_reminder_answer_uses_raw_mention_tags(self):
        n = notif(1, app_url="https://app.basecamp.com/1/questions/answers/entries/3")
        self.reply(n, "@bob done", {("projects", "list"): [{"name": "Proj", "id": 10}],
                                    ("checkins", "questions"): [{"id": 4, "title": "Item 1"}]})
        call = self.fake.called("checkins", "answer", "create")[0]
        self.assertIn('sgid="B"', call[4])

    def test_checkin_resolution_failure(self):
        n = notif(1, app_url="https://app.basecamp.com/1/questions/answers/entries/3")
        app = self.reply(n, routes={("projects", "list"): []})
        self.assertIn("can't find the check-in question", app.status)

    def test_bad_mentions_reopen_editor_then_send(self):
        self.reply(notif(1), ["hi @zed", "hi @bob"])
        self.assertEqual(self.fake.called("comment")[0][2], "hi [@Bob Stone](mention:B)")

    def test_cancelled_and_failed(self):
        app = self.reply(notif(1), None)
        self.assertEqual(app.status, "nothing written, reply not sent")
        app = self.reply(notif(1), "x", {("comment",): bt.BCError("denied")})
        self.assertEqual(app.status, "reply failed: denied")

    def test_people_failure_and_unreplyable(self):
        app = self.reply(notif(1), "@x", {("people", "pingable"): bt.BCError("down")})
        self.assertIn("couldn't load people", app.status)
        app = self.reply(notif(1, app_url="https://app.basecamp.com/my/x"))
        self.assertEqual(app.status, "can't reply to this one")

    def test_note_reply_refused(self):
        app = self.app([], notes_content="<p>📥 Inbox</p><ul><li>n1</li></ul>")
        app.key("r")
        self.assertIn("that's your note", app.status)


class TestCompose(Base):
    def editor(self, write=None):
        """Fake editor: optionally writes `write` to the file (bumping mtime), records argv."""
        seen = {}

        def call(argv):
            seen["argv"] = argv
            path = argv[-1]
            seen["initial"] = Path(path).read_text()
            if write is not None:
                Path(path).write_text(write)
                st = os.stat(path)
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            return 0
        return seen, mock.patch.object(bt.subprocess, "call", side_effect=call)

    def test_vim_gets_dictionary_and_notes_are_stripped(self):
        self.fake.routes[("people", "pingable")] = PEOPLE
        app = self.app([])
        before = set(os.listdir(tempfile.gettempdir()))
        seen, p = self.editor(f"{bt.NOTE}fix this\nhello\n")
        with p, mock.patch.dict(os.environ, {"VISUAL": "vim", "EDITOR": ""}):
            self.assertEqual(app.compose("hello", ["fix this"]), "hello")
        self.assertIn("+normal G$", seen["argv"])
        self.assertTrue(any("dictionary=" in a for a in seen["argv"]))
        self.assertEqual(seen["initial"], f"{bt.NOTE}fix this\nhello")
        leftovers = [f for f in set(os.listdir(tempfile.gettempdir())) - before if f.startswith("basecamp-tui-")]
        self.assertEqual(leftovers, [])

    def test_quit_without_writing_returns_none(self):
        app = self.app([])
        seen, p = self.editor(None)
        with p, mock.patch.dict(os.environ, {"VISUAL": "nano"}):
            self.assertIsNone(app.compose())
        self.assertEqual(seen["argv"][0], "nano")

    def test_people_failure_leaves_no_dictionary_file(self):
        self.fake.routes[("people", "pingable")] = bt.BCError("down")
        app = self.app([])
        before = {f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".dict")}
        seen, p = self.editor("x")
        with p, mock.patch.dict(os.environ, {"VISUAL": "vim"}):
            self.assertEqual(app.compose(), "x")
        self.assertIn("+startinsert", seen["argv"])
        after = {f for f in os.listdir(tempfile.gettempdir()) if f.endswith(".dict")}
        self.assertEqual(after - before, set())

    def test_add_note(self):
        store = {"content": ""}
        app = self.app([])
        self.fake.routes[("api", "get", "/my/notes.json")] = lambda *a: dict(store)
        self.fake.routes[("api", "put", "/my/notes.json")] = lambda *a: store.update(json.loads(a[4]))
        with mock.patch.object(SyncApp, "compose", return_value="remember"):
            app.key("n")
        app.drain()
        self.assertEqual(bt.note_texts(store["content"]), ["remember"])
        self.assertTrue(bt.is_note(app.items[-1]))

    def test_add_note_cancel_and_failure(self):
        app = self.app([])
        with mock.patch.object(SyncApp, "compose", return_value=""):
            app.key("n")
        self.assertEqual(app.status, "nothing written, no note added")
        self.fake.routes[("api", "get", "/my/notes.json")] = bt.BCError("x")
        with mock.patch.object(SyncApp, "compose", return_value="r"):
            app.key("n")
        app.drain()
        self.assertEqual(app.items, [])
        self.assertIn("note not saved", app.status)


class TestClaudeAndBrowser(Base):
    def test_ask_claude(self):
        app = self.app([notif(1, "messages/3", title="Launch plan for Q4")])
        with mock.patch.object(bt.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(SyncApp, "prompt_line", return_value=" summarise "), \
                mock.patch.object(bt, "start_session", return_value="s1") as start:
            app.key("c")
        app.drain()
        name, prompt = start.call_args[0]
        self.assertEqual(name, "Launch plan for Q4 · summarise")
        self.assertIn("https://app.basecamp.com/1/buckets/10/messages/3", prompt)
        self.assertEqual(app.status, "session created · Launch plan for Q4 · summarise")

    def test_ask_claude_about_note_and_failures(self):
        app = self.app([], notes_content="<p>📥 Inbox</p><ul><li>idea</li></ul>")
        with mock.patch.object(bt.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(SyncApp, "prompt_line", return_value="go"), \
                mock.patch.object(bt, "start_session", side_effect=RuntimeError("nope")) as start:
            app.key("C")
        app.drain()
        self.assertIn("My note:\nidea", start.call_args[0][1])
        self.assertEqual(app.status, "claude failed: nope")
        with mock.patch.object(bt.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(SyncApp, "prompt_line", return_value=None), \
                mock.patch.object(bt, "start_session") as start:
            app.key("c")
        start.assert_not_called()
        with mock.patch.object(bt.shutil, "which", return_value=None):
            app.key("c")
        self.assertIn("not on PATH", app.status)

    def test_open_browser_or_yank(self):
        app = self.app([notif(1)])
        with mock.patch.dict(os.environ, {"BROWSER": "firefox", "DISPLAY": ":0"}), \
                mock.patch.object(bt.subprocess, "Popen") as popen:
            app.key("o")
        self.assertEqual(popen.call_args[0][0][0], "firefox")
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER")}
        env["TMUX"] = "1"
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(bt.sys, "platform", "linux"):
            app.key("o")
        self.assertTrue(self.stdout.out[-1].startswith("\033Ptmux;"))
        self.assertIn("copied:", app.status)

    def test_no_selection(self):
        app = self.app([])
        app.open_browser()
        app.yank()
        app.ask_claude()
        self.assertEqual(self.stdout.out, [])


class TestKeys(Base):
    def test_navigation(self):
        app = self.app([notif(i) for i in range(1, 51)], h=12)
        app.key("G")
        self.assertEqual(app.cur, 49)
        app.key("g")
        app.key("g")
        self.assertEqual(app.cur, 0)
        for k in ("1", "0", "j"):
            app.key(k)
        self.assertEqual(app.cur, 10)
        app.key("k")
        app.key(curses.KEY_DOWN)
        app.key("J")
        app.key("K")
        app.key(curses.KEY_UP)
        self.assertEqual(app.cur, 9)
        app.key("\x04")
        self.assertEqual(app.cur, 14)
        app.key("\x02")
        self.assertEqual(app.cur, 4)
        for k in ("5", "G"):
            app.key(k)
        self.assertEqual(app.cur, 4)
        for k in ("3", "g", "g"):
            app.key(k)
        self.assertEqual(app.cur, 2)
        app.draw()
        app.key("L")
        self.assertEqual(app.cur, 9)
        app.key("M")
        app.key("H")
        self.assertEqual(app.cur, app.top)
        app.key("0")  # a leading 0 is not a count
        self.assertEqual(app.count, "")

    def test_preview_scrolling(self):
        app = self.app([notif(1), notif(2)])
        self.fake.routes[("show",)] = {"title": "x"}
        app.key("l")
        self.assertEqual(app.focus, "preview")
        app.key("j")
        app.key("j")
        app.key("k")
        self.assertEqual(app.pscroll, 1)
        app.key("\x04")
        app.key(curses.KEY_PPAGE)
        self.assertEqual(app.pscroll, 0)
        app.key("G")
        self.assertEqual(app.pscroll, 10 ** 6)
        app.key("g")
        app.key("g")
        self.assertEqual(app.pscroll, 0)
        app.key("J")
        self.assertEqual(app.cur, 1)
        app.key("h")
        self.assertEqual(app.focus, "list")
        app.key("l")
        app.key("q")
        self.assertEqual(app.focus, "list")

    def test_quit_esc_help(self):
        app = self.app([notif(1)])
        app.key("V")
        self.assertTrue(app.key("q"))
        self.assertIsNone(app.visual)
        app.key("V")
        app.key("\x1b")
        self.assertIsNone(app.visual)
        app.key("?")
        self.assertTrue(app.help)
        app.key("x")
        self.assertFalse(app.help)
        app.key("d")
        app.key("x")  # d followed by anything else cancels
        self.assertEqual(len(app.view()), 1)
        app.key("g")
        app.key("x")
        self.assertFalse(app.key("q"))

    def test_tab_and_refresh(self):
        app = self.app([notif(1), notif(2, read=True)])
        app.key("\t")
        self.assertEqual((len(app.view()), app.status), (2, "showing all"))
        app.key("\t")
        self.assertEqual(app.status, "showing unread")
        app.key("R")
        app.drain()
        self.assertEqual(len(self.fake.called("notifications", "list", "--page", "1")), 2)

    def test_search(self):
        app = self.app([notif(1, title="alpha"), notif(2, title="beta"), notif(3, title="alphabet")])
        with mock.patch.object(SyncApp, "prompt_line", return_value="alpha"):
            app.key("/")
        self.assertEqual(app.cur, 2)
        with mock.patch.object(SyncApp, "prompt_line", return_value=""):
            app.key("/")
        self.assertEqual(app.cur, 0)
        app.key("N")
        self.assertEqual(app.cur, 2)
        with mock.patch.object(SyncApp, "prompt_line", return_value="zzz"):
            app.key("/")
        self.assertEqual(app.status, "not found: zzz")
        app.search = ""
        app.do_search()

    def test_colon_commands(self):
        app = self.app([notif(i) for i in range(1, 6)])
        for cmd, cur in (("3", 2), ("e", 2), ("bogus", 2), ("", 2)):
            with mock.patch.object(SyncApp, "prompt_line", return_value=cmd):
                self.assertTrue(app.key(":"))
            self.assertEqual(app.cur, cur)
        self.assertEqual(app.status, "not a command: bogus")
        with mock.patch.object(SyncApp, "prompt_line", return_value="wq"):
            self.assertFalse(app.key(":"))


class TestEscapes(unittest.TestCase):
    def test_kitty_and_modify_other_keys_ctrl_letters(self):
        self.assertEqual(bt.decode_escape("[117;5u"), "\x15")  # C-u
        self.assertEqual(bt.decode_escape("[100;5u"), "\x04")  # C-d
        self.assertEqual(bt.decode_escape("[117:85;5u"), "\x15")  # with alternate keys
        self.assertEqual(bt.decode_escape("[27;5;117~"), "\x15")

    def test_special_keys(self):
        self.assertEqual(bt.decode_escape(""), "\x1b")
        self.assertEqual(bt.decode_escape("[27u"), "\x1b")
        self.assertEqual(bt.decode_escape("[13u"), "\r")
        self.assertEqual(bt.decode_escape("[9u"), "\t")
        self.assertEqual(bt.decode_escape("[127u"), "\x7f")
        self.assertEqual(bt.decode_escape("[106u"), "j")
        self.assertEqual(bt.decode_escape("[103;2u"), "G")
        self.assertEqual(bt.decode_escape("\r"), bt.ALT_ENTER)
        self.assertEqual(bt.decode_escape("[13;3u"), bt.ALT_ENTER)
        self.assertEqual(bt.decode_escape("[13;2u"), bt.ALT_ENTER)

    def test_navigation_sequences(self):
        self.assertEqual(bt.decode_escape("[A"), curses.KEY_UP)
        self.assertEqual(bt.decode_escape("OB"), curses.KEY_DOWN)
        self.assertEqual(bt.decode_escape("[1;5C"), curses.KEY_RIGHT)
        self.assertEqual(bt.decode_escape("[H"), curses.KEY_HOME)
        self.assertEqual(bt.decode_escape("[4~"), curses.KEY_END)
        self.assertEqual(bt.decode_escape("[5~"), curses.KEY_PPAGE)
        self.assertEqual(bt.decode_escape("[6;5~"), curses.KEY_NPAGE)

    def test_unknown_and_alt_combos_are_ignored(self):
        self.assertIsNone(bt.decode_escape("[99~"))
        self.assertIsNone(bt.decode_escape("x"))
        self.assertIsNone(bt.decode_escape("[97;3u"))  # M-a
        self.assertIsNone(bt.decode_escape("[49;5u"))  # C-1


class TestPromptLine(Base):
    def prompt(self, keys, w=40):
        app = self.app([notif(1)], w=w)
        app.scr.keys = list(keys)
        return app, app.prompt_line("› ")

    def test_typing_editing_and_submit(self):
        _, s = self.prompt(list("hello wor") + ["\x17"] + list("there") + ["\x7f", "\n"])
        self.assertEqual(s, "hello ther")
        _, s = self.prompt(list("abc") + ["\x15"] + list("x") + [curses.KEY_ENTER])
        self.assertEqual(s, "x")

    def test_escape_and_backspace_on_empty_cancel(self):
        self.assertIsNone(self.prompt(["a", "\x1b"])[1])
        self.assertIsNone(self.prompt(["\x7f"])[1])

    def test_bracketed_paste_keeps_newlines_and_alt_enter(self):
        keys = ["\x1b", "[", "2", "0", "0", "~", "a", "\n", "\t", "b", "\x1b", "[", "2", "0", "1", "~",
                "\x1b", "\r", "c", "\n"]
        app, s = self.prompt(keys)
        self.assertEqual(s, "a\n    b\nc")
        self.assertIn("\033[?2004h", self.stdout.out)
        self.assertEqual(self.stdout.out[-1], "\033[?2004l")

    def test_escape_sequence_ended_by_non_str_key(self):
        _, s = self.prompt(["\x1b", "[", curses.KEY_LEFT, "z", "\n"])
        self.assertEqual(s, "z")

    def test_long_input_wraps_upwards(self):
        app, s = self.prompt(list("word " * 30) + ["\n"], w=30)
        self.assertEqual(s, "word " * 30)
        self.assertIn("─", app.scr.text())


class TestVimKeys(Base):
    def run_keys(self, app, keys):
        app.scr.keys = list(keys) + ["q"]
        app.run()

    def test_ctrl_u_from_kitty_protocol_is_half_page_up_not_undo(self):
        # regression: ESC[117;5u was read as Esc, count 1175, u -> undo
        app = self.app([notif(i) for i in range(1, 41)], h=22)
        self.fake.routes[("show",)] = {"title": "x"}
        app.key("e")
        app.key("G")
        self.run_keys(app, ["\x1b", *"[117;5u"])
        self.assertEqual(app.cur, 38 - 10)
        self.assertEqual(len(app.undo), 1)
        self.assertEqual(app.count, "")
        self.run_keys(app, ["\x1b", *"[27;5;100~"])  # C-d, modifyOtherKeys
        self.assertEqual(app.cur, 38)

    def test_lone_and_unknown_escapes_in_the_run_loop(self):
        app = self.app([notif(1), notif(2)])
        self.fake.routes[("show",)] = {"title": "x"}
        app.focus, app.visual = "preview", 0
        self.run_keys(app, ["\x1b", curses.error()])  # the escape timeout passes: a lone Esc
        self.assertEqual((app.focus, app.visual), ("list", None))
        self.run_keys(app, ["\x1b", *"[99~", "j"])
        self.assertEqual(app.cur, 1)

    def test_counts_survive_two_key_commands(self):
        app = self.app([notif(i) for i in range(1, 11)])
        for k in "4gg":
            app.key(k)
        self.assertEqual(app.cur, 3)
        for k in "3dd":
            app.key(k)
        self.assertEqual([n["id"] for n in app.view()][:4], [1, 2, 3, 7])

    def test_ctrl_n_p_home_end(self):
        app = self.app([notif(i) for i in range(1, 11)])
        app.key("\x0e")
        app.key("\x0e")
        app.key("\x10")
        self.assertEqual(app.cur, 1)
        app.key(curses.KEY_END)
        self.assertEqual(app.cur, 9)
        app.key(curses.KEY_HOME)
        self.assertEqual(app.cur, 0)
        app.focus, app.pscroll = "preview", 5
        app.key(curses.KEY_HOME)
        self.assertEqual(app.pscroll, 0)

    def test_scroll_lines_keep_selection_visible(self):
        app = self.app([notif(i) for i in range(1, 51)], h=12)  # 10 rows
        app.draw()
        for k in "3\x05":
            app.key(k)
        self.assertEqual((app.top, app.cur), (3, 3))
        app.key("j")
        app.key("\x19")
        self.assertEqual((app.top, app.cur), (2, 4))
        app.key("G")
        app.draw()
        app.key("\x19")
        self.assertEqual(app.cur, app.top + 9)
        app.focus = "preview"
        app.key("\x05")
        app.key("\x05")
        app.key("\x19")
        self.assertEqual(app.pscroll, 1)

    def test_z_commands(self):
        app = self.app([notif(i) for i in range(1, 51)], h=12)
        app.goto(20)
        for keys, top in (("zt", 20), ("zz", 15), ("zb", 11), ("z.", 15)):
            for k in keys:
                app.key(k)
            self.assertEqual(app.top, top, keys)
        app.draw()
        self.assertEqual(app.top, 15)
        app.key("z")
        app.key("x")
        self.assertEqual(app.top, 15)

    def test_zz_zq_quit_and_ctrl_l(self):
        app = self.app([notif(1)])
        app.key("Z")
        self.assertFalse(app.key("Z"))
        app.key("Z")
        self.assertFalse(app.key("Q"))
        app.key("Z")
        self.assertTrue(app.key("x"))
        app.draw()
        app.key("\x0c")
        self.assertEqual(app.scr.rows, {})

    def test_prompt_decodes_kitty_keys(self):
        app = self.app([notif(1)])
        app.scr.keys = [*"abc def", "\x1b", *"[119;5u", *"x", "\x1b", *"[13;3u", *"y", "\x1b", *"[13u"]
        self.assertEqual(app.prompt_line("/"), "abc x\ny")
        app.scr.keys = [*"abc", "\x1b", *"[27u"]
        self.assertIsNone(app.prompt_line("/"))
        # a lone Esc inside a paste is dropped, not a cancel; unknown sequences are ignored
        app.scr.keys = ["\x1b", *"[200~", "a", "\x1b", curses.error(), "\x1b", *"[201~", "\x1b", *"[99~", "\n"]
        self.assertEqual(app.prompt_line("/"), "a")


class TestDrawing(Base):
    def items(self):
        return [notif(1, title="Unread thing", type="Mention"), notif(2, title="Read thing", read=True, type="Chat"),
                notif(3, title="Third", type="Unknown", read_at="0001-01-01T00:00:00Z")]

    def test_list_view(self):
        app = self.app(self.items(), w=100)
        app.key("V")
        app.draw()
        out = app.scr.text()
        self.assertIn("Basecamp inbox · 2 unread · UNREAD", out)
        self.assertIn("Unread thing", out)
        self.assertNotIn("Read thing", out)
        self.assertIn("-- VISUAL --", out)
        app.key("\x1b")
        app.key("\t")
        app.draw()
        self.assertIn("Read thing", app.scr.text())

    def test_empty_and_loading(self):
        app = self.app([])
        app.draw()
        self.assertIn("Inbox zero", app.scr.text())
        app.loading = True
        app.draw()
        self.assertIn("loading…", app.scr.text())

    def test_help(self):
        app = self.app([notif(1)])
        app.key("?")
        app.draw()
        self.assertIn("vim-style Basecamp inbox", app.scr.text())

    def test_split_preview_with_focus_scroll_and_percent(self):
        comments = [{"id": i, "creator": {"name": f"P{i}"}, "content": f"comment {i}"} for i in range(30)]
        self.fake.routes.update({("show",): {"type": "Todo", "title": "Big", "comments_count": 30},
                                 ("comments", "list"): comments})
        app = self.app([notif(1, "todos/100#__recording_25")], h=20, w=150)
        app.key("l")
        app.drain()
        app.draw()
        out = app.scr.text()
        self.assertIn("│", out)
        self.assertIn("▶ P25", out)
        self.assertRegex(out, r"\d+%")
        self.assertIn("j/k scroll", out)

    def test_full_preview_states(self):
        app = self.app([notif(1, "messages/3")], w=80)
        app.focus = "preview"
        app.inflight.add(1)  # still loading
        app.draw()
        self.assertIn("loading…", app.scr.text())
        app.cache[1] = bt.BCError("boom")
        app.draw()
        self.assertIn("failed to load: boom", app.scr.text())

    def test_chat_opens_at_the_bottom(self):
        lines = [{"id": i, "created_at": f"2026-01-01T00:{i:02d}", "creator": {"name": "A"}, "content": f"l{i}"}
                 for i in range(40)]
        self.fake.routes[("api", "get")] = lines
        app = self.app([notif(1, "chats/5")], h=15, w=80)
        app.key("l")
        app.drain()
        self.assertGreater(app.pscroll, 0)
        app.draw()
        self.assertIn("l39", app.scr.text())

    def test_status_flash_expires(self):
        app = self.app([notif(1)])
        app.flash("hello")
        app.draw()
        self.assertIn("hello", app.scr.text())
        app.status_t = 0
        app.draw()
        self.assertIn("1/1", app.scr.text())

    def test_tiny_terminal_draws_without_hanging(self):
        self.fake.routes[("show",)] = {"title": "A title that needs wrapping", "content": "body 界"}
        app = self.app([notif(1, "messages/3")], h=4, w=3)
        app.key("l")
        app.drain()
        app.draw()

    def test_use_default_colors_failure(self):
        def boom():
            raise curses.error
        with mock.patch.object(bt.curses, "use_default_colors", boom):
            app = self.app([])
        self.assertIn("bar", app.c)


class TestRunAndMain(Base):
    def test_run_loop_until_quit(self):
        app = self.app([notif(1), notif(2)])
        self.fake.routes[("show",)] = {"title": "x"}
        app.scr.keys = [curses.error(), curses.KEY_RESIZE, "j", "q"]
        app.run()
        self.assertEqual(app.cur, 1)
        self.assertEqual(len(self.fake.called("show")), 2)  # prefetched

    def test_main_requires_cli(self):
        with mock.patch.object(bt.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit) as e:
                bt.main()
        self.assertIn("CLI not found", str(e.exception))

    def test_main_starts_curses(self):
        with mock.patch.object(bt.shutil, "which", return_value="/bin/basecamp"), \
                mock.patch.object(bt.locale, "setlocale"), \
                mock.patch.object(bt.curses, "wrapper") as wrapper:
            bt.main()
        wrapper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
