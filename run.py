from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=False + use_reloader=False: important so the worker thread
    # (started once in create_app) isn't duplicated by Flask's reloader.
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
