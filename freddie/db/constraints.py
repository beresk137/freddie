from peewee import SQL

DEFAULT_EMPTY_STR = SQL("DEFAULT ''")
DEFAULT_EMPTY_DICT = SQL("DEFAULT '{}'")
DEFAULT_EMPTY_LIST = SQL("DEFAULT '[]'")
DEFAULT_ZERO = SQL("DEFAULT 0")