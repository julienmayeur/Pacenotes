"""Rules for applying a sync result: answered questions are locked, status only moves forward,
queue edits only touch not-yet-answered questions."""

from pacenote.state import MAX_ADDS_PER_SYNC, NEXT_COUNT

from .conftest import edit, result, upd


def _ids(state):
    return [q.id for q in state.questions]


def test_initial_state(state):
    assert state.mode == "consent"
    assert state.focus_id == "strategy-success" and state.current_question.id == "strategy-success"
    assert len(state.next_questions) == NEXT_COUNT and "strategy-success" not in state.next_questions
    assert state.next_questions[0] == "strategy-pain"
    assert all(v == 0.0 for v in state.coverage.values())


def test_status_moves_forward_and_answer_is_extracted(state):
    state.apply_sync_result(result(question_updates=[upd("import-flow", "partial", "Commande usine Italie")]))
    q = state.get("import-flow")
    assert (q.status, q.answer) == ("partial", "Commande usine Italie")
    state.apply_sync_result(result(question_updates=[upd("import-flow", "answered", "Italie, 10-12 semaines", "dix à douze")]))
    assert q.status == "answered" and q.locked
    assert q.answer == "Italie, 10-12 semaines" and q.evidence == "dix à douze"


def test_status_cannot_go_backwards(state):
    state.apply_sync_result(result(question_updates=[upd("stock-tools", "partial", "Excel")]))
    rep = state.apply_sync_result(result(question_updates=[upd("stock-tools", "pending")]))
    assert state.get("stock-tools").status == "partial"
    assert rep.rejected and "cannot go back" in rep.rejected[0].detail


def test_locked_question_is_immutable(state):
    state.apply_sync_result(result(question_updates=[upd("roi-budget", "answered", "50-80 k€")]))
    q = state.get("roi-budget")
    before = q.model_copy()
    rep = state.apply_sync_result(result(
        question_updates=[upd("roi-budget", "partial", "autre chose"), upd("roi-budget", "answered", "100 k€")],
        queue_edits=[
            edit("reword", "roi-budget", fr="Nouveau texte ?", en="New text?"),
            edit("remove", "roi-budget"),
            edit("reorder", "roi-budget", after="strategy-success"),
        ],
    ))
    assert q == before
    assert len(rep.rejected) == 5
    assert {r.kind for r in rep.rejected} == {"status", "reword", "remove", "reorder"}
    assert all(not r.applied for r in rep.rejected)


def test_locked_question_never_offered_as_next(state):
    state.apply_sync_result(result(question_updates=[upd("strategy-success", "answered", "Fin de la ressaisie")]))
    assert "strategy-success" not in state.next_questions
    assert state.focus_id == "strategy-success"  # stays on screen until the FDE moves on
    rep = state.apply_sync_result(result(next_questions=["strategy-success", "stock-visibility", "import-flow"]))
    assert state.next_questions[0] == "stock-visibility"
    assert "strategy-success" not in state.next_questions
    assert len(state.next_questions) == NEXT_COUNT  # topped up from the queue
    state.mark_answered("stock-visibility")
    rep = state.apply_sync_result(result(next_questions=["stock-visibility", "import-flow"]))
    assert "stock-visibility" not in state.next_questions
    assert any(r.kind == "next" and r.target == "stock-visibility" for r in rep.rejected)
    assert state.advance("test") == "import-flow"


def test_status_updates_apply_before_edits_in_the_same_sync(state):
    rep = state.apply_sync_result(result(
        question_updates=[upd("customers-ordering", "answered", "Mail + téléphone")],
        queue_edits=[edit("reword", "customers-ordering", fr="Reformulée ?", en="Reworded?")],
    ))
    assert state.get("customers-ordering").text_fr.startswith("Comment vos boulangeries")
    assert rep.rejected[0].kind == "reword"


def test_reword_open_question_keeps_history(state):
    old = state.get("stock-tools").text_fr
    state.apply_sync_result(result(queue_edits=[edit("reword", "stock-tools", fr="Le stock est-il dans l'ERP ?",
                                                     en="Is stock in the ERP?")]))
    q = state.get("stock-tools")
    assert q.text_fr == "Le stock est-il dans l'ERP ?" and q.text_en == "Is stock in the ERP?"
    assert q.previous_texts == [old]


def test_reword_partial_question_is_allowed(state):
    state.apply_sync_result(result(question_updates=[upd("aftersales-parts", "partial", "plusieurs semaines")]))
    rep = state.apply_sync_result(result(queue_edits=[edit("reword", "aftersales-parts", fr="Combien de semaines ?",
                                                           en="How many weeks?")]))
    assert not rep.rejected and state.get("aftersales-parts").text_fr == "Combien de semaines ?"


def test_remove_only_pending(state):
    state.apply_sync_result(result(question_updates=[upd("data-quality", "partial", "doublons clients")]))
    rep = state.apply_sync_result(result(queue_edits=[edit("remove", "data-quality"), edit("remove", "logistics-tracking")]))
    assert state.get("data-quality").status == "partial"  # partial answer kept
    assert state.get("logistics-tracking").status == "removed"
    assert [r.target for r in rep.rejected] == ["data-quality"]
    assert "logistics-tracking" not in state.next_questions
    # a removed question cannot come back through a status update
    rep = state.apply_sync_result(result(question_updates=[upd("logistics-tracking", "answered", "x")]))
    assert state.get("logistics-tracking").status == "removed" and rep.rejected


def test_add_question_after_anchor_and_unique_id(state):
    state.apply_sync_result(result(queue_edits=[
        edit("add", "import-flow", "import", "Qu'est-ce qui retarde les camions ?", "What delays the trucks?",
             after="import-flow"),
    ]))
    ids = _ids(state)
    new_id = ids[ids.index("import-flow") + 1]
    assert new_id == "import-flow-2"  # collided with an existing id -> made unique
    q = state.get(new_id)
    assert q.origin == "added" and q.status == "pending" and q.theme == "import"


def test_add_with_null_after_goes_to_head_of_queue(state):
    state.apply_sync_result(result(question_updates=[upd("strategy-success", "answered", "x")]))
    state.apply_sync_result(result(queue_edits=[edit("add", "urgent", "it_security", "Sauvegardes testées ?", None)]))
    assert state.queue()[0].id == "urgent"
    assert state.get("urgent").text_en == "Sauvegardes testées ?"  # missing language filled from the other


def test_add_rejections(state):
    n = len(state.questions)
    rep = state.apply_sync_result(result(queue_edits=[
        edit("add", "x1", "no-such-theme", "Q ?", "Q?"),
        edit("add", "x2", "import", "", ""),
        *[edit("add", f"ok{i}", "import", f"Q{i} ?", f"Q{i}?") for i in range(MAX_ADDS_PER_SYNC + 1)],
    ]))
    assert len(state.questions) == n + MAX_ADDS_PER_SYNC
    assert len(rep.rejected) == 3


def test_reorder_moves_open_question_without_touching_locked_ones(state):
    state.apply_sync_result(result(question_updates=[upd("strategy-success", "answered", "x")]))
    locked_before = state.get("strategy-success").model_copy()
    state.apply_sync_result(result(queue_edits=[edit("reorder", "roi-budget", after=None)]))
    assert state.queue()[0].id == "roi-budget"
    state.apply_sync_result(result(queue_edits=[edit("reorder", "data-quality", after="roi-budget")]))
    assert [q.id for q in state.queue()][:2] == ["roi-budget", "data-quality"]
    assert state.get("strategy-success") == locked_before
    rep = state.apply_sync_result(result(queue_edits=[edit("reorder", "roi-budget", after="roi-budget"),
                                                      edit("reorder", "roi-budget", after="nope")]))
    assert len(rep.rejected) == 2


def test_unknown_ids_are_rejected_not_fatal(state):
    rep = state.apply_sync_result(result(
        question_updates=[upd("nope", "answered", "x")],
        queue_edits=[edit("reword", "nope", fr="a", en="b"), edit("remove", None)],
    ))
    assert len(rep.rejected) == 3
    assert state.sync_count == 1


def test_coverage_clamped_and_computed_fallback(state):
    state.apply_sync_result(result(
        question_updates=[upd("roi-budget", "answered", "x"), upd("roi-expectation", "partial", "y")],
        coverage=[{"theme": "import", "score": 1.7, "note": "n"}, {"theme": "ghost", "score": 0.5, "note": ""}],
    ))
    assert state.coverage["import"] == 1.0
    assert state.coverage["roi"] == 0.5  # computed: (1 + 0.5 + 0) / 3
    assert "ghost" not in state.coverage
    assert state.coverage_notes["import"] == "n"


def test_plan_delta_accumulates_without_duplicates(state):
    pd = {"context": ["8 ans de distribution"], "priorities": [], "workflows": [], "quick_wins": ["Portail"],
          "risks": [], "budget": [], "open_points": []}
    state.apply_sync_result(result(plan_delta=pd))
    state.apply_sync_result(result(plan_delta={**pd, "context": ["8 ANS de distribution", "600 boulangeries"]}))
    assert state.plan_notes["context"] == ["8 ans de distribution", "600 boulangeries"]
    assert state.plan_notes["quick_wins"] == ["Portail"]


def test_enough_data_is_sticky_and_switches_mode(state):
    state.mode = "interview"
    state.apply_sync_result(result(enough_data=True, enough_data_reason="tout est couvert"))
    assert state.enough_data and state.mode == "finalize"
    state.apply_sync_result(result(enough_data=False))
    assert state.enough_data and state.mode == "finalize"


def test_manual_overrides(state):
    assert state.mark_answered("strategy-success")
    assert not state.mark_answered("strategy-success")  # already locked
    assert "strategy-success" not in state.next_questions
    state.apply_sync_result(result(question_updates=[upd("strategy-pain", "partial", "x")]))
    assert not state.skip("strategy-pain")  # partial: not skippable
    assert state.skip("strategy-size")
    assert state.get("strategy-size").status == "removed"


def test_edit_log_records_everything(state):
    state.apply_sync_result(result(
        question_updates=[upd("roi-budget", "answered", "x")],
        queue_edits=[edit("remove", "roi-budget")],
    ))
    kinds = [(e.kind, e.applied) for e in state.edit_log]
    assert ("status", True) in kinds and ("remove", False) in kinds
