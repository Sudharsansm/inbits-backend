from app.topics import classify_topic


def test_classifies_sports():
    assert classify_topic("India wins the cricket World Cup final", "") == "Sports"


def test_classifies_technology():
    assert classify_topic("New AI startup raises funding for chip design", "") == "Technology"


def test_classifies_politics():
    assert classify_topic("Parliament passes new election law", "") == "Politics"


def test_falls_back_to_general():
    assert classify_topic("A quiet afternoon in the park", "Nothing much happened") == "General"


def test_uses_excerpt_and_tags_too():
    assert classify_topic("Update", "The vaccine rollout continues nationwide", []) == "Health"
