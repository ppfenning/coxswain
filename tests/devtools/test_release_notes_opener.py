from devtools.release_notes_opener import (
    BANNED_MARKS,
    BANNED_WORDS,
    check_opener,
    draft_opener,
    opener_prompt,
)

GOOD = "The release added a flag. It fixed a crash. It dropped an old option."
BAD = "The release added a flag."
SIX = " ".join(["It changed a thing."] * 6)


class Fake:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies[len(self.prompts) - 1]


def test_banned_lists_match_the_prose_pack():
    assert BANNED_WORDS == ("genuinely", "exactly", "precisely", "robust", "comprehensive", "thorough")
    assert BANNED_MARKS == ("—", "(", "->", "→")


def test_prompt_carries_version_bullets_and_bans_without_mutation():
    bullets = ["Added a flag", "Fixed a crash"]
    prompt = opener_prompt("0.38.0", bullets)
    assert "0.38.0" in prompt
    assert all(bullet in prompt for bullet in bullets)
    assert all(item in prompt for item in BANNED_WORDS + BANNED_MARKS)
    assert "3 to 5 sentences" in prompt and "past tense" in prompt
    assert bullets == ["Added a flag", "Fixed a crash"]


def test_clean_three_and_four_sentence_texts_pass():
    assert check_opener(GOOD) == ()
    assert check_opener(GOOD + " It tidied the docs.") == ()


def test_six_sentences_and_two_sentences_are_flagged():
    assert len(check_opener(SIX)) == 1
    assert len(check_opener("One. Two.")) == 1
    assert check_opener("") != ()


def test_a_version_number_does_not_split_a_sentence():
    assert check_opener("Version 0.38.0 shipped. It fixed a crash. It added a flag.") == ()


def test_em_dash_is_flagged():
    assert check_opener(GOOD.replace("flag.", "flag — a big one.")) == ("banned mark: —",)


def test_each_other_mark_is_flagged():
    for mark in ("(", "->", "→"):
        assert check_opener(GOOD + f" Note {mark} here.") == (f"banned mark: {mark}",)


def test_banned_word_is_whole_word_and_case_insensitive():
    assert check_opener(GOOD.replace("old", "Robust")) == ("banned word: robust",)
    assert check_opener(GOOD.replace("old", "thoroughly")) == ()


def test_good_opener_is_returned_after_one_call():
    complete = Fake([GOOD])
    assert draft_opener("0.38.0", ["a"], complete) == GOOD
    assert len(complete.prompts) == 1


def test_bad_then_good_is_retried_with_violations_appended():
    complete = Fake([BAD, GOOD])
    assert draft_opener("0.38.0", ["a"], complete) == GOOD
    assert len(complete.prompts) == 2
    assert complete.prompts[1].startswith(complete.prompts[0])
    assert check_opener(BAD)[0] in complete.prompts[1]


def test_two_bad_replies_give_none():
    complete = Fake([BAD, SIX])
    assert draft_opener("0.38.0", ["a"], complete) is None
    assert len(complete.prompts) == 2


def test_zero_retries_gives_none_after_one_call():
    complete = Fake([BAD])
    assert draft_opener("0.38.0", ["a"], complete, retries=0) is None
    assert len(complete.prompts) == 1
