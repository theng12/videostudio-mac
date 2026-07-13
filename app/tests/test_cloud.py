"""Cloud video gateway — routing, catalog merge, and spend guardrails."""
import pytest


def test_cloud_id_dispatch(isolated):
    from backend.providers import registry
    assert registry.is_cloud_id("fal:fal-ai/veo3")
    assert not registry.is_cloud_id("Lightricks/LTX-Video")     # local repo
    assert not registry.is_cloud_id("unknownprovider:model")    # provider not registered
    pair = registry.provider_for_id("fal:fal-ai/veo3")
    assert pair is not None
    prov, model = pair
    assert prov.key == "fal" and model.provider == "fal"


def test_catalog_merge_shape(isolated):
    from backend.providers import registry
    models = registry.cloud_models_serialized()
    assert models, "expected curated fal models"
    m = next(x for x in models if x["repo"].startswith("fal:"))
    # Local-catalog-compatible + Hub fields must all be present.
    for k in ("repo", "label", "family", "capabilities", "size_gb",
              "is_cloud", "hub_modality", "provider", "cost_tier", "price", "status"):
        assert k in m, f"missing {k}"
    assert m["is_cloud"] is True
    assert m["hub_modality"] == "video"
    assert m["cost_tier"] == "paid-cloud"
    assert m["size_gb"] == 0
    assert "cloud-fal" in registry.cloud_families()


def test_estimate_cost_per_second_and_per_video(isolated):
    from backend.providers import registry
    prov, veo = registry.provider_for_id("fal:fal-ai/veo3")            # per_second 0.75
    assert prov.estimate_cost(veo, {"duration": 8}) == round(0.75 * 8, 4)
    pair = registry.provider_for_id("fal:fal-ai/ltx-video-13b-distilled")  # per_video 0.04
    assert pair[0].estimate_cost(pair[1], {"duration": 5}) == 0.04


def test_gate_blocks_over_cap_allows_under(isolated):
    from backend import spend
    isolated["spend_caps"] = {"global": {"daily": 5.0, "monthly": 0}, "per_provider": {}}
    with pytest.raises(spend.CapExceeded):
        spend.check_gate("fal", 6.0)
    spend.check_gate("fal", 3.0)   # under the cap → allowed (no raise)


def test_gate_after_booking(isolated):
    from backend import spend
    isolated["spend_caps"] = {"global": {"daily": 5.0, "monthly": 0}, "per_provider": {}}
    spend.check_gate("fal", 3.0)
    sid = spend.record_submit("fal", "fal:x", "job1", 3.0)
    with pytest.raises(spend.CapExceeded):
        spend.check_gate("fal", 3.0)   # 3 booked + 3 = 6 > 5
    spend.record_finish(sid, actual_usd=3.0, duration_s=4, state="done")
    assert spend.summary()["global"]["today"] == 3.0


def test_per_provider_cap_is_scoped(isolated):
    from backend import spend
    isolated["spend_caps"] = {"global": {"daily": 0, "monthly": 0},
                              "per_provider": {"fal": {"daily": 2.0, "monthly": 0}}}
    with pytest.raises(spend.CapExceeded):
        spend.check_gate("fal", 3.0)
    spend.check_gate("other", 3.0)     # a different provider isn't bound by fal's cap


def test_errored_job_releases_its_estimate(isolated):
    from backend import spend
    isolated["spend_caps"] = {"global": {"daily": 5.0, "monthly": 0}, "per_provider": {}}
    sid = spend.record_submit("fal", "fal:x", "j", 4.0)
    spend.record_finish(sid, actual_usd=None, duration_s=None, state="error")
    spend.check_gate("fal", 4.0)       # errored spend no longer counts → passes
    assert spend.summary()["global"]["today"] == 0.0


def test_set_caps_coerces_and_persists(isolated):
    from backend import spend
    spend.set_caps({"global": {"daily": "10", "monthly": -5}, "per_provider": {"fal": {"daily": 3}}})
    caps = isolated["spend_caps"]
    assert caps["global"]["daily"] == 10.0
    assert caps["global"]["monthly"] == 0.0            # negative coerced to 0
    assert caps["per_provider"]["fal"]["daily"] == 3.0


def test_spend_summary_includes_14_day_history(isolated):
    from backend import spend
    sid = spend.record_submit("fal", "fal:x", "job1", 1.25)
    spend.record_finish(sid, actual_usd=1.0, duration_s=4.0, state="done")
    history = spend.summary()["daily_history"]
    assert len(history) == 14
    assert history[-1]["total"] == 1.0
    assert history[-1]["providers"] == {"fal": 1.0}
