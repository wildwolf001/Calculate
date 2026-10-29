from app.llm.client import extract_json


def test_extract_json_uses_first_complete_object():
    text = (
        '{"intent_type":"query","slots":{}}'
        "\nAdditional explanation that must not make the valid object fail"
    )
    assert extract_json(text) == {"intent_type": "query", "slots": {}}


def test_extract_json_tolerates_surrounding_prose():
    text = 'prefix {"ok": true, "items": [1, 2]} suffix'
    assert extract_json(text) == {"ok": True, "items": [1, 2]}
