"""Blueprint registry. Every module exposes `bp`."""

from . import (ai_api, alerts, assistant_api, correspondence, drafts, files, game, health,
               license_api, mail_api, ooo_api, mail_client_api, mail_terminal_api, projects, questions, rules_api, search_api, setup, subtasks, triage,
               update_api, workflows_api)

ALL = (projects, subtasks, triage, correspondence, drafts, questions, alerts, health, workflows_api,
       setup, files, game, mail_api, mail_client_api, mail_terminal_api, rules_api, search_api, ai_api, assistant_api, license_api, ooo_api, update_api)


def register_blueprints(app):
    for mod in ALL:
        app.register_blueprint(mod.bp)
    return app
