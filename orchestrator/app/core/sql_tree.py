"""What DuckDB's own parser says a statement reads and does.

Every regex over a statement has been one spelling away from a real passport number:
`warehouse.T`, `FROM 'T'`, `"query_table"('T')`, `GROUP BY 1`. So the facts that decide
what is run come from `json_serialize_sql`, on a connection of its own that holds no
data and can reach nothing. A shape this module does not know is refused, because that
JSON is DuckDB's internal format and a new version may add shapes:
tests/test_warehouse_statements.py pins the labels it reads.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Collection, Dict, Iterator, Mapping, Optional, Set, Tuple

log = logging.getLogger(__name__)

#: The table functions a statement may call. Anything else may read past the mask:
#: `histogram_values('T', col)`, `read_duckdb(path)`, `pragma_storage_info('T')`,
#: `json_execute_serialized_sql(...)`, and whatever a new DuckDB adds.
_ALLOWED_TABLE_FUNCTIONS = frozenset({"unnest", "range", "generate_series"})
#: Scalar functions that answer about the server and not about any record. `get_block_size`
#: and every `pg_` macro are scalar spellings of the views refused below: `pg_get_viewdef`
#: reads `duckdb_views()`. Names, never a `current_` prefix: `current_date()` must keep working.
_SERVER_FUNCTIONS = frozenset({"current_setting", "current_query", "getvariable", "version", "get_block_size", "format_type"})
#: A value turned into bytes or another spelling leaves every rule that reads text blind:
#: `encode(Email)` was held back by none of them, and the base64 of a link is no link.
_ENCODERS = frozenset({
    "encode", "decode", "hex", "to_hex", "unhex", "from_hex", "bin", "to_binary", "unbin", "from_binary",
    "base64", "to_base64", "from_base64", "url_encode", "url_decode", "bitstring", "bitstring_agg", "create_sort_key",
})
_ENCODED_TYPES = frozenset({"BLOB", "BIT"})
#: Where a table can honestly be read from. Not `raw`: its columns are all text and it holds
#: the plumbing, so `raw.T` read past every rule and `MAX(amount)` compared strings.
_ALLOWED_QUALIFIERS = frozenset({"", "main", "warehouse", "information_schema", "pg_catalog"})
#: Views that say things about the server, such as the path of the file it reads.
_SERVER_VIEWS = ("duckdb_", "pragma_", "sqlite_", "pg_")
_TABLE_KINDS = frozenset({"BASE_TABLE", "JOIN", "SUBQUERY", "TABLE_FUNCTION", "EXPRESSION_LIST", "EMPTY"})
#: The DuckDB whose trees this reads. A different one moves labels and every check here
#: reads wrong: on 2.0.0 a held column counted as refused and a CTE's LIMIT as "every row".
_READS_TREES_OF = "1.5."
#: Past this a recursive walk can run out of stack after the statement ran. Each nested
#: `+` costs about 2 levels; `IN` of 5,000 items is 9 levels and 20 nested subqueries 67.
_MAX_TREE_DEPTH = 200

_guard = threading.Lock()
_reader = None


class StatementRefused(ValueError):
    """Not a statement this engine runs. The text goes to the writer's retry, so it says what to do."""


def _read_json(sql: str) -> dict:
    import duckdb  # lazy, same as the engines

    if not duckdb.__version__.startswith(_READS_TREES_OF):
        log.error("sql_tree refuses every statement: it reads DuckDB %s trees, this is %s", _READS_TREES_OF, duckdb.__version__)
        raise StatementRefused("This statement cannot be checked. Write a plain SELECT.")
    global _reader
    with _guard:
        if _reader is None:
            _reader = duckdb.connect(
                ":memory:",
                config={
                    "enable_external_access": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                },
            )
        cursor = _reader.cursor()
    try:
        return json.loads(cursor.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    finally:
        cursor.close()


def read(sql: str) -> Optional[dict]:
    """The statement's tree, or None when it does not parse.

    None lets it run, so DuckDB refuses it in its own words and the retry reads those.
    Anything else that cannot be read is refused: a PIVOT in a subquery runs, and
    leaks, and comes back here as "Only SELECT statements can be serialized".
    """
    try:
        tree = _read_json(sql)
    except Exception as exc:
        raise StatementRefused("This statement cannot be checked. Write a plain SELECT.") from exc
    if tree.get("error"):
        if tree.get("error_type") == "parser":
            return None
        raise StatementRefused("This statement cannot be checked. Write a plain SELECT with no PIVOT.")
    return tree


def refuse_too_deep(tree: Optional[dict]) -> None:
    """For the writer's statement only: a wrapper adds depth, and a refused wrapper reads as a cut result.

    Measured without recursion, because the walks later in the turn would die on what this refuses.
    """
    pending = [(tree, 1)] if tree else []
    while pending:
        value, depth = pending.pop()
        if depth > _MAX_TREE_DEPTH:
            raise StatementRefused("This statement is nested too deeply. Write a plainer SELECT.")
        for item in value.values() if isinstance(value, dict) else value:
            if isinstance(item, (dict, list)):
                pending.append((item, depth + 1))


def nodes(value) -> Iterator[dict]:
    """Every object in the tree."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def refuse_unseen_reads(tree: dict) -> None:
    """What no mask can stand in front of: a second statement, or a read it cannot see."""
    if len(tree.get("statements") or []) != 1:
        raise StatementRefused("Write exactly one statement.")
    for node in nodes(tree):
        kind = node.get("type")
        # Every node: CAST(CAST([x] AS BLOB[]) AS BIT[]) has a LIST at the top.
        if str(node.get("id")) in _ENCODED_TYPES:
            raise StatementRefused("Show text as it is stored. Do not encode it or cast it to BLOB or BIT.")
        if node.get("class") == "FUNCTION":
            called = str(node.get("function_name") or "").lower()
            if called in _ENCODERS:
                raise StatementRefused("Show text as it is stored. Do not encode it or cast it to BLOB or BIT.")
            if called in _SERVER_FUNCTIONS or called.startswith(_SERVER_VIEWS):
                raise StatementRefused(f"The function {called} is not allowed. Use only the tables and columns you were given.")
        if "sample" in node and node["sample"]:
            raise StatementRefused("A sample gives a different number on every run. Do not use USING SAMPLE or TABLESAMPLE.")
        if "sample" in node and "alias" in node and kind not in _TABLE_KINDS:
            raise StatementRefused(f"This statement cannot read from a {kind}. Read each table by its bare name.")
        if kind == "TABLE_FUNCTION":
            name = str((node.get("function") or {}).get("function_name", "")).lower()
            if name not in _ALLOWED_TABLE_FUNCTIONS:
                raise StatementRefused(f"The table function {name} is not allowed. Read each table by its bare name.")
        elif kind == "BASE_TABLE":
            qualifiers = {str(node.get("catalog_name") or "").lower(), str(node.get("schema_name") or "").lower()}
            if qualifiers - _ALLOWED_QUALIFIERS:
                raise StatementRefused("Read each table by its bare name, with no catalog or schema before it.")
            if str(node.get("table_name") or "").lower().startswith(_SERVER_VIEWS):
                raise StatementRefused("That catalogue view is not allowed. Read each table by its bare name.")


def tables_read(tree: dict) -> Set[str]:
    """Every name read as a table, in lower case, its own CTE names among them."""
    return {str(node.get("table_name")).lower() for node in nodes(tree) if node.get("type") == "BASE_TABLE"}


def cte_names(tree: dict) -> Set[str]:
    """Every name a statement defines for itself, at any depth, in lower case."""
    return {
        str(entry.get("key")).lower()
        for node in nodes(tree)
        if isinstance(node.get("cte_map"), dict)
        for entry in node["cte_map"].get("map") or []
    }


def reads_past_a_name(tree: Optional[dict], names: Collection[str]) -> bool:
    """A table in `names` still written with a qualifier: no shadow stands in front of that."""
    if tree is None:
        return True
    return any(
        node.get("type") == "BASE_TABLE"
        and str(node.get("table_name")).lower() in names
        and (node.get("schema_name") or node.get("catalog_name"))
        for node in nodes(tree)
    )


# --------------------------------------------------------------------- held values --


class _Used(Exception):
    pass


#: What `_HeldValues.expression` knows how to walk. Anything else is a shape nobody has
#: read: `#22` passed as a harmless node and `COUNT(DISTINCT #22)` counted markers.
#: The parser writes that node itself for `ARRAY(SELECT col ...)`, so no text rule or
#: exemption for parser-written nodes closes it.
_EXPRESSION_CLASSES = frozenset({
    "BETWEEN", "CASE", "CAST", "COLLATE", "COLUMN_REF", "COMPARISON", "CONJUNCTION",
    "CONSTANT", "DEFAULT", "FUNCTION", "LAMBDA", "OPERATOR", "PARAMETER", "STAR",
    "SUBQUERY", "WINDOW",
})


def _column(node) -> str:
    return str((node.get("column_names") or [""])[-1]).lower()


def _string_constant(node) -> Optional[str]:
    if isinstance(node, dict) and node.get("class") == "CONSTANT":
        value = (node.get("value") or {}).get("value")
        if isinstance(value, str):
            return value
    return None


def _number_constant(node) -> Optional[int]:
    if isinstance(node, dict) and node.get("class") == "CONSTANT":
        value = (node.get("value") or {}).get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


class _HeldValues:
    """Walks a statement for any use of a held column but the few that stay right.

    Behind the mask a held column reads as one marker. Shown, it is dropped on the way
    out. `COUNT(col)` and "is it filled in" stay right, because only filled values
    change. Everything else counts markers: `COUNT(DISTINCT col)` says 1 however many are
    held, and `GROUP BY 1` says one group. Those are wrong numbers, so they are refused.
    """

    def __init__(self, held: Set[str], every_row: Set[str], columns: Dict[str, Set[str]]) -> None:
        self.held = held
        self.every_row = every_row
        self.columns = columns
        self.rows: Set[str] = set()  # what reads as a whole row where the walk is
        self.names = set(held)

    def is_held(self, node) -> bool:
        if not (isinstance(node, dict) and node.get("class") == "COLUMN_REF"):
            return False
        parts = node.get("column_names") or []
        bare = str(parts[0]).lower() if len(parts) == 1 else ""
        # DuckDB names a subquery with no alias unnamed_subquery, then unnamed_subquery2.
        return _column(node) in self.names or bare in self.rows or bare.startswith("unnamed_subquery")

    def from_names(self, table) -> Tuple[Set[str], Set[str]]:
        """What a FROM brings: its relations' names, and the real columns it exposes.

        A subquery or a WITH name exposes nothing this can vouch for.
        """
        kind = (table or {}).get("type")
        if kind == "JOIN":
            left, right = self.from_names(table.get("left")), self.from_names(table.get("right"))
            return left[0] | right[0], left[1] | right[1]
        if kind not in ("BASE_TABLE", "SUBQUERY"):
            return set(), set()
        name = str(table.get("table_name") or "").lower()
        return {str(table.get("alias") or name).lower()} - {""}, self.columns.get(name, set())

    def collect_aliases(self, root: dict) -> None:
        """The alias of a shown held column is held too, in every scope, and a shown row's name."""
        while True:
            before = len(self.names)
            for node in nodes(root):
                if node.get("type") == "SELECT_NODE":
                    self.rows = self.every_row - self.from_names(node.get("from_table"))[1]
                    for item in node.get("select_list") or []:
                        if self.is_held(item):
                            self.names.add(str(item.get("alias") or _column(item)).lower())
            if len(self.names) == before:
                self.rows = set()
                return

    def is_blank_test(self, node) -> bool:
        """`NULLIF(col, '')`: nothing but whether it is filled in."""
        return (
            isinstance(node, dict)
            and node.get("class") == "FUNCTION"
            and str(node.get("function_name")).lower() == "nullif"
            and len(node.get("children") or []) == 2
            and self.is_held(node["children"][0])
            and _string_constant(node["children"][1]) == ""
        )

    def is_presence_test(self, node: dict) -> bool:
        kind = node.get("type")
        children = node.get("children") or []
        if kind in ("OPERATOR_IS_NULL", "OPERATOR_IS_NOT_NULL"):
            return bool(children) and (self.is_held(children[0]) or self.is_blank_test(children[0]))
        if node.get("class") != "COMPARISON":
            return False
        left, right = node.get("left"), node.get("right")
        if kind in ("COMPARE_EQUAL", "COMPARE_NOTEQUAL"):
            for one, other in ((left, right), (right, left)):
                said = _string_constant(other)
                if self.is_held(one) and said is not None and said.lower() in ("", "true", "false"):
                    return True
                if (
                    isinstance(one, dict)
                    and one.get("type") == "OPERATOR_COALESCE"
                    and said == ""
                    and len(one.get("children") or []) == 2
                    and self.is_held(one["children"][0])
                    and _string_constant(one["children"][1]) == ""
                ):
                    return True
        # LENGTH(col) > 0 and = 0. The marker has a length, and so does any filled value.
        if kind in ("COMPARE_GREATERTHAN", "COMPARE_EQUAL") and _number_constant(right) == 0:
            return (
                isinstance(left, dict)
                and left.get("class") == "FUNCTION"
                and str(left.get("function_name")).lower() in ("length", "len")
                and len(left.get("children") or []) == 1
                and self.is_held(left["children"][0])
            )
        return False

    def expression(self, node) -> None:
        if isinstance(node, list):
            for item in node:
                self.expression(item)
            return
        if not isinstance(node, dict):
            return
        kind, group = node.get("type"), node.get("class")
        if group is not None and group not in _EXPRESSION_CLASSES:
            raise _Used
        if kind in ("SELECT_NODE", "SET_OPERATION_NODE"):
            self.query(node, nested=True)
            return
        if group == "COLUMN_REF":
            if self.is_held(node):
                raise _Used
            return
        if group == "STAR":  # row(*), COLUMNS('regex') in anything
            raise _Used
        if group == "CONSTANT":
            # t['col'] and struct_extract(t, 'col') read the column through a word.
            said = _string_constant(node)
            if said is not None and said.lower() in self.held:
                raise _Used
            return
        if group == "SUBQUERY" and node.get("subquery_type") == "EXISTS":
            self.query(node["subquery"]["node"])
            return
        if self.is_presence_test(node):
            return
        if group == "FUNCTION" and str(node.get("function_name")).lower() == "count" and not node.get("distinct"):
            children = node.get("children") or []
            if len(children) == 1 and (self.is_held(children[0]) or self.is_blank_test(children[0])):
                self.expression(node.get("filter"))
                return
        for value in node.values():
            if isinstance(value, (dict, list)):
                self.expression(value)

    def table(self, node) -> None:
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind in ("BASE_TABLE", "SUBQUERY") and node.get("column_name_alias"):
            raise _Used  # AS r(a, b): a new name for whatever stands in that place
        if kind == "JOIN":
            if node.get("ref_type") == "NATURAL":
                raise _Used
            if {str(name).lower() for name in node.get("using_columns") or []} & self.names:
                raise _Used
            self.table(node.get("left"))
            self.table(node.get("right"))
            self.expression(node.get("condition"))
        elif kind == "SUBQUERY":
            self.query(node["subquery"]["node"])
        elif kind not in ("BASE_TABLE", "EMPTY"):
            self.expression(node)

    def query(self, node: dict, *, nested: bool = False, in_set: bool = False) -> None:
        for entry in (node.get("cte_map") or {}).get("map") or []:
            value = entry.get("value") or {}
            if value.get("aliases"):
                raise _Used  # WITH t(k) AS ...: the same new name
            self.query(value["query"]["node"])
        kind = node.get("type")
        if kind == "SET_OPERATION_NODE":
            self.query(node["left"], nested=nested, in_set=True)
            self.query(node["right"], nested=nested, in_set=True)
            self.expression(node.get("modifiers"))
            return
        if kind != "SELECT_NODE":
            raise _Used
        # DuckDB binds a bare name to a column of this FROM, then to a relation named so
        # here, then outward. So it is a whole row unless a real table here has that column.
        outer = self.rows
        named, exposed = self.from_names(node.get("from_table"))
        self.rows = (outer | named) - exposed
        items = node.get("select_list") or []
        modifiers = node.get("modifiers") or []
        tells_apart = (
            in_set
            or nested
            or any(modifier.get("type") == "DISTINCT_MODIFIER" for modifier in modifiers)
            or bool(node.get("group_expressions") or node.get("group_sets"))
            or node.get("aggregate_handling") == "FORCE_AGGREGATES"
        )
        shown = []
        for item in items:
            if item.get("class") == "STAR":
                if item.get("columns") or item.get("replace_list") or item.get("rename_list"):
                    raise _Used
                shown.append(True)
            elif self.is_held(item):
                shown.append(True)
            else:
                shown.append(False)
                self.expression(item)
        if tells_apart and any(shown):
            raise _Used
        self.expression(node.get("group_expressions"))
        self.table(node.get("from_table"))
        for clause in ("where_clause", "having", "qualify"):
            self.expression(node.get(clause))
        for modifier in modifiers:
            if modifier.get("type") == "ORDER_MODIFIER":
                self.orders(modifier.get("orders") or [], items, shown)
            else:
                self.expression(modifier)
        self.rows = outer

    def orders(self, orders: list, items: list, shown: list) -> None:
        for order in orders:
            key = order.get("expression") or {}
            place = _number_constant(key)
            if place is not None:
                # ORDER BY 1 on a star cannot be told from here: the marker sorts the
                # same whatever it hides, and nothing was asked of the value.
                if 1 <= place <= len(items) and shown[place - 1] and items[place - 1].get("class") != "STAR":
                    raise _Used
            elif key.get("class") == "STAR" and key.get("columns") and key.get("expr") is None:
                if any(shown):  # ORDER BY ALL
                    raise _Used
            else:
                self.expression(key)


def reads_a_held_value(tree: dict, held: Collection[str], columns: Mapping[str, Collection[str]]) -> bool:
    """Is a held column used for anything that would count its marker as a value?

    `columns` is what each table read has: a lookup called what a table is called stays a lookup.
    """
    root = tree["statements"][0]["node"]
    # A relation's own name in a column's place is the whole row, marker and all.
    rows = {
        str(node.get("alias") or node.get("table_name") or "").lower()
        for node in nodes(root)
        if node.get("type") in ("BASE_TABLE", "SUBQUERY")
    } - {""}
    own = cte_names(tree)  # A WITH name vouches for nothing, whatever table it is called after.
    real = {
        table.lower(): {column.lower() for column in named}
        for table, named in columns.items()
        if table.lower() not in own
    }
    walker = _HeldValues({name.lower() for name in held}, rows, real)
    try:
        walker.collect_aliases(root)
        walker.query(root)
    except _Used:
        return True
    except (KeyError, TypeError, AttributeError):
        return True  # a shape nobody has seen
    return False


# -------------------------------------------------------------- what the rows are --


def _level_has_figures(node: dict, is_aggregate_name) -> bool:
    if node.get("group_expressions") or node.get("group_sets") or node.get("aggregate_handling") == "FORCE_AGGREGATES":
        return True

    def has_one(value) -> bool:
        if isinstance(value, list):
            return any(has_one(item) for item in value)
        if not isinstance(value, dict) or value.get("class") == "SUBQUERY" or value.get("class") == "WINDOW":
            return False
        if value.get("class") == "FUNCTION" and is_aggregate_name(str(value.get("function_name"))):
            return True
        return any(has_one(item) for item in value.values() if isinstance(item, (dict, list)))

    return has_one(node.get("select_list"))


def rows_are_groups(tree: Optional[dict], is_aggregate_name) -> bool:
    """Is each result row a group, by the outer SELECT or by what it reads?

    Rows with an aggregate only in a filter or a window are still one row per record.
    """
    if not tree or len(tree.get("statements") or []) != 1:
        return False
    ctes: Dict[str, dict] = {}

    def groups(node: dict) -> bool:
        for entry in (node.get("cte_map") or {}).get("map") or []:
            ctes[str(entry.get("key")).lower()] = entry["value"]["query"]["node"]
        if node.get("type") != "SELECT_NODE":
            return False
        if _level_has_figures(node, is_aggregate_name):
            return True
        source = node.get("from_table") or {}
        if source.get("type") == "SUBQUERY":
            return groups(source["subquery"]["node"])
        if source.get("type") == "BASE_TABLE":
            body = ctes.get(str(source.get("table_name")).lower())
            return groups(body) if body is not None else False
        return False

    try:
        return groups(tree["statements"][0]["node"])
    except (KeyError, TypeError, AttributeError):
        return False


def inner_cut(tree: Optional[dict], is_aggregate_name) -> bool:
    """A LIMIT or OFFSET in what the outer SELECT reads, over rows rather than over figures.

    `(SELECT x FROM t LIMIT 100)` read part of t. A top 5 after a GROUP BY, or a
    `= (SELECT ... LIMIT 1)` in a condition, dropped nothing the figures are about.
    """
    if not tree or len(tree.get("statements") or []) != 1:
        return False

    def cuts(node: dict) -> bool:
        if node.get("type") == "SET_OPERATION_NODE":
            return _cut_here(node) or level(node)
        return _cut_here(node) and not _level_has_figures(node, is_aggregate_name)

    def level(node: dict) -> bool:
        """Anything the statement at `node` reads that cut its rows."""
        for entry in (node.get("cte_map") or {}).get("map") or []:
            body = entry["value"]["query"]["node"]
            if cuts(body) or level(body):
                return True
        if node.get("type") == "SET_OPERATION_NODE":
            return any(cuts(node[side]) or level(node[side]) for side in ("left", "right"))
        return reads(node.get("from_table"))

    def reads(table) -> bool:
        if not isinstance(table, dict):
            return False
        if table.get("type") == "JOIN":
            return reads(table.get("left")) or reads(table.get("right"))
        if table.get("type") == "SUBQUERY":
            body = table["subquery"]["node"]
            return cuts(body) or level(body)
        return False

    try:
        return level(tree["statements"][0]["node"])
    except (KeyError, TypeError, AttributeError):
        return False


def _cut_here(node: dict) -> bool:
    return any(
        modifier.get("type") in ("LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER")
        for modifier in node.get("modifiers") or []
    )


def outer_cut(tree: Optional[dict], is_aggregate_name) -> bool:
    """The outer SELECT's own LIMIT or OFFSET, over rows rather than over figures."""
    if not tree or len(tree.get("statements") or []) != 1:
        return False
    root = tree["statements"][0]["node"]
    return _cut_here(root) and (root.get("type") != "SELECT_NODE" or not _level_has_figures(root, is_aggregate_name))


def plain_columns(tree: Optional[dict]) -> Optional[Set[str]]:
    """The result names that are bare columns of the one table read, lower case.

    None means the statement is no plain read of one table: a join, a subquery, or a
    computed value. A blank there says nothing about any field on the record.
    """
    if not tree or len(tree.get("statements") or []) != 1:
        return None
    root = tree["statements"][0]["node"]
    if root.get("type") != "SELECT_NODE" or (root.get("from_table") or {}).get("type") != "BASE_TABLE":
        return None
    if (root.get("cte_map") or {}).get("map"):
        return None
    found: Set[str] = set()
    for item in root.get("select_list") or []:
        if item.get("class") == "COLUMN_REF":
            found.add(str(item.get("alias") or _column(item)).lower())
        elif item.get("class") == "STAR":
            found.add("*")
    return found
