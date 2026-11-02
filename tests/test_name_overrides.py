from highlights.recording.sequences import name_override_commands


def test_empty_dict_returns_empty_list():
    """No players means no commands."""
    assert name_override_commands({}) == []


def test_single_entry():
    """A normal SteamID64 -> name pair produces one command."""
    assert name_override_commands({"76561198000000001": "Ace"}) == [
        'mirv_replace_name byXuid add x76561198000000001 "Ace"'
    ]


def test_names_with_spaces_are_kept_and_quoted():
    """Spaces survive cleaning and the name stays wrapped in quotes."""
    assert name_override_commands({"76561198000000002": "Sir Nice Guise"}) == [
        'mirv_replace_name byXuid add x76561198000000002 "Sir Nice Guise"'
    ]


def test_quotes_backslashes_and_semicolons_are_stripped():
    """Console-breaking characters are removed from the name."""
    assert name_override_commands({"76561198000000003": 'A"b\\c;d'}) == [
        'mirv_replace_name byXuid add x76561198000000003 "Abcd"'
    ]


def test_non_numeric_steamid_is_skipped():
    """An id that is not all digits produces no command."""
    assert name_override_commands({"BOT01": "Ace"}) == []


def test_name_empty_after_cleaning_is_skipped():
    """A name that cleans down to nothing produces no command."""
    assert name_override_commands({"76561198000000004": ' " \\ ;  '}) == []
    assert name_override_commands({"76561198000000005": "   "}) == []


def test_insertion_order_preserved():
    """Commands follow the dict's insertion order."""
    commands = name_override_commands(
        {
            "76561198000000007": "Third",
            "76561198000000006": "First",
            "BAD": "Skipped",
            "76561198000000005": "Second",
        }
    )
    assert commands == [
        'mirv_replace_name byXuid add x76561198000000007 "Third"',
        'mirv_replace_name byXuid add x76561198000000006 "First"',
        'mirv_replace_name byXuid add x76561198000000005 "Second"',
    ]
