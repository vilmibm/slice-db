# SliceDB

## Overview

SliceDB is a tool for capturing and restoring a subset of a PostgreSQL database.
It also supports scrubbing sensive data.

Keywords: Database subset, scrubbing, PostgreSQL

## Basic usage

First, query a database to create a schema file.

```sh
slicedb schema > schema.yml
```

Second, dump a slice:

```sh
slicedb dump --root public.example 'WHERE id IN (7, 56, 234)' --schema schema.yml > slice.zip
```

Third, restore that slice into another database:

```sh
slicedb restore < slice.zip
```

See [#Example](Example).

## Connection

Use the
[libpq environment variables](https://www.postgresql.org/docs/current/libpq-envars.html)
to configure the connection.

```sh
PGHOST=myhost slicedb schema > slice.yml
```

## Dump

### Output types

SliceDB can produce multiple formats:

- **slice** - ZIP archive of table data. This can be restored into an existing
  database with `slicedb restore`.
- **sql** - SQL file. This can be restored into an existing database with `psql`
  or another client, if triggers are disabled. It may include the schema, in
  which case it is restored into a new database.

### Schema

See formats/schema.yml for the JSONSchema of the schema file.

The `schema` command uses foreign keys to infer relationships between tables. It
is a suggested starting point.

You may want to prune the slice by removing relationships, or expand the slice
by adding relationships that don't have explicit foreign keys.

`slicedb schema-filter` can help modify the schema, or generic JSON tools like
`jq`.

### Algorithm

The slicing process works as follows:

1. Starting with the root table, query the physical IDs (ctid) of rows.

2. Add the row IDs to the existing list.

3. For new IDs, process each of the adjacent tables, using them as the current
   root.

4. Write out the manifest of tables as a ZIP entry.

5. For each table part, query the data, transforming it as necessary, and write
   it to a new ZIP entry.

Do this in parallel, using `pg_export_snapshot()` to guarantee a consistent
snapshot across workers.

## Transformation

_TODO_

Replacements are deterministic for a given pepper. By default, the pepper is
randomly geneated for a slice. You may specify it as `--pepper`. Note that
possession of the pepper makes the data guessable.

Transformation may operate an existing slice, or happen during the dump.

### Replacments

- `alphanumeric` - Replace alphanumeric characters, preserve the type and case
  of characters.
- `date_year` - Change date by up to one year.
- `geozip` - Replace zip code, preserving the first three digits.
- `given_name` - Replace given name.
- `person_name` - Replace name.
- `surname` - Replace surname.
- `composite` - Parse as a PostgreSQL composite, with suboptions.

### Replacement data

- Given names:
  [https://www.ssa.gov/cgi-bin/popularnames.cgi](https://www.ssa.gov/cgi-bin/popularnames.cgi)
- Surnames:
  [https://raw.githubusercontent.com/fivethirtyeight/data/master/most-common-name/surnames.csv](https://raw.githubusercontent.com/fivethirtyeight/data/master/most-common-name/surnames.csv)
- Zip codes:
  [https://simplemaps.com/data/us-zips](https://simplemaps.com/data/us-zips)

## Restore

SliceDB can restore slices into existing databases. In practice, this should
normally be an empty existing database.

### Cycles

Foreign keys may form a cycle only if at least one foreign key in the cycle is
deferrable.

That foreign key will be deferred during restore.

A restore may happen in a single transaction or not. Parallelism requires
multiple transactions.

## Not supported

- Multiple databases
- Databases other than PostgreSQL

## Example

<details>
<summary>Run PostgreSQL</summary>

```sh
docker run -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER="$USER" -p 5432:5432 --rm postgres
```

```sh
PGHOST=localhost createdb source

PGHOST=localhost PGDATABASE=source psql -c '
CREATE TABLE parent (
    id int PRIMARY KEY
);

CREATE TABLE child (
    id int PRIMARY KEY,
    parent_id int REFERENCES parent (id)
);

INSERT INTO parent (id)
VALUES (1), (2);

INSERT INTO child (id, parent_id)
VALUES (1, 1), (2, 1), (3, 2);
'

PGHOST=localhost createdb target

PGHOST=localhost PGDATABASE=target psql -c '
CREATE TABLE parent (
    id int PRIMARY KEY
);

CREATE TABLE child (
    id int PRIMARY KEY,
    parent_id int REFERENCES parent (id)
);
'
```

</details>

<details>
<summary>Dump a slice</summary>

```sh
PGHOST=localhost PGDATABASE=source slicedb schema > schema.json
PGHOST=localhost PGDATABASE=source slicedb dump --root public.parent 'id = 1' --schema schema.json > slice.zip
```

</details>

<details>
<summary>Restore a slice</summary>

```sh
PGHOST=localhost PGDATABASE=target slicedb restore < slice.zip
```

</details>
