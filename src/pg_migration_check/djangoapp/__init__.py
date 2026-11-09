"""Django integration: add "pg_migration_check.djangoapp" to INSTALLED_APPS, then

    python manage.py check_migrations --dsn postgresql://localhost/scratch

checks the migrations your database has not applied yet (or every one, with --all) against an empty scratch
Postgres, the same way the command line tool checks a SQL file.
"""
