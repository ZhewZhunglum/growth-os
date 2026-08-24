from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    response = JsonResponse(
        {
            "status": "ok",
            "database": "ok",
            "deployment": {
                "stage": settings.DEPLOYMENT_STAGE,
                "revision": settings.RELEASE_SHA,
            },
        }
    )
    # Health responses are operational evidence, not cacheable application
    # content.  They deliberately expose no host, database, secret, or user data.
    response["Cache-Control"] = "no-store"
    return response

