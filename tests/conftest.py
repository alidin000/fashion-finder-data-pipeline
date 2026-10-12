"""Initialize Django before catalogue models are imported by test modules."""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fashion_finder_data.django_project.settings")
django.setup()
