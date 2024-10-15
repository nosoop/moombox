#!/usr/bin/python3

import sqlite3
from contextvars import ContextVar

# holds the database context so it can be used across modules without circular imports
database_ctx: ContextVar[sqlite3.Connection | None] = ContextVar("database", default=None)
