from __future__ import annotations

import collections
import contextlib
import dataclasses
import enum
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import typing

import numpy
import psycopg2.sql as sql

from .collection.set import IntSet
from .concurrent.work import Worker, WorkerRunner
from .formats.dump import (
    DUMP_DATA_JSON_FORMAT,
    DumpReferenceDirection,
    DumpRoot,
    DumpSchema,
)
from .formats.manifest import (
    MANIFEST_DATA_JSON_FORMAT,
    Manifest,
    ManifestTable,
    ManifestTableSegment,
)
from .log import TRACE
from .pg import Tid, export_snapshot, freeze_transaction, tid_to_int, transaction
from .resource import ResourceFactory
from .slice import SliceWriter
from .sql import SqlWriter


class OutputType(enum.Enum):
    SQL = enum.auto()
    SLICE = enum.auto()


@dataclasses.dataclass
class DumpIo:
    conn: ResourceFactory
    schema_file: ResourceFactory[typing.TextIO]
    output: ResourceFactory[typing.BinaryIO]


@dataclasses.dataclass
class DumpParams:
    include_schema: bool
    parallelism: int
    output_type: OutputType


def dump(
    root_configs: typing.List[DumpRoot],
    io: DumpIo,
    params: DumpParams,
):
    """
    Dump
    """
    if params.output_type == OutputType.SLICE and params.include_schema:
        raise Exception("--output-type=slice is incompatable with --include-schema")

    dump_config = DUMP_DATA_JSON_FORMAT.load(io.schema_file)
    schema = Schema(dump_config)
    roots = []
    for root_config in root_configs:
        try:
            table = schema.get_table(root_config.table)
        except KeyError:
            raise Exception(f"Root table {root_config.table} does not exist")
        roots.append(Root(table=table, condition=sql.SQL(root_config.condition)))

    with io.output() as file, contextlib.ExitStack() as stack:
        if params.output_type == OutputType.SLICE:
            slice_writer = stack.enter_context(SliceWriter(file))
            output = _SliceOutput(slice_writer)
        elif params.output_type == OutputType.SQL:
            sql_writer = SqlWriter(file)
            if params.include_schema:
                with sql_writer.open_predata() as f:
                    _pg_dump_section("pre-data", f)
            output = _SqlOutput(sql_writer)

        result = _DiscoveryResult()

        with io.conn() as conn, transaction(conn) as cur:
            if params.parallelism == 1:
                freeze_transaction(cur)

                def pg_manager():
                    return contextlib.nullcontext(cur)

            else:
                snapshot = export_snapshot(cur)
                logging.info("Running at snapshot %s", snapshot)

                @contextlib.contextmanager
                def pg_manager():
                    with io.conn() as conn, transaction(conn) as cur:
                        freeze_transaction(cur, snapshot=snapshot)
                        yield cur

            _dump_rows(roots, params.parallelism, pg_manager, result, output)

        if params.output_type == OutputType.SLICE:
            manifest = Manifest(tables=result.table_manifests())
            MANIFEST_DATA_JSON_FORMAT.dump(slice_writer.open_manifest, manifest)
        elif params.output_type == OutputType.SQL:
            if params.include_schema:
                with sql_writer.open_postdata() as f:
                    _pg_dump_section("post-data", f)


def _pg_dump_section(section: str, out: typing.BinaryIO) -> str:
    logging.log(TRACE, "Dumping %s schema", section)
    start = time.perf_counter()
    subprocess.check_call(
        ["pg_dump", "-B", "--no-acl", "--section", section],
        stdin=subprocess.DEVNULL,
        stdout=out,
    )
    end = time.perf_counter()
    logging.debug("Dumped %s schema (%.3fs)", section, end - start)


def _dump_rows(
    roots: typing.List[Root],
    parallelism: int,
    cur_resource: ResourceFactory,
    result,
    output: typing.Union[_SliceOutput, _SqlOutput],
):
    """
    Dump rows
    """

    logging.info("Dumping rows")
    start = time.perf_counter()
    worker = _Dump(result, output)
    runner = WorkerRunner(parallelism, worker.process_item, cur_resource)
    runner.run(
        [_RootItem(table=root.table, condition=root.condition) for root in roots]
    )
    end = time.perf_counter()
    logging.info("Dumped %d rows (%.3fs)", result.row_count, end - start)


class _SliceOutput:
    """
    Thread-safe slice output
    """

    def __init__(self, writer: SliceWriter):
        self._lock = threading.Lock()
        self._writer = writer

    @contextlib.contextmanager
    def open_segment(self, segment: _TableSegment):
        """
        Open segment for writing
        """
        with self._lock, self._writer.open_segment(
            segment.table.id, segment.index
        ) as f:
            yield f


class _SqlOutput:
    """
    Thread-safe SQL output
    """

    def __init__(self, writer: SqlWriter):
        self._lock = threading.Lock()
        self._writer = writer

    @contextlib.contextmanager
    def open_segment(self, segment: _TableSegment):
        with self._lock, self._writer.open_data(
            segment.table.id,
            segment.index,
            segment.table.schema,
            segment.table.name,
            segment.table.columns,
        ) as f:
            yield f


class _DiscoveryResult:
    """
    Discovered IDs
    """

    _row_ids: typing.DefaultDict[str, IntSet]
    _table_manifests: typing.Dict[str, ManifestTable]

    def __init__(self):
        self._id_count = 0
        self._row_ids = collections.defaultdict(lambda: IntSet(numpy.int64))
        self._table_manifests = {}
        self._lock = threading.Lock()

    def add(
        self, table: Table, row_ids: typing.List[Tid]
    ) -> typing.Optional[_TableSegment]:
        """
        Add IDs and return list of newly added segment
        """
        with self._lock:
            existing_ids = self._row_ids[table.id]
            ints = [tid_to_int(id) for id in row_ids]
            contains = existing_ids.contains(ints)
            new_ids = [id for id, c in zip(row_ids, contains) if not c]

            if not new_ids:
                return

            existing_ids.add([int for int, c in zip(ints, contains) if not c])
            self._id_count += len(new_ids)

            if table.id not in self._table_manifests:
                self._table_manifests[table.id] = ManifestTable(
                    columns=table.columns,
                    id=table.id,
                    name=table.name,
                    schema=table.schema,
                    segments=[],
                )
            table_manifest = self._table_manifests[table.id]

            segment = _TableSegment(
                table=table, row_ids=new_ids, index=len(table_manifest.segments)
            )
            table_manifest.segments.append(ManifestTableSegment(row_count=len(new_ids)))

        return segment

    @property
    def row_count(self):
        """
        Total rows
        """
        return self._id_count

    def table_manifests(self):
        """
        Iterable of ManifestTables
        """
        return self._table_manifests.values()


@dataclasses.dataclass
class _RootItem:
    table: Table
    """Table"""
    condition: sql.SQL
    """Condition"""


@dataclasses.dataclass
class _ReferenceItem:
    direction: DumpReferenceDirection
    """Direction"""
    reference: Reference
    """Reference"""
    segment: _TableSegment
    """Source segment"""


class _Dump:
    def __init__(self, result: _DiscoveryResult, output: _SliceOutput):
        self._result = result
        self._output = output

    def process_item(self, item: typing.Union[_RootItem, _ReferenceItem], cur):
        """
        Process item
        """
        if isinstance(item, _RootItem):
            segment = _discover_table_condition(
                cur, item.table, item.condition, self._result
            )
            if segment is None:
                return
            to_table = item.table

            yield from self._table_items(segment)
        elif isinstance(item, _ReferenceItem):
            segment = _discover_reference(
                cur, item.reference, item.direction, item.segment, self._result
            )
            if segment is None:
                return

            if item.direction == DumpReferenceDirection.FORWARD:
                to_table = item.reference.reference_table
            elif item.direction == DumpReferenceDirection.REVERSE:
                to_table = item.reference.table

            yield from self._table_items(segment, reference_item=item)

        with tempfile.TemporaryFile() as tmp:
            _dump_data(cur, to_table, segment.row_ids, tmp)
            tmp.seek(0)
            with self._output.open_segment(segment) as f:
                shutil.copyfileobj(tmp, f)

    def _table_items(
        self,
        segment: _TableSegment,
        reference_item: _ReferenceItem = None,
    ):
        """
        Create items for table
        """
        for reference in segment.table.references:
            if DumpReferenceDirection.FORWARD not in reference.directions:
                continue
            if (
                reference_item is not None
                and reference is reference_item.reference
                and reference_item.direction == DumpReferenceDirection.REVERSE
            ):
                continue
            yield _ReferenceItem(
                segment=segment,
                reference=reference,
                direction=DumpReferenceDirection.FORWARD,
            )
        for reference in segment.table.reverse_references:
            if DumpReferenceDirection.REVERSE not in reference.directions:
                continue
            if (
                reference_item is not None
                and reference is reference_item.reference
                and reference_item.direction == DumpReferenceDirection.FORWARD
            ):
                continue
            yield _ReferenceItem(
                segment=segment,
                reference=reference,
                direction=DumpReferenceDirection.REVERSE,
            )


@dataclasses.dataclass
class Root:
    """Root"""

    table: Table
    """Table"""
    condition: sql.SQL
    """Condition"""


@dataclasses.dataclass
class Table:
    """Table"""

    id: str
    """ID"""
    name: str
    """Name"""
    schema: str
    """Schema"""
    columns: typing.List[str]
    """Columns"""
    references: typing.List[Reference]
    """References to parent tables"""
    reverse_references: typing.List[Reference]
    """References to child tables"""


@dataclasses.dataclass
class Reference:
    """Reference"""

    directions: typing.List[DumpReferenceDirection]
    """Directions"""
    id: str
    """ID"""
    table: Table
    """Table"""
    columns: typing.List[str]
    """Columns"""
    reference_table: Table
    """Reference columns"""
    reference_columns: typing.List[str]


@dataclasses.dataclass
class _TableSegment:
    index: int
    row_ids: typing.List[Tid]
    table: Table


class Schema:
    """
    Graph model of schema
    """

    def __init__(self, schema: DumpSchema):
        self._tables = {}
        for table_config in schema.tables:
            table = Table(
                columns=table_config.columns,
                references=[],
                id=table_config.id,
                name=table_config.name,
                reverse_references=[],
                schema=table_config.schema,
            )
            if table.id in self._tables:
                raise Exception(f"Multiple definitions for table {table.id}")
            self._tables[table.id] = table

        self._references = {}
        for reference_config in schema.references:
            try:
                table = self._tables[reference_config.table]
            except KeyError:
                raise Exception(
                    f"No table {reference_config.table}, needed by reference {reference_config.id}"
                )
            try:
                reference_table = self._tables[reference_config.reference_table]
            except KeyError:
                raise Exception(
                    f"No table {reference_config.reference_table}, needed by reference {reference_config.id}"
                )

            reference = Reference(
                directions=reference_config.directions,
                id=reference_config.id,
                table=table,
                columns=reference_config.columns,
                reference_table=reference_table,
                reference_columns=reference_config.reference_columns,
            )
            if reference.id in self._references:
                raise Exception(f"Multiple definitions for reference {reference.id}")
            self._references[reference.id] = reference
            table.references.append(reference)
            reference_table.reverse_references.append(reference)

    def get_table(self, id) -> Table:
        """
        Get table by ID
        """
        return self._tables[id]

    def tables(self):
        """
        Iterable of tables
        """
        return self._tables.values()


def _dump_data(cur, table: Table, ids: typing.List[Tid], out):
    """
    Dump data
    """

    logging.log(TRACE, f"Dumping %s rows from table %s", len(ids), table.id)
    start = time.perf_counter()
    query = sql.SQL(
        """
            COPY (
                SELECT {}
                FROM {}
                WHERE ctid = ANY({}::tid[])
            )
            TO STDOUT
        """
    ).format(
        sql.SQL(", ").join([sql.Identifier(column) for column in table.columns]),
        sql.Identifier(table.schema, table.name),
        sql.Literal(ids),
    )
    cur.copy_expert(query, out, size=1024 * 32)
    end = time.perf_counter()
    logging.debug(
        f"Dumped %s rows from table %s (%.3fs)", len(ids), table.id, end - start
    )


def _discover_table_condition(
    cur, table: Table, condition: sql.SQL, result: _DiscoveryResult
) -> typing.List[Tid]:
    """
    Discover, using root
    """
    logging.log(TRACE, f"Finding rows from table %s", table.id)
    start = time.perf_counter()
    query = sql.SQL(
        """
            SELECT ctid
            FROM {}
            WHERE {}
        """
    ).format(
        sql.Identifier(table.schema, table.name),
        condition,
    )
    cur.execute(query)
    found_ids = [id_ for id_, in cur.fetchall()]
    segment = result.add(table, found_ids) if found_ids else None
    end = time.perf_counter()
    if segment is None:
        logging.debug(
            f"Found %s rows (no new) in table %s (%.3fs)",
            len(found_ids),
            table.id,
            end - start,
        )
    else:
        logging.debug(
            f"Found %s rows (%s new) as %s/%s (%.3fs)",
            len(found_ids),
            len(segment.row_ids),
            segment.table.id,
            segment.index,
            end - start,
        )
    end = time.perf_counter()
    return segment


def _discover_reference(
    cur,
    reference: Reference,
    direction: DumpReferenceDirection,
    segment: _TableSegment,
    result,
) -> typing.List[Tid]:
    """
    Discover, using reference
    """
    if direction == DumpReferenceDirection.FORWARD:
        from_columns = reference.columns
        from_table = reference.table
        to_columns = reference.reference_columns
        to_table = reference.reference_table
    elif direction == DumpReferenceDirection.REVERSE:
        from_columns = reference.reference_columns
        from_table = reference.reference_table
        to_columns = reference.columns
        to_table = reference.table

    logging.log(
        TRACE,
        f"Finding rows from table %s using %s/%s via %s",
        to_table.id,
        segment.table.id,
        segment.index,
        reference.id,
    )
    start = time.perf_counter()
    query = sql.SQL(
        """
            SELECT DISTINCT b.ctid
            FROM {} AS a
                JOIN {} AS b ON ({}) = ({})
            WHERE a.ctid = ANY(%s::tid[])
        """
    ).format(
        sql.Identifier(from_table.schema, from_table.name),
        sql.Identifier(to_table.schema, to_table.name),
        sql.SQL(", ").join([sql.Identifier("a", name) for name in from_columns]),
        sql.SQL(", ").join([sql.Identifier("b", name) for name in to_columns]),
    )
    cur.execute(query, [segment.row_ids])
    found_ids = [id_ for id_, in cur.fetchall()]

    if "account_id" in to_table.columns:
        query = sql.SQL(
            """
            SELECT account_id
            FROM {}
            WHERE ctid = ANY(%s::tid[]) AND account_id <> 3439
        """
        ).format(
            sql.Identifier(to_table.schema, to_table.name),
        )
        cur.execute(query, [found_ids])
        x = cur.fetchall()
        if x:
            raise Exception(f"{to_table.id} Account {x[0][0]}")

    new_segment = result.add(to_table, found_ids) if found_ids else None
    end = time.perf_counter()
    if new_segment is None:
        logging.debug(
            f"Found %s rows (no new) in table %s using %s/%s via %s (%.3fs)",
            len(found_ids),
            to_table.id,
            segment.table.id,
            segment.index,
            reference.id,
            end - start,
        )
    else:
        logging.debug(
            f"Found %s rows (%s new) as %s/%s using %s/%s via %s (%.3fs)",
            len(found_ids),
            len(new_segment.row_ids),
            new_segment.table.id,
            new_segment.index,
            segment.table.id,
            segment.index,
            reference.id,
            end - start,
        )

    return new_segment
