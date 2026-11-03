# Profession templates

A profession template is one JSON file that tells the board what a "project"
is called, which work types exist, which stages they move through, which
reference numbers to look for in email, what vocabulary the triage rules and
prompts should know, and how the columns and tabs are laid out. Everything
domain-specific lives here; the Python code reads stage **roles**, never
stage names.

Shipped templates (`app/plannerboard/professions/`): `general`,
`hvac_contractor`, `construction_gc`, `property_management`, `legal`,
`real_estate`, `insurance_agency`, `consulting`, `it_msp`, `recruiting`.
The wizard lists them from `GET /api/professions`; the active one, merged
with your overrides, is `GET /api/profession`.

## Schema

Top-level keys (all required except where noted; `profession.py:BASE` holds
the defaults a template inherits):

| key | type | meaning |
|---|---|---|
| `id` | string | file stem, `[a-z0-9_]` |
| `label`, `description`, `icon` | string | wizard card |
| `entity` | object | nouns: `project`, `projects`, `client`, `location`, `contact`, `quote`. Used everywhere in the UI and in prompts (`{entity_project}`, `{entity_client}`) |
| `work_types` | array | `{id, label, short, color, workflow_id}`. The pill on a card; `workflow_id` picks the stage roster |
| `fields` | array | `{key, label, type, show_on[], searchable, group, source}`. `type`: `text`, `money`, `date`, `number`. `show_on`: any of `card`, `drawer`, `list`. `source`: `key_details` (free JSON on the project) or `column` (a real column such as `quote_amount`, `approved_amount`, `quote_sent_date`, `won_date`) |
| `id_patterns` | array | `{key, label, regex, unique_match}`. Regexes run over subject and body; group 1 (or the whole match) is the value. A hit on a value already stored under `key_details.<key>` on exactly one project is a unique match and files the email automatically when `unique_match` is true |
| `date_types` | array | `{id, label}`. Built-ins `follow_up`, `deadline`, `sla`, `quote`; add your own and reference them from `owning_task.date_type` |
| `teams` | array | optional grouping for contacts |
| `business_hours` | object | `{tz, start, end, days[]}`; `days` are Monday=0. Drives SLA clocks and "business days until" on the board |
| `sla_hours` | number | default SLA for `todo` stages without their own |
| `money` | object | `{currency, annual_goal}`; hidden entirely when `features.money` is false |
| `triage` | object | `classifications[]` (the labels the classifier may return; the first ten are the built-in set), `notification_senders[]` (prefixes treated as machine mail), `internal_admin_subjects[]` (subjects routed to the internal class) |
| `vocab` | object | `keywords[]`, `acronyms[]`, `scope_phrases[]`: fed to the keyword index and to the prompts as `{vocab_keywords}` / `{acronyms}` |
| `prompt_profile` | object | `{industry, role_noun}`: rendered into every prompt's system text |
| `customer_visible_fields` | array | optional; which project fields a drafted reply may mention. Default `name, location, scope, contacts` |
| `features` | object | optional defaults for `money`, `game_layer`, `analytics` |
| `workflows` | object | the stage roster document (below) |

### The `workflows` block

```json
"workflows": {
  "workflows": [
    {
      "id": "quote", "label": "Quote Request", "icon": "...", "color": "#f0b429",
      "entry_stage": "quote_needed",
      "won_transition": {"from": "quote_sent", "to": "job_setup"},
      "extra_tasks": [],
      "stages": [
        {"id": "quote_needed", "label": "Quote Needed", "role": "todo", "column": true,
         "owning_task": {"text": "Prepare and send proposal", "date_type": "quote"},
         "advance_on_task_complete": "quote_sent"},
        {"id": "quote_sent", "label": "Quote Sent", "role": "out", "column": true,
         "owning_task": {"text": "Follow up with customer", "date_type": "follow_up", "days": 5},
         "auto_arm": false},
        {"id": "job_setup", "label": "Needs Setup", "role": "won", "column": true,
         "owning_task": {"text": "Set up job", "date_type": "sla", "sla_hours": 4, "category": "setup"},
         "advance_on_task_complete": "parts_needed"},
        {"id": "complete", "label": "Closed", "role": "closed", "column": false, "closed": true},
        {"id": "ignored", "label": "Ignored", "role": "ignored", "column": false, "closed": true}
      ]
    }
  ],
  "tabs": [
    {"id": "active", "label": "Active", "filter": {"closed": false}, "urgency": "sum"},
    {"id": "deadlines", "label": "Deadlines", "filter": {"subtask_date_type": "deadline", "pending": true}, "urgency": "deadline"}
  ],
  "tab_rules": {"auto_stage_tabs": true},
  "sort": {"mode": "subtask", "priority": [
    {"id": "deadline", "label": "Deadline", "match": {"date_type": "deadline"}},
    {"id": "sla", "label": "SLA", "match": {"date_type": "sla"}},
    {"id": "follow_up", "label": "Follow-up", "match": {"date_type": "follow_up"}},
    {"id": "other", "label": "Everything else", "match": {}}]},
  "hud": {"hp": "threads_closed_vs_goal", "mp": "tasks_done_vs_goal", "xp": "threads_closed"},
  "theme": "default"
}
```

Stage keys:

| key | meaning |
|---|---|
| `id`, `label` | `[a-z0-9_]` id, display label |
| `role` | one of the eight roles below; the only thing code checks |
| `column` | show as a kanban column |
| `closed` | counts as finished (reports, revive rules) |
| `owning_task` | the one task this stage is about: `{text, date_type, days?, sla_hours?, category?}`. `days` sets a follow-up date N business days out; `sla_hours` a clock from arrival |
| `advance_on_task_complete` | when the owning task is completed the project moves to this stage and that stage's owning task is armed. Chains of these form the ladder |
| `auto_arm` | `false` stops the owning task from being created automatically on entry (the user arms it) |

`entry_stage` is where a new project of this workflow starts.
`won_transition` says which stage a win (`won_date` set) moves the project
from and to; clearing `won_date` reverts and drops the ladder tasks.
`extra_tasks` are additional tasks armed alongside the ladder.

After first run the block is copied to `<data_root>/workflows.json`, which
the Settings > Workflows editor writes. Template changes to `workflows` do
not overwrite that file; delete it (or edit in Settings) to re-seed.

## Stage roles

Every literal status check in the app was replaced by a role check. A
template may name stages anything, but each needs one of these roles:

| role | meaning | board behavior |
|---|---|---|
| `todo` | work the user must do now | counts toward "action" stats; SLA clock from `sla_hours`; new email on a closed project revives into the entry stage |
| `waiting` | ball is in someone else's court | follow-up timers; no SLA pressure |
| `out` | a proposal, quote, offer or engagement letter has gone to the client | the "quote sent" cascade: setting the owning task complete records `quote_sent_date`; eligible for `won_transition.from` |
| `won` | the client said yes | `won_transition.to` target; setting `won_date` moves the project here and arms this stage's owning task, which starts the ladder |
| `in_progress` | delivery work after the win | ladder rungs; each `advance_on_task_complete` hop re-arms the next owning task |
| `closed` | finished | hidden from columns, counted in throughput, revivable by fresh mail |
| `cold` | went quiet, kept for reference | hidden, revivable |
| `ignored` | deliberately dropped | hidden, never revived, excluded from analytics |

`closed` and `ignored` are the closed roles; `closed` and `cold` revive when a
new email arrives on the thread; `ignored` never does.

## Writing a custom template

1. Copy the closest shipped file (`general.json` for a simple to-do board,
   `hvac_contractor.json` for a sold-work ladder, `legal.json` for
   waiting-heavy matters) and change `id`, `label`, `description`.
2. Rename the nouns in `entity`. Keep `projects` plural.
3. Define `work_types`. Each must point at a `workflow_id` that exists in
   `workflows.workflows`; the validator flags a dangling reference.
4. Write `id_patterns` for the reference numbers your email actually carries.
   Test the regex against three real subjects. Use a capture group for the
   value and `\b` anchors so "PO 44512" does not match a phone number.
5. Define the workflows. Rules of thumb: one `todo` entry stage, at most one
   `out` stage per workflow, exactly one `closed` and one `ignored`, and a
   `won_transition` only if a yes/no moment exists. Keep ladders short; every
   rung is a task the user must tick.
6. Fill `triage.classifications` with the built-in ten plus your domain
   classes; the classifier prompt whitelists these labels. Add
   `vocab.keywords` your clients use; they weight the keyword matcher.
7. Set `business_hours.tz` to an IANA zone name.
8. Validate: `python -c "import json,plannerboard.profession as p; print(p.validate_template(json.load(open('my.json'))))"`
   prints `[]` when the structure is right. Then `python -m pytest -q
   tests/test_profession.py` still passes.
9. Install it. Two options:
   - **Overrides** (no files): `PUT /api/profession` with the parts you want
     changed; they deep-merge over the chosen template and are stored in
     `plannerboard.json` under `profession.overrides`. Good for renaming
     nouns, adding a field or an id pattern.
   - **A new template file**: drop `my_profession.json` into the
     `professions/` folder of the install (`plannerboard/professions/` in a
     source or pip install; `_internal/plannerboard/professions/` in a
     bundled build) and select it in the wizard or set
     `profession.template` in `plannerboard.json`. A packaged build replaces
     that folder on upgrade, so keep the source file somewhere else too.

Em dashes are not allowed in labels or descriptions (house rule for UI text).

## Examples from shipped templates

### `hvac_contractor`: a sold-work ladder

- Entity: Job / Jobs, Customer, Site, Contact, Proposal.
- Work types: Repair / Replacement, Controls, Maintenance Agreement, Budget,
  Bid / RFP (all on the `quote` workflow), Service Call (`service_call`),
  General Task (`general`).
- Id patterns: `work_order` (`WO 123456`, `W.O. #123456`, `Work Order:
  123456`) and `po_number` (`PO 778899`). Both `unique_match`, so a reply
  quoting a known WO files itself.
- `quote` workflow: `quote_needed` (todo, owning task "Prepare and send
  proposal", date type `quote`) -> `quote_sent` (out, "Follow up with
  customer" in 5 business days, `auto_arm: false`) -> win ->
  `job_setup` (won, "Set up job", 4 h SLA) -> `parts_needed` -> `awaiting_parts`
  -> `ready_to_schedule` -> `scheduled` (all `in_progress`, each rung armed
  by the previous task's completion) -> `complete`. `cold` and `ignored` on
  the side.
- Fields on the card: WO #, PO #, Quoted; drawer adds Equipment, Capacity,
  Parts PO, Approved, Sent on, Won on.

### `legal`: waiting-heavy matters, no ladder

- Entity: Matter / Matters, Client, Jurisdiction, Contact, Engagement letter.
- Id patterns: `matter_no` (`Matter 2024-118`, `File #3301`) and `case_no`
  (`Case No. 2:24-cv-01234`, `Docket ...`).
- Extra triage classes: `court_notice`, `opposing_counsel`,
  `client_document`, `filing_deadline`, `billing`.
- `matter` workflow: `intake` (todo) -> `engagement_sent` (out) -> win ->
  `active` (won) with `awaiting_client` and `awaiting_court` as `waiting`
  stages the user moves into by hand; `cold`, `complete`, `ignored`. No
  `advance_on_task_complete` chain: legal work does not march through fixed
  rungs, so follow-up timers and deadline tasks carry the urgency instead.
- Vocabulary: motion, discovery, deposition, hearing, retainer, opposing
  counsel; acronyms NDA, LOI, MSA, ESI, CLE.

### `recruiting`: two parallel workflows

Shows a template with two first-class workflows: `requisition` (intake ->
sourcing -> interviewing -> offer_out (out) -> accepted (won)) and
`candidate` (new -> screened -> submitted (out) -> placed (won)), each with
its own `won_transition`, plus the stock `general` workflow for odd tasks.
Entity nouns: Search, Hiring Manager, Department, Candidate, Offer.
