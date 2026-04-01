from app.core.whitelist import is_allowed

def test_wife_id_not_allowed():
    # If this fails, she IS allowed.
    assert not is_allowed('tg_185625742')
