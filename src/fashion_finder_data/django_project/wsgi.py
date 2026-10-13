import os

from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fashion_finder_data.django_project.settings")
# Without a fixed key every server process would generate its own, breaking sessions.
if (
    not os.environ.get("FASHION_FINDER_SECRET_KEY")
    and os.environ.get("FASHION_FINDER_DEBUG") != "1"
):
    raise ImproperlyConfigured("set FASHION_FINDER_SECRET_KEY before starting the web server")
application = get_wsgi_application()
