"""Match slugs — the shareable handle a player types to find a table."""
import pytest

from plugins.bdv.bdv.services import slug as slug_service


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Amber Hawk 42", "amber-hawk-42"),
            ("  Friday Game  ", "friday-game"),
            ("UPPER_CASE_thing", "upper-case-thing"),
            ("weird!!chars??here", "weird-chars-here"),
            ("--leading-and-trailing--", "leading-and-trailing"),
            ("multiple   spaces", "multiple-spaces"),
        ],
    )
    def test_normalises_to_a_typable_handle(self, raw, expected):
        assert slug_service.normalise(raw) == expected

    def test_caps_the_length(self):
        assert len(slug_service.normalise("x" * 200)) <= slug_service.MAX_SLUG_LENGTH

    def test_handles_empty_input(self):
        assert slug_service.normalise("") == ""
        assert slug_service.normalise(None) == ""


class TestValidate:
    def test_accepts_and_returns_the_normalised_form(self):
        assert slug_service.validate("Friday Night Game") == "friday-night-game"

    @pytest.mark.parametrize("bad", ["", "ab", "!!", "  -  "])
    def test_rejects_unusable_slugs(self, bad):
        with pytest.raises(slug_service.InvalidSlugError):
            slug_service.validate(bad)


class TestGenerate:
    def test_generates_a_readable_three_part_slug(self):
        value = slug_service.generate()
        assert slug_service.SLUG_PATTERN.match(value)
        assert len(value.split("-")) == 3

    def test_avoids_slugs_already_taken(self):
        seen = set()

        def taken(candidate):
            if candidate in seen:
                return True
            seen.add(candidate)
            return False

        first = slug_service.generate(taken)
        second = slug_service.generate(taken)
        assert first != second

    def test_falls_back_rather_than_looping_forever(self):
        value = slug_service.generate(lambda _c: True)
        assert value.startswith("table-")
        assert slug_service.SLUG_PATTERN.match(value)

    def test_generated_slugs_are_safe_to_say_out_loud(self):
        """No leetspeak, no ambiguity — it gets read aloud in chat."""
        for _ in range(50):
            assert slug_service.SLUG_PATTERN.match(slug_service.generate())
