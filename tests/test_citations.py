# classify_blocks over dict fixtures — no SDK, no network, no disk.
#
# The load-bearing assertion here is that the visited-URL set comes back
# NORMALISED, because that set is the input to verify_source_url in
# test_driver_guards.py. A bug that leaves it empty (every write refused) or
# over-full (the guard defeated) has to fail here rather than in a demo.

import pytest

from app.citations import (attach_snippets, classify_blocks, dataset_record,
                           display_label, merge_records, normalise_url,
                           records_from_tool_result, upgrade_session_sources,
                           web_record)

URL = "https://www.example.com/commodities/chicken-meal"
NORM = normalise_url(URL)


def search_result_block(*urls):
    return {"type": "web_search_tool_result",
            "content": [{"type": "web_search_result", "url": u, "title": f"Title {i}",
                         "encrypted_content": "OPAQUE-BLOB"}
                        for i, u in enumerate(urls)]}


def fetch_result_block(url, retrieved_at="2026-08-03T09:15:00Z", title="Fetched page"):
    return {"type": "web_fetch_tool_result",
            "content": {"type": "web_fetch_result", "url": url,
                        "retrieved_at": retrieved_at,
                        "content": {"type": "document", "title": title}}}


# ---------- Server tool blocks never reach local dispatch ----------

def test_server_tool_blocks_are_never_returned_as_local_tool_uses():
    blocks = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "chicken meal"}},
        search_result_block(URL),
        {"type": "server_tool_use", "name": "web_fetch", "input": {"url": URL}},
        fetch_result_block(URL),
        {"type": "text", "text": "Prices rose."},
        {"type": "thinking", "thinking": "considering"},
    ]
    res = classify_blocks(blocks)
    assert res["local_tool_uses"] == []


def test_only_real_tool_use_blocks_are_dispatched_locally():
    blocks = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "x"}},
        {"type": "tool_use", "id": "tu_1", "name": "cost_buildup", "input": {}},
    ]
    res = classify_blocks(blocks)
    assert [b["name"] for b in res["local_tool_uses"]] == ["cost_buildup"]


def test_research_events_carry_the_query_or_url():
    blocks = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "wheat price"}},
        {"type": "server_tool_use", "name": "web_fetch", "input": {"url": URL}},
    ]
    res = classify_blocks(blocks)
    assert res["research"][0]["query"] == "wheat price"
    assert res["research"][1]["url"] == URL


def test_empty_and_none_content_are_handled():
    for value in ([], None):
        res = classify_blocks(value)
        assert res["local_tool_uses"] == []
        assert res["source_records"] == []
        assert res["fetched_urls"] == set()


# ---------- Error objects: HTTP 200, error in the block ----------

def test_a_search_error_object_yields_a_web_error_and_does_not_crash():
    blocks = [{"type": "web_search_tool_result",
               "content": {"type": "web_search_tool_result_error",
                           "error_code": "max_uses_exceeded"}}]
    res = classify_blocks(blocks)
    assert res["web_errors"] == [{"tool": "web_search", "error_code": "max_uses_exceeded"}]
    assert res["source_records"] == []
    assert res["fetched_urls"] == set()


def test_a_fetch_error_object_yields_a_web_error():
    blocks = [{"type": "web_fetch_tool_result",
               "content": {"type": "web_fetch_tool_result_error",
                           "error_code": "url_not_accessible"}}]
    res = classify_blocks(blocks)
    assert res["web_errors"] == [{"tool": "web_fetch", "error_code": "url_not_accessible"}]


def test_a_successful_result_is_a_list_and_produces_no_error():
    res = classify_blocks([search_result_block(URL)])
    assert res["web_errors"] == []
    assert len(res["source_records"]) == 1


def test_errors_and_successes_can_coexist_in_one_turn():
    blocks = [
        search_result_block(URL),
        {"type": "web_fetch_tool_result",
         "content": {"type": "web_fetch_tool_result_error", "error_code": "too_many_requests"}},
    ]
    res = classify_blocks(blocks)
    assert len(res["source_records"]) == 1
    assert len(res["web_errors"]) == 1


# ---------- The visited-URL set is normalised ----------

def test_visited_url_set_is_normalised():
    res = classify_blocks([search_result_block("https://WWW.Example.com/commodities/chicken-meal/")])
    assert res["fetched_urls"] == {NORM}


def test_search_and_fetch_of_one_url_collapse_to_one_record_and_one_visited_entry():
    blocks = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "chicken meal"}},
        search_result_block(URL + "?utm_source=newsletter"),
        {"type": "server_tool_use", "name": "web_fetch", "input": {"url": URL}},
        fetch_result_block(URL + "/"),
    ]
    res = classify_blocks(blocks)
    assert len(res["source_records"]) == 1, "one page must yield one chip, not two"
    assert res["fetched_urls"] == {NORM}
    # web_fetch wins as the `via`: we actually read that page.
    assert res["source_records"][0]["via"] == "web_fetch"
    assert res["source_records"][0]["accessed"] == "2026-08-03"


def test_distinct_urls_stay_distinct():
    res = classify_blocks([search_result_block(URL, "https://other.example/wheat")])
    assert len(res["source_records"]) == 2
    assert len(res["fetched_urls"]) == 2


def test_first_appearance_order_survives_dedup():
    a, b, c = "https://a.example/1", "https://b.example/2", "https://c.example/3"
    res = classify_blocks([search_result_block(a, b, c), search_result_block(c, a)])
    assert [r["url"] for r in res["source_records"]] == [a, b, c]


def test_encrypted_content_is_never_stored():
    res = classify_blocks([search_result_block(URL)])
    assert "encrypted_content" not in res["source_records"][0]
    assert "OPAQUE-BLOB" not in str(res["source_records"][0])


# ---------- normalise_url ----------

@pytest.mark.parametrize("variant", [
    URL, URL + "/", URL + "#section", URL + "?utm_campaign=x&utm_source=y",
    URL + "?ref=nav", "https://example.com/commodities/chicken-meal",
    "HTTPS://WWW.EXAMPLE.COM/commodities/chicken-meal",
])
def test_equivalent_forms_normalise_together(variant):
    assert normalise_url(variant) == NORM


def test_meaningful_query_params_are_kept_and_sorted():
    assert normalise_url("https://x.example/s?b=2&a=1") == normalise_url("https://x.example/s?a=1&b=2")
    assert normalise_url("https://x.example/s?a=1") != normalise_url("https://x.example/s?a=2")


def test_path_case_is_preserved_because_servers_may_be_case_sensitive():
    assert normalise_url("https://x.example/Path") != normalise_url("https://x.example/path")


def test_empty_input_normalises_to_empty_string():
    assert normalise_url(None) == ""
    assert normalise_url("") == ""


# ---------- Local tool results: render_chart contributes no source ----------

def test_a_tool_result_without_a_source_file_contributes_no_record():
    # render_chart is presentation only — it must never pollute the source list,
    # and the same must hold for every future presentation-only tool.
    assert records_from_tool_result("render_chart", {"ok": True, "points": 7}) == []


def test_a_tool_result_with_a_source_file_contributes_one_dataset_record():
    recs = records_from_tool_result("cost_buildup", {"source_file": "cost_buildup.parquet"})
    assert recs == [{"kind": "dataset", "id": "cost_buildup.parquet",
                     "label": "cost_buildup.parquet", "tool": "cost_buildup"}]


def test_an_error_result_contributes_no_record():
    assert records_from_tool_result("driver_status", {"error": "nope"}) == []
    assert records_from_tool_result("x", None) == []


# ---------- Legacy sessions upgrade cleanly ----------

def test_a_legacy_session_with_only_string_sources_upgrades():
    legacy = {"sources": ["budget_vs_actuals.parquet", "drivers.parquet"]}
    records = upgrade_session_sources(legacy)
    assert [r["kind"] for r in records] == ["dataset", "dataset"]
    assert [display_label(r) for r in records] == legacy["sources"]


def test_a_session_with_source_records_is_returned_as_is():
    rec = web_record(URL, title="Chicken meal index")
    session = {"sources": ["ignored.parquet"], "source_records": [rec]}
    assert upgrade_session_sources(session) == [rec]


def test_an_empty_session_upgrades_to_an_empty_list():
    assert upgrade_session_sources({}) == []
    assert upgrade_session_sources(None) == []


def test_display_label_prefers_title_then_url_for_web_records():
    assert display_label(web_record(URL, title="Chicken meal index")) == "Chicken meal index"
    assert display_label(web_record(URL)) == URL
    assert display_label(dataset_record("data/x.parquet")) == "x.parquet"


# ---------- Citations on text blocks ----------

def test_citations_are_extracted_from_text_blocks():
    blocks = [{"type": "text", "text": "Prices rose 28%.",
               "citations": [{"type": "web_search_result_location", "url": URL,
                              "title": "Index", "cited_text": "up 28% since February"}]}]
    res = classify_blocks(blocks)
    assert len(res["citations"]) == 1
    assert res["citations"][0]["id"] == NORM
    assert res["citations"][0]["cited_text"] == "up 28% since February"


def test_text_blocks_without_citations_contribute_none():
    assert classify_blocks([{"type": "text", "text": "plain"}])["citations"] == []


def test_attach_snippets_puts_cited_text_on_the_matching_record():
    res = classify_blocks([search_result_block(URL),
                           {"type": "text", "text": "x",
                            "citations": [{"url": URL + "/", "title": "T",
                                           "cited_text": "up 28%"}]}])
    records = attach_snippets(res["source_records"], res["citations"])
    assert records[0]["snippet"] == "up 28%"


def test_attach_snippets_ignores_citations_with_no_matching_record():
    records = [web_record(URL)]
    attach_snippets(records, [{"id": normalise_url("https://nowhere.example"),
                               "cited_text": "orphan"}])
    assert records[0]["snippet"] is None


# ---------- merge_records ----------

def test_merge_fills_missing_fields_from_the_later_record():
    a = web_record(URL, title=None, via="web_search")
    b = web_record(URL, title="Chicken meal index", via="web_fetch", accessed="2026-08-03")
    merged = merge_records([a, b])
    assert len(merged) == 1
    assert merged[0]["title"] == "Chicken meal index"
    assert merged[0]["via"] == "web_fetch"
    assert merged[0]["accessed"] == "2026-08-03"


def test_dataset_and_web_records_with_the_same_id_do_not_collide():
    recs = merge_records([dataset_record("x"), web_record("https://x.example")])
    assert len(recs) == 2
