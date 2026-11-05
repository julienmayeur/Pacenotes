"""The sync JSON contract: validation and the exact request sent to the API."""

import json

import anthropic
import httpx2
import pytest

from pacenote.contract import ContractError, SyncResult, empty_result, parse_sync_result
from pacenote.llm import FALLBACK_BETA, ClaudeEngine, SyncFailed

from .conftest import edit, fill, upd

VALID = {
    "question_updates": [upd("import-flow", "answered", "10-12 semaines usine → entrepôt", "dix à douze semaines")],
    "slots": [fill("import-replenishment", "volume", "Une commande usine par semaine")],
    "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []},
    "recipe_matches": [{"recipe_id": "customer-order-intake", "use_case": "bakery-orders", "fit": "partial",
                        "heard_signals": ["orders arrive by email or PDF and someone re-keys them into the ERP"],
                        "missing": ["read access to the mailbox the orders arrive in"]}],
    "queue_edits": [edit("add", "import-delays", "import", "Qu'est-ce qui allonge ces délais ?",
                         "What makes those lead times longer?", "import-flow")],
    "next_questions": ["stock-visibility", "customers-ordering", "aftersales-warranty"],
    "coverage": [{"theme": "import", "score": 0.7, "note": "transporteur à préciser"}],
    "plan_delta": {"context": ["Distributeur Fornaxa depuis 8 ans"], "priorities": [], "workflows": [],
                   "quick_wins": [], "risks": [], "budget": [], "open_points": []},
    "clean_passages": [{"start": "00:47", "speaker": "client", "lang": "fr", "text": "On commande à l'usine…"}],
    "enough_data": False,
    "enough_data_reason": "SAV et ROI pas encore couverts",
}


def test_valid_payload_parses_from_dict_string_and_fenced_string():
    for raw in (VALID, json.dumps(VALID), "```json\n" + json.dumps(VALID) + "\n```"):
        r = parse_sync_result(raw)
        assert isinstance(r, SyncResult)
        assert r.question_updates[0].status == "answered"
        assert r.queue_edits[0].op == "add"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("enough_data"),  # missing required field
        lambda d: d.update(extra_field=1),  # unknown key
        lambda d: d["question_updates"][0].update(status="locked"),  # status outside the enum
        lambda d: d["queue_edits"][0].update(op="rename"),  # op outside the enum
        lambda d: d["slots"][0].update(slot="budget"),  # not an integration slot
        lambda d: d["plan_delta"].pop("risks"),  # nested required field
        lambda d: d["clean_passages"][0].update(speaker="ceo"),
        lambda d: d.update(enough_data="maybe"),
        lambda d: d.pop("recipe_matches"),  # strict: the recipe matches are required (may be empty)
        lambda d: d["recipe_matches"][0].update(fit="maybe"),
        lambda d: d["recipe_matches"][0].pop("use_case"),  # null, never omitted
        lambda d: d["recipe_matches"][0].update(score=1),
    ],
)
def test_contract_violations_are_rejected(mutate):
    bad = json.loads(json.dumps(VALID))
    mutate(bad)
    with pytest.raises(ContractError):
        parse_sync_result(bad)


def test_not_json_is_rejected():
    with pytest.raises(ContractError):
        parse_sync_result("Sure! Here is the update: {")


def test_schema_is_strict_everywhere():
    schema = SyncResult.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj.get("title")
        assert set(obj["required"]) == set(obj["properties"]), obj.get("title")
    status = schema["$defs"]["QuestionUpdate"]["properties"]["status"]["enum"]
    assert status == ["answered", "partial", "pending"]


# --- the request actually sent to the Anthropic API (no network: mock transport) ---


def _engine(reply_text: str, stop_reason: str = "end_turn", **kw):
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": reply_text}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return ClaudeEngine(client=client, **kw), seen


def test_sync_request_uses_structured_output_contract(state):
    engine, seen = _engine(json.dumps(VALID))
    out = engine.sync("SYSTEM", "USER", state=state, delta=[])
    assert out.queue_edits[0].question_id == "import-delays"
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(SyncResult.model_fields)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    # only text goes out: no audio/file content blocks of any kind
    assert all(isinstance(m["content"], str) for m in body["messages"])
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in seen["headers"]["anthropic-beta"]


def test_model_and_fallbacks_are_configurable(state):
    engine, seen = _engine(json.dumps(VALID), model="claude-sonnet-5-5", sync_effort="low", use_fallbacks=False)
    engine.sync("S", "U", state=state, delta=[])
    assert seen["body"]["model"] == "claude-sonnet-5-5"
    assert seen["body"]["output_config"]["effort"] == "low"
    assert "fallbacks" not in seen["body"]


def test_refusal_and_truncation_raise_sync_failed(state):
    engine, _ = _engine(json.dumps(VALID), stop_reason="refusal")
    with pytest.raises(SyncFailed):
        engine.sync("S", "U", state=state, delta=[])
    engine, _ = _engine('{"question_updates": [', stop_reason="max_tokens")
    with pytest.raises(SyncFailed):
        engine.sync("S", "U", state=state, delta=[])


def test_empty_result_is_contract_valid():
    assert parse_sync_result(empty_result().model_dump_json()) == empty_result()
