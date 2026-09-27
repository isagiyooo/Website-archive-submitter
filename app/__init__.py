from flask import Flask
from .database import init_db
from .worker import worker


def create_app():
    app = Flask(__name__)
    app.secret_key = "dev-secret-key-change-me"  # fine for a local assignment demo

    init_db()
    worker.start()  # recovers any interrupted queue items and starts polling

    from .routes import bp
    app.register_blueprint(bp)

    return app
