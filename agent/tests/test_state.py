from agent.state import AgentState, StateMachine


def test_happy_path():
    sm = StateMachine()
    assert sm.state is AgentState.STOPPED
    assert sm.transition(AgentState.STARTING)
    assert sm.transition(AgentState.CONNECTED)
    assert sm.transition(AgentState.DISCONNECTED)
    assert sm.transition(AgentState.CONNECTED)
    assert sm.transition(AgentState.STOPPED)


def test_illegal_transition_is_noop():
    sm = StateMachine()
    assert not sm.transition(AgentState.CONNECTED)  # STOPPED -> CONNECTED illegal
    assert sm.state is AgentState.STOPPED


def test_error_records_reason():
    sm = StateMachine()
    sm.transition(AgentState.STARTING)
    sm.fail("boom")
    assert sm.state is AgentState.ERROR
    assert sm.last_error == "boom"
    assert sm.transition(AgentState.STARTING)  # recovery allowed


def test_blocked_records_reason():
    sm = StateMachine()
    sm.transition(AgentState.STARTING)
    sm.transition(AgentState.CONNECTED)
    sm.block("cloudflare challenge")
    assert sm.state is AgentState.BLOCKED
    assert "cloudflare" in (sm.last_error or "")


def test_connected_clears_error():
    sm = StateMachine()
    sm.transition(AgentState.STARTING)
    sm.fail("x")
    sm.transition(AgentState.STARTING)
    sm.transition(AgentState.CONNECTED)
    assert sm.last_error is None


def test_snapshot_shape():
    sm = StateMachine()
    snap = sm.snapshot()
    assert snap["state"] == "STOPPED"
    assert snap["healthy"] is False
    assert "lastTransitionAt" in snap
