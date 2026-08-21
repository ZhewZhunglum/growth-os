from growth_os.settings import *  # noqa: F403,F401


INSTALLED_APPS = [*INSTALLED_APPS]  # noqa: F405
for app_name in ("insights", "intelligence"):
    if app_name not in INSTALLED_APPS:
        INSTALLED_APPS.append(app_name)
