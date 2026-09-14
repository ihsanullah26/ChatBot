

#!/usr/bin/env bash
# Loads 12-30-2025.sql (a MySQL dump) into the real Postgres database
# that watchman_chatbox_postgres.py connects to (via DB_CONFIG /
# psycopg2), using pgloader — the standard tool for MySQL -> Postgres
# migrations, and the only practical way to move a 200+MB dump like
# this one without hand-translating every CREATE TABLE statement.
#
# WHY NOT hand-write this in the chatbot session: this dump is ~218MB
# of real MySQL syntax (backtick identifiers, ENUM types, AUTO_INCREMENT,
# etc.) — pgloader handles the MySQL -> Postgres type/syntax conversion
# and streams the data in, which a one-off script cannot do reliably at
# this size.
#
# >>> MANUAL: run this on YOUR machine (not in any hosted chat
# session), where you have a real Postgres server the chatbot can
# reach, and where 12-30-2025.sql lives on disk.
#
# Usage:
#   chmod +x migrate_mysql_dump_to_postgres.sh
#   ./migrate_mysql_dump_to_postgres.sh /path/to/12-30-2025.sql mydatabase
#
set -e

DUMP_FILE="${1:?Usage: $0 /path/to/pp-code-db-0926.sql <postgres_db_name>}"
PG_DB="${2:?Usage: $0 /path/to/pp-code-db-0926.sql <postgres_db_name>}"
MYSQL_TMP_DB="watchman_migration_tmp"
MYSQL_USER="migrator"
MYSQL_PASS="ChangeThisPassword123!"          # >>> MANUAL: set your local MySQL root password
PG_USER="postgres"
PG_PASS="PASSWORD"             # >>> MANUAL: set your local Postgres password
PG_HOST="127.0.0.1"
PG_PORT="5432"

echo "== Step 1/3: loading the MySQL dump into a temporary local MySQL DB =="
echo "   (pgloader reads FROM a live MySQL server, not directly from a .sql file)"
mysql -u "$MYSQL_USER" ${MYSQL_PASS:+-p"$MYSQL_PASS"} -e "DROP DATABASE IF EXISTS $MYSQL_TMP_DB; CREATE DATABASE $MYSQL_TMP_DB;"
# The dump was produced by real MySQL 8 (DigitalOcean managed MySQL) and
# includes "SET @@GLOBAL.GTID_PURGED=...;" replication-bookkeeping
# statements. MariaDB (what `mysql`/`mariadb` on Fedora actually is)
# doesn't have that system variable at all and errors out on it —
# harmless to drop since we only need the data/schema, not replication
# state. Also strips the \r from the dump's Windows-style line endings
# so the line-range delete below matches cleanly. Streamed through a
# pipe rather than written to a second 250MB+ file on disk.
tr -d '\r' < "$DUMP_FILE" | sed "/^SET @@GLOBAL\.GTID_PURGED=/,/';\$/d" \
  | mysql -u "$MYSQL_USER" ${MYSQL_PASS:+-p"$MYSQL_PASS"} "$MYSQL_TMP_DB"

echo "== Step 2/3: making sure the target Postgres database exists =="
PGPASSWORD="$PG_PASS" createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_DB" 2>/dev/null || true

echo "== Step 3/3: running pgloader (MySQL -> Postgres) =="
pgloader \
  "mysql://${MYSQL_USER}:${MYSQL_PASS}@127.0.0.1/${MYSQL_TMP_DB}" \
  "postgresql://${PG_USER}:${PG_PASS}@${PG_HOST}:${PG_PORT}/${PG_DB}"

echo "== Done =="
echo "Now point DB_CONFIG in watchman_chatbox_postgres.py at:"
echo "  dbname=\"$PG_DB\", host=\"$PG_HOST\", port=$PG_PORT, user=\"$PG_USER\""
echo
echo "Sanity check a couple of tables:"
echo "  psql -h $PG_HOST -U $PG_USER -d $PG_DB -c \"SELECT count(*) FROM ppcode_usersregister;\""
echo "  psql -h $PG_HOST -U $PG_USER -d $PG_DB -c \"SELECT count(*) FROM watchrecord;\""