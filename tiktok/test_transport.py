"""Transport regressions; network responses are supplied at the curl boundary."""
import json
from types import SimpleNamespace

import pytest

from tiktok import fetch, parsers, users


def test_search_bootstraps_from_search_page_and_retries_first_signed_call(monkeypatch):
    paths = []
    replies = [
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        '{"__DEFAULT_SCOPE__":{"webapp.app-context":{"wid":"123"}}}</script>',
        "",
        json.dumps({"status_code": 0, "item_list": [], "has_more": 0}),
    ]

    def get(url, **kwargs):
        paths.append(url.split("?", 1)[0])
        return SimpleNamespace(status_code=200, text=replies.pop(0))

    client = SimpleNamespace(session=SimpleNamespace(get=get, cookies={}), country="us",
                             device_id="old", born=0, booted=False, answered=False, spent={})
    monkeypatch.setattr(fetch, "_acquire", lambda *args: client)
    monkeypatch.setattr(fetch, "_release", lambda client: None)
    payload = fetch.api("/api/search/item/full/", {"keyword": "nasi goreng"}, boot=True)
    assert payload["status_code"] == 0
    assert paths == [fetch.WWW + "/search", fetch.WWW + "/api/search/item/full/",
                     fetch.WWW + "/api/search/item/full/"]
    assert client.booted and client.device_id == "123"


def test_deadline_clips_requests_and_is_not_retried(monkeypatch):
    now = [100.0]
    timeouts = []
    closed = []
    monkeypatch.setattr(fetch.time, "monotonic", lambda: now[0])

    def get(url, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 3
        raise OSError("sensitive proxy URL")

    client = SimpleNamespace(session=SimpleNamespace(get=get, cookies={}), country="us",
                             device_id="123", born=0, answered=False, close=lambda: closed.append(True))
    monkeypatch.setattr(fetch, "_acquire", lambda *args: client)
    with pytest.raises(fetch.TikTokTimeout), fetch.deadline(2):
        fetch.api("/api/item/detail/", {}, countries=("us", "de"))
    assert timeouts == [2]
    assert closed == [True]
    assert fetch._timeout(30) == 30


def test_nested_deadline_and_parallel_work_cannot_extend_budget(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: now[0])
    with fetch.deadline(2):
        with fetch.deadline(20):
            assert fetch.run_parallel([lambda: fetch._timeout(30), lambda: fetch._timeout(30)]) == [2, 2]
        assert fetch._timeout(30) == 2
    assert fetch._timeout(30) == 30


def test_profile_does_not_fallback_after_timeout(monkeypatch):
    users._lookups.clear()
    def timeout(*args, **kwargs):
        raise fetch.TikTokTimeout("deadline expired")
    def unexpected(*args, **kwargs):
        pytest.fail("a timed out lookup must not start a page fallback")
    monkeypatch.setattr(users, "api", timeout)
    monkeypatch.setattr(users, "get_page", unexpected)
    with pytest.raises(fetch.TikTokTimeout):
        users.lookup({"username": "tiktok"})


def test_transport_errors_do_not_disclose_credentials():
    error = fetch._transport_error(OSError("http://user:secret@proxy/private"))
    assert str(error) == "request failed: OSError"


def test_playback_describes_required_headers():
    media = parsers.video_file({"playAddr": "https://www.tiktok.com/aweme/v1/play/?video_id=example"})
    assert media["request_headers"] == {"Referer": "https://www.tiktok.com/"}
