from growth_os.settings import *  # noqa: F403


INSTALLED_APPS = [*INSTALLED_APPS, "workflow"]  # noqa: F405
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

