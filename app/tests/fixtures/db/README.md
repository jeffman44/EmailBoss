# Board database fixtures

`1.2.sqlite` and `1.3.sqlite` are empty board databases built from `schema.sql` as it shipped in
1.2.0 and 1.3.2, with one project and one correspondence row. Upgrade tests open a data root built
on each and check that 2.0 starts on it without changing the board tables.

1.0 and 1.1 predate this repository's history; their fixtures are added when real copies are
available (plan Phase 6).
