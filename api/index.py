import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

# NOTE: on Vercel, each request may hit a fresh, isolated function instance.
# create_app() starts a background worker thread and opens a SQLite file --
# neither persists across invocations here, so the submission queue and
# archive history will NOT reliably work the way they do when run locally
# or on a persistent host (see render.yaml for the option that does).
app = create_app()
