"""Every read surface shows a task's comments (task 346).

A comment is where the filer corrects or redirects a brief after it was
written. Two of the three read surfaces never fetched them, so a run that
re-read its ticket saw the description alone and worked from the version the
human had already moved on from. The gap had been in place long enough to be
carried as operational lore -- "check comments via the API, the page renders
none" -- which is the tell: a workaround one reader has memorised is not a
surface.

What is pinned here:

  * `vkctl.py show`, the /task page and the MCP's `get_task` all display
    comments, and all three take the SAME projection, so a field added to one
    is not a field the other two silently lack.
  * A task with no comments SAYS so, on every surface. Blank is what the gap
    looked like, so absence has to be distinguishable from the feature being
    missing -- the test that would have caught this originally.
  * Comment bodies are other people's HTML. They are flattened for reading and
    escaped where they reach a page.
"""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout

from vikunja_claude.vikunja import COMMENT_FIELDS, comment_view
from vikunja_claude.web import comments_html, ticket_page

from .support import McpTestCase, ServiceTestCase

# Two comments on task 9, the Ready ticket the default layout provides.
COMMENTS = {
    9: [
        {
            "id": 41,
            "comment": "<p>Scope changed: cover <code>vikunja-db</code> too.</p>",
            "created": "2026-08-05T09:00:00Z",
            "author": {"username": "glen"},
        },
        {
            "id": 42,
            "comment": "<p>And the <strong>restore</strong> path.</p>",
            "created": "2026-08-05T10:00:00Z",
            "author": {"username": "glen"},
        },
    ]
}


class TheProjection(unittest.TestCase):
    """One definition of a comment's published shape."""

    def test_a_raw_row_is_reduced_to_the_published_fields(self):
        view = comment_view(COMMENTS[9][0])
        self.assertEqual(set(view), set(COMMENT_FIELDS))
        self.assertEqual(view["id"], 41)
        self.assertEqual(view["author"], "glen")
        self.assertEqual(view["created"], "2026-08-05T09:00:00Z")

    def test_the_body_is_flattened_out_of_editor_html(self):
        view = comment_view(COMMENTS[9][0])
        self.assertIn("Scope changed", view["text"])
        self.assertIn("`vikunja-db`", view["text"], "inline code keeps its backticks")
        self.assertNotIn("<p>", view["text"])

    def test_a_field_vikunja_adds_upstream_is_not_published(self):
        """It projects, it does not pass through -- the boundary's standing
        rule, and the reason this is a function rather than a dict copy."""
        view = comment_view({**COMMENTS[9][0], "reactions": {"thumbsup": ["someone"]}})
        self.assertEqual(set(view), set(COMMENT_FIELDS))

    def test_a_row_missing_everything_still_projects(self):
        """Vikunja has answered with an author-less row. A read surface must
        not raise on one -- it must render what it has."""
        view = comment_view({})
        self.assertEqual(set(view), set(COMMENT_FIELDS))
        self.assertIsNone(view["author"])
        self.assertEqual(view["text"], "")


class ThePreviewPayload(ServiceTestCase):
    comments = COMMENTS

    def test_preview_carries_the_comments(self):
        data = self.service.preview(self.service.get_task(9))
        self.assertEqual([c["id"] for c in data["comments"]], [41, 42])
        self.assertIn("Scope changed", data["comments"][0]["text"])

    def test_comments_are_oldest_first(self):
        data = self.service.preview(self.service.get_task(9))
        created = [c["created"] for c in data["comments"]]
        self.assertEqual(created, sorted(created))

    def test_the_preview_takes_the_shared_projection(self):
        data = self.service.preview(self.service.get_task(9))
        self.assertEqual(set(data["comments"][0]), set(COMMENT_FIELDS))


class ThePreviewPayloadWithoutComments(ServiceTestCase):
    def test_an_uncommented_task_carries_an_empty_list_not_a_missing_key(self):
        """A missing key and an empty list read the same to a renderer that
        uses `.get`, and differently to one that does not. The page must be
        able to say "no comments" rather than fail to mention them."""
        data = self.service.preview(self.service.get_task(9))
        self.assertIn("comments", data)
        self.assertEqual(data["comments"], [])


class ThePage(unittest.TestCase):
    @staticmethod
    def _page(comments):
        return ticket_page({
            "task_id": 9, "reference": "task 9", "summary": "Back up Vikunja",
            "url": "http://127.0.0.1:3456/tasks/9", "bucket": "Ready",
            "labels": [], "done": False, "workdir": "/home/glen/stacks/investment",
            "description": "the description", "prompt": "the prompt",
            "running": None, "comments": comments,
        })

    def test_the_page_has_a_comments_section(self):
        html = self._page([comment_view(c) for c in COMMENTS[9]])
        self.assertIn("<h2>Comments</h2>", html)

    def test_each_comment_renders_with_its_author_and_time(self):
        html = self._page([comment_view(c) for c in COMMENTS[9]])
        for fragment in ("Scope changed", "restore", "glen",
                         "2026-08-05T09:00:00Z", "2026-08-05T10:00:00Z"):
            self.assertIn(fragment, html)

    def test_an_uncommented_task_says_so_rather_than_rendering_nothing(self):
        """The regression test for the gap itself. An empty section is what
        the page showed for two months while it read no comments at all."""
        html = self._page([])
        self.assertIn("<h2>Comments</h2>", html)
        self.assertIn("No comments yet", html)

    def test_a_page_built_without_the_key_still_renders(self):
        """`preview` always supplies it now, but the renderer is also reached
        from the launch page and from tests, and a KeyError here would take
        down the whole ticket view over a missing comment list."""
        data = {
            "task_id": 9, "reference": "task 9", "summary": "Back up Vikunja",
            "url": "http://x", "bucket": "Ready", "labels": [], "done": False,
            "workdir": "/w", "description": "d", "prompt": "p", "running": None,
        }
        self.assertIn("No comments yet", ticket_page(data))

    def test_a_comment_body_is_escaped(self):
        """Comment text is written by someone other than the operator and is
        the one part of this page that is. Flattening the editor's HTML is not
        escaping -- a body containing markup arrives here as markup."""
        html = comments_html([comment_view({
            "id": 1, "created": "2026-08-05T09:00:00Z",
            "author": {"username": "glen"},
            "comment": "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
        })])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_author_name_is_escaped(self):
        html = comments_html([{"id": 1, "created": "", "text": "hi",
                               "author": "<b>glen</b>"}])
        self.assertNotIn("<b>glen</b>", html)
        self.assertIn("&lt;b&gt;glen&lt;/b&gt;", html)

    def test_comments_appear_between_the_description_and_the_prompt(self):
        """Position is the point: the prompt is long, and a comment placed
        after it is a comment nobody scrolls to."""
        html = self._page([comment_view(c) for c in COMMENTS[9]])
        self.assertLess(html.index("<h2>Description</h2>"), html.index("<h2>Comments</h2>"))
        self.assertLess(html.index("<h2>Comments</h2>"), html.index("<h2>Generated prompt</h2>"))


class TheMcpTool(McpTestCase):
    comments = COMMENTS

    def test_get_task_still_returns_the_same_projection(self):
        """The MCP had this already; task 346 pointed it at the shared
        projection rather than its own copy, and its output must not move."""
        result = self.call_tool("get_task", task_number=8)
        payload = result["result"]["structuredContent"]
        self.assertEqual([c["id"] for c in payload["comments"]], [41, 42])
        self.assertEqual(set(payload["comments"][0]), set(COMMENT_FIELDS))
        self.assertIn("`vikunja-db`", payload["comments"][0]["text"])


class CliTestCase(ServiceTestCase):
    """Drives `vkctl show` itself, not the helper it calls.

    The first version of these tests called `_print_comments` directly and
    reproduced what `show` does around it. That passes with the call site
    DELETED from `show` -- it measured the helper and called it the command,
    which is the same class of mistake as asserting a constant instead of the
    served page. So `main()` is invoked for real, with only the config and the
    transport substituted.
    """

    def run_vkctl(self, *argv: str) -> tuple[int, str]:
        import vkctl

        config, client = self.config, self.client
        original_from_env = vkctl.Config.from_env
        original_client = vkctl.VikunjaClient
        vkctl.Config.from_env = staticmethod(lambda *a, **kw: config)
        vkctl.VikunjaClient = lambda *a, **kw: client
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = vkctl.main(list(argv))
        finally:
            vkctl.Config.from_env = original_from_env
            vkctl.VikunjaClient = original_client
        return code, out.getvalue()


class TheCliOutput(CliTestCase):
    comments = COMMENTS

    def _show(self, task_id: int) -> str:
        code, text = self.run_vkctl("show", "--task", str(task_id))
        self.assertEqual(code, 0, text)
        return text

    def test_show_prints_each_comment_with_its_author_and_time(self):
        text = self._show(9)
        self.assertIn("comments (2):", text)
        self.assertIn("Scope changed", text)
        self.assertIn("restore", text)
        self.assertIn("by glen", text)
        self.assertIn("2026-08-05T09:00:00Z", text)

    def test_show_prints_comments_as_text_not_markup(self):
        text = self._show(9)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<strong>", text)

    def test_show_puts_the_comments_after_the_description(self):
        text = self._show(9)
        self.assertLess(text.index("authoritative"), text.index("comments (2):"))


class TheCliOutputWithoutComments(CliTestCase):
    def test_show_says_none_rather_than_printing_nothing(self):
        """The other half of the regression test. `show` printing nothing is
        precisely what it did before it read comments at all."""
        code, text = self.run_vkctl("show", "--task", "9")
        self.assertEqual(code, 0, text)
        self.assertIn("comments: (none)", text)


class TheReadsStayReads(ServiceTestCase):
    comments = COMMENTS

    def test_showing_comments_writes_nothing(self):
        """These are read paths. A surface that repaired or annotated what it
        read would give a reader capability it was never granted."""
        self.service.preview(self.service.get_task(9))
        writes = [(method, path) for method, path, _ in self.vikunja.calls
                  if method in ("POST", "PUT", "DELETE")]
        self.assertEqual(writes, [])

    def test_the_comments_endpoint_is_read_with_GET(self):
        self.service.preview(self.service.get_task(9))
        self.assertIn(
            ("GET", "/tasks/9/comments"),
            [(method, path) for method, path, _ in self.vikunja.calls],
        )


class TheProjectionIsNotDuplicated(unittest.TestCase):
    def test_no_surface_rebuilds_the_comment_shape_for_itself(self):
        """Three surfaces derived this independently before task 346, which is
        how the MCP came to have it and the other two did not. Pinned by
        looking for the giveaway -- reaching into the raw row's `comment` key
        outside the projection itself."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for rel in ("vkctl.py", "vikunja_claude/web.py", "vikunja_claude/service.py"):
            source = (root / rel).read_text()
            if re.search(r"""\.get\(\s*["']comment["']""", source):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"raw comment rows handled outside the projection: {offenders}")
