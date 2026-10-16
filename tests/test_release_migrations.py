import pytest

from devtools.release_migrations import (
    MigrationError,
    migrate_requirement_met,
    parse_declaration,
    validate_migrations,
)


def test_a_declaration_returns_the_ids_in_order():
    notes = "# 0.41.0\n\nMigrations: split-lake-store, rename-slot-keys\n"
    assert parse_declaration(notes) == ("split-lake-store", "rename-slot-keys")


def test_no_migrations_line_is_no_declaration():
    assert parse_declaration("# 0.41.0\n\nThe word Migrations: mid-line is prose.\n") is None


def test_migrations_none_is_an_empty_declaration():
    assert parse_declaration("Migrations: none\n") == ()


def test_a_bare_migrations_line_is_an_error():
    assert isinstance(parse_declaration("Migrations:\n"), MigrationError)


def test_an_id_with_a_space_is_an_error():
    assert isinstance(parse_declaration("Migrations: split lake, other\n"), MigrationError)


def test_a_duplicate_id_is_an_error_naming_it():
    assert validate_migrations(("a", "b", "a")) == MigrationError("duplicate migration id: a")


def test_an_empty_name_is_an_error():
    assert isinstance(validate_migrations(("a", "")), MigrationError)


def test_a_clean_list_and_an_empty_list_are_valid():
    assert validate_migrations(("a", "b")) is None
    assert validate_migrations(()) is None


@pytest.mark.parametrize(
    ("result", "met"),
    [("pass", True), ("fail", False), ("skip", False), (None, False)],
)
def test_declared_migrations_are_met_only_by_pass(result, met):
    assert migrate_requirement_met(("a",), result) is met


@pytest.mark.parametrize("result", ["pass", "fail", "skip", None])
def test_undeclared_migrations_are_met_by_any_result(result):
    assert migrate_requirement_met((), result) is True
