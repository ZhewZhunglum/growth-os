#!/bin/sh
set -eu

attempt=1
max_attempts="${DATABASE_WAIT_ATTEMPTS:-30}"
wait_seconds="${DATABASE_WAIT_INTERVAL_SECONDS:-2}"

until python -c "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'growth_os.settings'); import django; django.setup(); from django.db import connections; cursor = connections['default'].cursor(); cursor.execute('SELECT 1'); cursor.close()" 2>/dev/null
do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "Database was not ready after $attempt attempts; startup is stopping." >&2
        python -c "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'growth_os.settings'); import django; django.setup(); from django.db import connections; connections['default'].cursor().execute('SELECT 1')"
        exit 1
    fi
    echo "Database is not ready (attempt $attempt/$max_attempts); retrying in ${wait_seconds}s."
    attempt=$((attempt + 1))
    sleep "$wait_seconds"
done

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
