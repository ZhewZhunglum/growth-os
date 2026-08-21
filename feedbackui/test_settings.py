from growth_os.settings import *  # noqa: F403


if "feedbackui" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS = [*INSTALLED_APPS, "feedbackui"]  # noqa: F405
ROOT_URLCONF = "feedbackui.test_urls"
