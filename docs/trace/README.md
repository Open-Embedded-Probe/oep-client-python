# Map from oep-spec rules to tests

[日本語](README.ja.md)

`tools/spec_units.py` takes the normative units (list items, numbered steps, data rows of tables) out of oep-spec's
core / transports / interface documents, section by section, and gives each an ID `<document>:<section>:<number>`
(`pre` before the first heading). `<document>.json` holds, per unit, the pytest IDs of the tests that check it (`verify`)
and of the fault-injection tests that catch a violation (`mutants`), or the reason for an exclusion (`excluded`:
informational rows, reference lists, electrical wires or target-side rules this repository cannot model, and so on).

```sh
.venv/bin/python tools/check_trace.py ../oep-spec          # all documents: check for gaps, drift, test presence and results
```

`check_trace.py` checks that every unit of the spec is in the map, that the start of the text matches, that every unit
that is not excluded has tests, and that the listed tests are collected and pass. If any of these is missing, it prints
PROBLEM and fails.
