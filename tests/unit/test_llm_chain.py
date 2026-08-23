"""The provider chain: fall-through, circuit breaking, and never lying.

The chain exists because free providers are unreliable in every direction at
once — rate limits, blocked hosts, retired model ids, compatibility layers that
reject parameters they advertise. What it must never do is turn any of that into
a wrong answer: it either returns something that validated against the requested
schema, or it reports that nothing did.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest
from pydantic import BaseModel

from yukti.llm import cache as cache_mod
from yukti.llm.chain import AllProvidersFailed, LayeredClient
from yukti.llm.errors import Disposition, classify
from yukti.llm.registry import BY_NAME, PROVIDERS, resolve_order


class Answer(BaseModel):
    verdict: str


def _cache(tmp: pathlib.Path, enabled: bool = False) -> cache_mod.ResponseCache:
    return cache_mod.ResponseCache(directory=tmp, enabled=enabled)


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield pathlib.Path(d)


class FakeClient:
    """A provider that behaves however the test needs."""

    def __init__(self, *, answer=None, raises=None, name="fake") -> None:
        self.answer = answer
        self.raises = raises
        self.calls = 0
        self.name = name

    def complete(self, **kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.answer, {"input": 1, "output": 1}, "fake-model"


def _chain(tmpdir, providers: dict[str, FakeClient], order: list[str]):
    chain = LayeredClient(order=order, cache=_cache(tmpdir))
    chain._clients = dict(providers)
    # Every named provider is treated as configured; the test controls the
    # behaviour through its fake client rather than through the environment.
    for state in chain.states:
        state.spec = type(state.spec)(
            **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
               "keyless": True}
        )
    return chain


class Boom(Exception):
    def __init__(self, status: int | None = None, msg: str = "boom") -> None:
        super().__init__(msg)
        self.status_code = status


# --- registry ---------------------------------------------------------------

class TestRegistry:
    def test_every_provider_has_a_reachable_shape(self):
        for spec in PROVIDERS:
            assert spec.name
            assert spec.default_fast and spec.default_planner
            assert spec.protocol in ("openai", "anthropic")
            if spec.protocol == "openai":
                assert spec.base_url, f"{spec.name} needs a base_url"

    def test_only_ollama_is_keyless(self):
        """A provider wrongly marked keyless would be tried on every call."""
        keyless = {s.name for s in PROVIDERS if s.keyless}
        assert keyless == {"ollama"}

    def test_an_empty_order_means_the_default_not_nothing(self):
        """An unset env var must not silently disable the whole chain."""
        assert len(resolve_order("")) == len(PROVIDERS)
        assert len(resolve_order(None)) == len(PROVIDERS)
        assert len(resolve_order([])) == len(PROVIDERS)

    def test_unknown_provider_names_are_dropped_not_fatal(self):
        names = [p.name for p in resolve_order("gemini,typo,ollama")]
        assert names == ["gemini", "ollama"]

    def test_model_ids_are_overridable_per_provider(self, monkeypatch):
        """Provider model ids drift; a stale default must be fixable by config."""
        spec = BY_NAME["groq"]
        assert spec.model_for("fast") == spec.default_fast
        monkeypatch.setenv("YUKTI_GROQ_MODEL_FAST", "some-new-model")
        assert spec.model_for("fast") == "some-new-model"


# --- error classification ---------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_permanent_rejections_disable_the_provider(self, status):
        assert classify("p", Boom(status)).disposition is Disposition.DISABLE

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_transient_rejections_fall_through_without_disabling(self, status):
        assert classify("p", Boom(status)).disposition is Disposition.FALL_THROUGH

    def test_a_schema_miss_is_retried_before_moving_on(self):
        """Sampling varies — one retry is worth more than a provider switch."""
        assert classify("p", ValueError("bad shape")).disposition is Disposition.RETRY

    def test_a_proxy_policy_denial_is_treated_as_unreachable(self):
        """The build environment answers 403 to CONNECT for blocked hosts."""
        failure = classify("groq", Boom(None, "gateway answered 403 to CONNECT"))
        assert failure.disposition is Disposition.DISABLE

    def test_a_400_falls_through_rather_than_being_fatal(self):
        """Compatibility layers 400 on parameters they do not implement.

        Treating that as fatal would lose every remaining provider over one
        vendor's unsupported `response_format`.
        """
        assert classify("p", Boom(400)).disposition is Disposition.FALL_THROUGH


# --- the chain --------------------------------------------------------------

class TestChain:
    def test_the_first_working_provider_answers(self, tmpdir):
        good = FakeClient(answer=Answer(verdict="ok"))
        chain = _chain(tmpdir, {"gemini": good}, ["gemini"])

        result = chain.complete(system="s", prompt="p", schema=Answer)

        assert result.parsed.verdict == "ok"
        assert result.provider == "gemini"
        assert good.calls == 1

    def test_it_falls_through_to_the_next_provider(self, tmpdir):
        dead = FakeClient(raises=Boom(429))
        good = FakeClient(answer=Answer(verdict="second"))
        chain = _chain(tmpdir, {"gemini": dead, "groq": good}, ["gemini", "groq"])

        result = chain.complete(system="s", prompt="p", schema=Answer)

        assert result.provider == "groq"
        assert dead.calls == 1 and good.calls == 1

    def test_a_disabled_provider_is_not_tried_again(self, tmpdir):
        """The circuit breaker. Without it, every call pays the dead provider's
        connect timeout — which in an environment that blocks most providers
        makes the chain slower than having no chain."""
        dead = FakeClient(raises=Boom(403))
        good = FakeClient(answer=Answer(verdict="ok"))
        chain = _chain(tmpdir, {"gemini": dead, "groq": good}, ["gemini", "groq"])

        chain.complete(system="s", prompt="p", schema=Answer)
        chain.complete(system="s", prompt="p2", schema=Answer)

        assert dead.calls == 1, "a permanently-failed provider was retried"
        assert good.calls == 2

    def test_a_rate_limited_provider_is_tried_again_on_a_later_call(self, tmpdir):
        """429 is temporary. Disabling on it would lose the best provider for
        the rest of the run over one burst."""
        limited = FakeClient(raises=Boom(429))
        good = FakeClient(answer=Answer(verdict="ok"))
        chain = _chain(tmpdir, {"gemini": limited, "groq": good}, ["gemini", "groq"])

        chain.complete(system="s", prompt="p", schema=Answer)
        chain.complete(system="s", prompt="p2", schema=Answer)

        assert limited.calls == 2

    def test_a_schema_miss_is_retried_once_on_the_same_provider(self, tmpdir):
        flaky = FakeClient(raises=ValueError("not json"))
        chain = _chain(tmpdir, {"gemini": flaky}, ["gemini"])

        with pytest.raises(AllProvidersFailed):
            chain.complete(system="s", prompt="p", schema=Answer)

        assert flaky.calls == 2, "expected exactly one retry before moving on"

    def test_all_failures_are_reported_together(self, tmpdir):
        chain = _chain(tmpdir, {"gemini": FakeClient(raises=Boom(429)),
                                "groq": FakeClient(raises=Boom(500))},
                       ["gemini", "groq"])

        with pytest.raises(AllProvidersFailed) as exc:
            chain.complete(system="s", prompt="p", schema=Answer)

        assert len(exc.value.failures) == 2
        assert "gemini" in str(exc.value) and "groq" in str(exc.value)

    def test_an_unconfigured_provider_opens_no_socket(self, tmpdir):
        """What makes a nine-provider chain cheap rather than a nine-way timeout."""
        chain = LayeredClient(order=["gemini", "groq"], cache=_cache(tmpdir))
        exploding = FakeClient(raises=AssertionError("a socket was opened"))
        chain._clients = {"gemini": exploding, "groq": exploding}
        for state in chain.states:
            assert not state.spec.configured()   # no keys in the test env

        with pytest.raises(AllProvidersFailed) as exc:
            chain.complete(system="s", prompt="p", schema=Answer)

        assert exploding.calls == 0
        assert all("not configured" in f.reason for f in exc.value.failures)

    def test_it_never_returns_an_unvalidated_answer(self, tmpdir):
        """No relaxing the schema to get *something* back."""
        chain = _chain(tmpdir, {"gemini": FakeClient(raises=ValueError("shape"))},
                       ["gemini"])
        with pytest.raises(AllProvidersFailed):
            chain.complete(system="s", prompt="p", schema=Answer)


# --- cache ------------------------------------------------------------------

class TestCache:
    def test_a_repeated_question_costs_nothing(self, tmpdir):
        good = FakeClient(answer=Answer(verdict="cached"))
        chain = LayeredClient(order=["gemini"], cache=_cache(tmpdir, enabled=True))
        chain._clients = {"gemini": good}
        for state in chain.states:
            state.spec = type(state.spec)(
                **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
                   "keyless": True})

        first = chain.complete(system="s", prompt="p", schema=Answer)
        second = chain.complete(system="s", prompt="p", schema=Answer)

        assert good.calls == 1, "the second identical question hit the provider"
        assert second.from_cache and not first.from_cache
        assert second.parsed.verdict == "cached"

    def test_the_key_ignores_the_provider(self, tmpdir):
        """Switching providers must not invalidate work already done — any
        provider can serve any call, which is the point of the chain."""
        a = cache_mod.key_for("s", "p", "Answer", "planner")
        b = cache_mod.key_for("s", "p", "Answer", "planner")
        assert a == b

    def test_a_changed_prompt_misses(self, tmpdir):
        assert (cache_mod.key_for("s", "p1", "Answer", "planner")
                != cache_mod.key_for("s", "p2", "Answer", "planner"))

    def test_a_changed_schema_misses(self, tmpdir):
        assert (cache_mod.key_for("s", "p", "Answer", "planner")
                != cache_mod.key_for("s", "p", "Other", "planner"))

    def test_a_stale_entry_is_a_miss_not_a_wrong_answer(self, tmpdir):
        """A cached payload that no longer fits the schema must not be served."""
        cache = _cache(tmpdir, enabled=True)
        key = cache_mod.key_for("s", "p", "Answer", "planner")
        cache.put(key, {"totally": "different"}, "gemini", "m")

        good = FakeClient(answer=Answer(verdict="fresh"))
        chain = LayeredClient(order=["gemini"], cache=cache)
        chain._clients = {"gemini": good}
        for state in chain.states:
            state.spec = type(state.spec)(
                **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
                   "keyless": True})

        result = chain.complete(system="s", prompt="p", schema=Answer)
        assert result.parsed.verdict == "fresh"
        assert not result.from_cache

    def test_a_corrupt_entry_is_a_miss_not_a_crash(self, tmpdir):
        cache = _cache(tmpdir, enabled=True)
        (tmpdir / "deadbeef.json").write_text("{not json")
        assert cache.get("deadbeef") is None


# --- reporting --------------------------------------------------------------

def test_report_distinguishes_no_key_from_disabled(tmpdir):
    """A chain silently degrading to fallbacks looks identical to a working one."""
    chain = _chain(tmpdir, {"gemini": FakeClient(raises=Boom(403))},
                   ["gemini", "groq"])
    chain.states[1].spec = BY_NAME["groq"]      # back to needing a key

    with pytest.raises(AllProvidersFailed):
        chain.complete(system="s", prompt="p", schema=Answer)

    report = {r["provider"]: r["status"] for r in chain.report()}
    assert report["gemini"].startswith("disabled")
    assert report["groq"] == "no key"


class TestValidationHook:
    """A well-formed answer is not necessarily a usable one.

    The RCA specialist rejects a narrative citing an evidence id it was never
    shown. That answer passes the schema perfectly, so without a hook that runs
    before the write, it got cached, re-served and re-rejected forever — pinning
    the question to the fallback while the model was never asked again. Safe,
    and quietly worse over time.
    """

    def test_a_rejected_answer_is_not_cached(self, tmpdir):
        cache = _cache(tmpdir, enabled=True)
        good = FakeClient(answer=Answer(verdict="rejected-by-caller"))
        chain = LayeredClient(order=["gemini"], cache=cache)
        chain._clients = {"gemini": good}
        for state in chain.states:
            state.spec = type(state.spec)(
                **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
                   "keyless": True})

        with pytest.raises(AllProvidersFailed):
            chain.complete(system="s", prompt="p", schema=Answer,
                           validate=lambda _: False)

        assert cache.stats()["entries"] == 0, "a rejected answer was written to disk"

    def test_the_next_call_re_asks_the_provider(self, tmpdir):
        """The point of not caching it: the model gets another chance."""
        cache = _cache(tmpdir, enabled=True)
        client = FakeClient(answer=Answer(verdict="v"))
        chain = LayeredClient(order=["gemini"], cache=cache)
        chain._clients = {"gemini": client}
        for state in chain.states:
            state.spec = type(state.spec)(
                **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
                   "keyless": True})

        with pytest.raises(AllProvidersFailed):
            chain.complete(system="s", prompt="p", schema=Answer,
                           validate=lambda _: False)
        calls_after_rejection = client.calls

        # Same question again, this time accepted.
        result = chain.complete(system="s", prompt="p", schema=Answer,
                                validate=lambda _: True)

        assert client.calls > calls_after_rejection, "the provider was never re-asked"
        assert result.parsed.verdict == "v"
        assert not result.from_cache

    def test_a_rejection_falls_through_to_the_next_provider(self, tmpdir):
        """Another model may do better, so a rejection is a provider failure."""
        first = FakeClient(answer=Answer(verdict="bad"))
        second = FakeClient(answer=Answer(verdict="good"))
        chain = _chain(tmpdir, {"gemini": first, "groq": second}, ["gemini", "groq"])

        seen: list[str] = []

        def only_good(answer: Answer) -> bool:
            seen.append(answer.verdict)
            return answer.verdict == "good"

        result = chain.complete(system="s", prompt="p", schema=Answer,
                                validate=only_good)

        assert seen == ["bad", "good"]
        assert result.provider == "groq"

    def test_an_accepted_answer_is_cached_normally(self, tmpdir):
        cache = _cache(tmpdir, enabled=True)
        good = FakeClient(answer=Answer(verdict="fine"))
        chain = LayeredClient(order=["gemini"], cache=cache)
        chain._clients = {"gemini": good}
        for state in chain.states:
            state.spec = type(state.spec)(
                **{**{f: getattr(state.spec, f) for f in state.spec.__slots__},
                   "keyless": True})

        chain.complete(system="s", prompt="p", schema=Answer, validate=lambda _: True)
        second = chain.complete(system="s", prompt="p", schema=Answer,
                                validate=lambda _: True)

        assert good.calls == 1
        assert second.from_cache

    def test_no_hook_means_no_extra_validation(self, tmpdir):
        """The parameter is optional; omitting it must change nothing."""
        good = FakeClient(answer=Answer(verdict="ok"))
        chain = _chain(tmpdir, {"gemini": good}, ["gemini"])
        assert chain.complete(system="s", prompt="p", schema=Answer).parsed.verdict == "ok"
