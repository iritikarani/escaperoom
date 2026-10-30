import pytest

from answer_validator import canonical, is_correct, normalize, validate_guess


@pytest.mark.parametrize("guess", ["BLUE KEY", "blue key", "Blue Key", "  Blue Key  ",
                                   "blue   key", "Blue-Key!", "blue key.", "The Blue Key",
                                   "bluekey", "Blúe Kéy"])
def test_equivalent_forms_accepted(guess):
    assert is_correct(guess, ["blue key"])


@pytest.mark.parametrize("guess", ["blue keys", "red key", "blue", "key", "blue key door", "bleu key"])
def test_different_answers_rejected(guess):
    assert not is_correct(guess, ["blue key"])


def test_multiple_accepted_answers():
    assert is_correct("wall clock", ["clock", "wall clock"])
    assert is_correct("A clock", ["clock", "wall clock"])
    assert not is_correct("watch", ["clock", "wall clock"])


def test_numbers_with_separators():
    for guess in ["1369", "1 3 6 9", "1-3-6-9", "1,369", " 1369 "]:
        assert is_correct(guess, ["1369"]), guess
    assert not is_correct("1396", ["1369"])
    assert not is_correct("13690", ["1369"])


def test_apostrophes_and_accents():
    assert normalize("Don’t PANIC") == "dont panic"
    assert canonical("Café") == canonical("cafe")


def test_lone_article_is_not_stripped():
    assert canonical("the") == "the"
    assert not is_correct("the", ["a"])


@pytest.mark.parametrize("raw", [None, "", "   ", "!!!", "x" * 201])
def test_invalid_guesses_rejected(raw):
    ok, error = validate_guess(raw)
    assert not ok and error


def test_empty_guess_never_correct():
    assert not is_correct("   ", ["blue key"])
    assert not is_correct("?!", ["blue key"])
