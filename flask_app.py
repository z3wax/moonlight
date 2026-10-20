"""WSGI entry point for the admin panel (Flask).

PythonAnywhere: Web tab -> WSGI configuration file:

    import sys
    sys.path.insert(0, "/home/USERNAME/moonlight")
    from flask_app import application

The bot itself must run separately:  python worker.py  (Always-on task).
"""

from autoreply.web.flask_app import create_app

application = create_app()
app = application  # `flask --app flask_app run` for local testing
