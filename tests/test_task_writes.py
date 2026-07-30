"""Writing to a task must never cost the description.

POST /tasks/{id} replaces the whole task, so any partial body silently blanks
the fields it omits. These tests first prove the fake reproduces that hazard --
otherwise everything below passes vacuously -- and then prove the client's one
write path survives it.
"""

from __future__ import annotations

import pytest

from vikunja_claude.vikunja import DescriptionLost, VikunjaClient, VikunjaError

from .fakes import PROJECT_ID, FakeVikunja

LONG_DESCRIPTION = (
    "<p>Vikunja is now the <strong>authoritative</strong> queue.</p>"
    "<ul><li>cover <code>vikunja-db</code></li></ul>"
)
TASK_WITH_DESCRIPTION = 9


def client(fake: FakeVikunja) -> VikunjaClient:
    return VikunjaClient("http://vikunja.test/api/v1", "token", transport=fake)


def stored(fake: FakeVikunja, task_id: int) -> dict:
    return fake._find(task_id)


def test_partial_post_wipes_the_description_in_the_fake_too():
    """The hazard is real in the test double, so the tests below mean something."""
    fake = FakeVikunja()
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == LONG_DESCRIPTION

    client(fake).call("POST", f"/tasks/{TASK_WITH_DESCRIPTION}", {"done": True})

    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == ""


def test_close_task_keeps_the_description():
    fake = FakeVikunja()
    updated = client(fake).close_task(TASK_WITH_DESCRIPTION)

    assert updated["done"] is True
    assert updated["description"] == LONG_DESCRIPTION
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == LONG_DESCRIPTION


def test_close_task_reads_before_it_writes():
    fake = FakeVikunja()
    client(fake).close_task(TASK_WITH_DESCRIPTION)

    task_calls = [c for c in fake.calls if c[1] == f"/tasks/{TASK_WITH_DESCRIPTION}"]
    assert [c[0] for c in task_calls] == ["GET", "POST"]
    # The body posted is the whole task, not a patch.
    posted = task_calls[1][2]
    assert posted is not None
    assert posted["description"] == LONG_DESCRIPTION
    assert posted["title"].startswith("#33")


def test_close_task_keeps_the_title_too():
    fake = FakeVikunja()
    before = stored(fake, TASK_WITH_DESCRIPTION)["title"]
    client(fake).close_task(TASK_WITH_DESCRIPTION)
    assert stored(fake, TASK_WITH_DESCRIPTION)["title"] == before


def test_a_mutation_that_damages_the_description_is_caught():
    """The assertion is the point: a future edit to a mutate() cannot go quiet."""
    fake = FakeVikunja()

    def careless(task):
        task["done"] = True
        task["description"] = ""

    with pytest.raises(DescriptionLost) as exc:
        client(fake).update_task(TASK_WITH_DESCRIPTION, careless)

    assert "recover" in str(exc.value).lower()
    assert str(len(LONG_DESCRIPTION)) in str(exc.value)


def test_set_description_replaces_and_verifies():
    fake = FakeVikunja()
    new = "<p>Rewritten.</p>"
    updated = client(fake).set_description(TASK_WITH_DESCRIPTION, new)

    assert updated["description"] == new
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == new
    # Still a whole-object write: the title survived the description change.
    assert stored(fake, TASK_WITH_DESCRIPTION)["title"].startswith("#33")


def test_set_description_refuses_to_blank_a_task():
    fake = FakeVikunja()
    with pytest.raises(VikunjaError):
        client(fake).set_description(TASK_WITH_DESCRIPTION, "   ")
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == LONG_DESCRIPTION


def test_set_description_notices_a_server_side_rewrite():
    class Rewriting(FakeVikunja):
        def _replace(self, task_id, body):
            body = dict(body, description=(body.get("description") or "") + "<!--x-->")
            return super()._replace(task_id, body)

    fake = Rewriting()
    with pytest.raises(VikunjaError) as exc:
        client(fake).set_description(TASK_WITH_DESCRIPTION, "<p>Rewritten.</p>")
    assert "differs from what was sent" in str(exc.value)


def test_set_task_fields_replaces_the_title_and_keeps_the_description():
    fake = FakeVikunja()
    client(fake).set_task_fields(TASK_WITH_DESCRIPTION, title="#33 Renamed")

    assert stored(fake, TASK_WITH_DESCRIPTION)["title"] == "#33 Renamed"
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == LONG_DESCRIPTION


def test_set_task_fields_replaces_both_in_one_write():
    """One write, so the task never holds the new title and the old body."""
    fake = FakeVikunja()
    client(fake).set_task_fields(
        TASK_WITH_DESCRIPTION, title="#33 Renamed", description_html="<p>New.</p>"
    )

    posts = [c for c in fake.calls if c[0] == "POST" and c[1].startswith("/tasks/")]
    assert len(posts) == 1
    assert stored(fake, TASK_WITH_DESCRIPTION)["title"] == "#33 Renamed"
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == "<p>New.</p>"


def test_set_task_fields_refuses_to_blank_either_field():
    fake = FakeVikunja()
    for kwargs in ({"title": "  "}, {"description_html": " \n "}, {}):
        with pytest.raises(VikunjaError):
            client(fake).set_task_fields(TASK_WITH_DESCRIPTION, **kwargs)
    assert stored(fake, TASK_WITH_DESCRIPTION)["description"] == LONG_DESCRIPTION
    assert stored(fake, TASK_WITH_DESCRIPTION)["title"].startswith("#33")


def test_a_title_only_change_still_trips_the_description_guard():
    """The one field it is not touching is the one that has been lost before."""

    class Dropping(FakeVikunja):
        def _replace(self, task_id, body):
            return super()._replace(task_id, dict(body, description=""))

    with pytest.raises(DescriptionLost):
        client(Dropping()).set_task_fields(TASK_WITH_DESCRIPTION, title="#33 Renamed")


def test_set_task_fields_notices_a_server_side_rewrite():
    class Rewriting(FakeVikunja):
        def _replace(self, task_id, body):
            return super()._replace(task_id, dict(body, title=body["title"] + " (sic)"))

    with pytest.raises(VikunjaError) as exc:
        client(Rewriting()).set_task_fields(TASK_WITH_DESCRIPTION, title="#33 Renamed")
    assert "stored title differs" in str(exc.value)


def test_create_task_returns_a_task_with_an_id():
    fake = FakeVikunja()
    created = client(fake).create_task(PROJECT_ID, "New ticket", "<p>Body.</p>")

    assert created["id"] > 0
    assert created["title"] == "New ticket"
    assert stored(fake, created["id"])["description"] == "<p>Body.</p>"


def test_update_task_rejects_a_non_task_response():
    class Odd(FakeVikunja):
        def __call__(self, method, path, body=None):
            if method == "GET" and path == f"/tasks/{TASK_WITH_DESCRIPTION}":
                return None
            return super().__call__(method, path, body)

    with pytest.raises(VikunjaError):
        client(Odd()).close_task(TASK_WITH_DESCRIPTION)
