"""Blueprint registry. Every module exposes `bp`."""

from . import (ai_api, alerts, assistant_api, correspondence, drafts, files, game, health,
               license_api, mail_api, mail_client_api, projects, questions, setup, subtasks, triage, update_api,
               workflows_api)

ALL = (projects, subtasks, triage, correspondence, drafts, questions, alerts, health, workflows_api,
       setup, files, game, mail_api, mail_client_api, ai_api, assistant_api, license_api, update_api)


def register_blueprints(app):
    for mod in ALL:
        app.register_blueprint(mod.bp)
    return app
