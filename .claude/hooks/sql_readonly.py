"""Is the SQL in a PreToolUse payload read-only?

Reads the hook payload on stdin and prints "read-only", or the first word that
stops it being provably so. Used by guard-sql-writes.sh. Any doubt is a
not-read-only answer: the caller turns that into a prompt, never a block.
"""
import json
import re
import sys

READ_STARTS = {"select", "with", "show", "explain", "values", "table"}
SET_TIMEOUT = re.compile(r"^set\s+(local\s+)?statement_timeout\b")
# Words that make an otherwise-SELECT statement write or reach outside the DB:
# data-modifying CTEs, SELECT INTO, EXPLAIN ANALYZE <write>, FOR UPDATE, and
# functions with side effects.
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|copy|"
    r"into|call|nextval|setval|set_config|pg_terminate_backend|pg_cancel_backend|"
    r"pg_reload_conf|lo_import|lo_export|lo_unlink|pg_read_file|"
    r"pg_read_binary_file|pg_ls_dir|pg_stat_file|dblink\w*)\b"
)
DOLLAR_TAG = re.compile(r"\$(\w*)\$")


def _escape_string_prefix(sql: str, quote: int) -> bool:
    """Does the quote at ``quote`` open an E'..' or U&'..' literal?"""
    def word(k: int) -> bool:
        return k >= 0 and (sql[k].isalnum() or sql[k] == "_")
    if quote >= 1 and sql[quote - 1] in "eE" and not word(quote - 2):
        return True
    return sql[max(0, quote - 2):quote].upper() == "U&" and not word(quote - 3)


def strip(sql: str) -> str:
    """Lower-case SQL with comments removed and literals blanked, so a word
    inside a string or a comment is never read as a keyword."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"":
            if c == "'" and _escape_string_prefix(sql, i):
                # E'..' / U&'..' end where backslash escapes say, not where
                # this scan would: refuse to judge rather than misread them.
                # A plain '..' is literal (standard_conforming_strings, the
                # Postgres default), so '\s+' in a regex is fine.
                raise ValueError("escape-string")
            j = i + 1
            while j < n:
                if sql[j] == c and j + 1 < n and sql[j + 1] == c:
                    j += 2
                    continue
                if sql[j] == c:
                    break
                j += 1
            out.append(" '' ")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "$" and DOLLAR_TAG.match(sql, i):
            tag = DOLLAR_TAG.match(sql, i).group(0)
            j = sql.find(tag, i + len(tag))
            out.append(" '' ")
            i = n if j < 0 else j + len(tag)
        else:
            out.append(c)
            i += 1
    return "".join(out).lower()


def verdict(sql: str) -> str:
    try:
        text = strip(sql)
    except ValueError as e:
        return str(e)
    for stmt in text.split(";"):
        stmt = stmt.strip()
        if not stmt or SET_TIMEOUT.match(stmt):
            continue
        first = re.split(r"[\s(]", stmt, maxsplit=1)[0]
        if first not in READ_STARTS:
            return re.sub(r"\W", "", first)[:20] or "statement"
        hit = WRITE_WORDS.search(stmt)
        if hit:
            return hit.group(1)
    return "read-only"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        query = (payload.get("tool_input") or {}).get("query")
    except (ValueError, AttributeError):
        query = None
    print(verdict(query) if isinstance(query, str) else "no-query")


if __name__ == "__main__":
    main()
