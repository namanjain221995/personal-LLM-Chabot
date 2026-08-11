import os

from syncworker.config import load_object_configs, load_settings

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

#: The CRM objects every deployment is expected to carry. The full list is now
#: imported from each org's own export, so pinning an exact set would just
#: break every time someone legitimately adds an object — what matters is that
#: the core ones never quietly disappear.
CORE_OBJECTS = {"Account", "Contact", "Lead", "Opportunity", "Case", "User"}


def test_the_core_crm_objects_are_configured():
    names = {o.name for o in load_object_configs(CONFIG_PATH)}
    missing = CORE_OBJECTS - names
    assert not missing, f"core objects dropped from the config: {sorted(missing)}"


def test_the_shipped_config_is_valid_and_not_empty():
    objects = load_object_configs(CONFIG_PATH)
    assert len(objects) >= len(CORE_OBJECTS)
    assert len({o.name for o in objects}) == len(objects), "duplicate object entries"


def test_every_object_has_id_and_its_declared_watermark():
    for obj in load_object_configs(CONFIG_PATH):
        assert "Id" in obj.fields
        if obj.watermark_field is not None:
            assert obj.watermark_field in obj.fields


def test_rag_fields_are_subset_of_fields():
    for obj in load_object_configs(CONFIG_PATH):
        for f in obj.rag_fields:
            assert f in obj.fields


def test_embedding_api_key_is_read_without_affecting_endpoint(monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "ephemeral-local-key")
    monkeypatch.setenv("EMBED_VIA", "http://host.docker.internal:8089/v1/")
    settings = load_settings()
    assert settings.embed_api_key == "ephemeral-local-key"
    assert settings.embed_via == "http://host.docker.internal:8089/v1"
    assert "ephemeral-local-key" not in repr(settings)
