# Checking combinations of pin declarations

[日本語](pin-constraints-conformance.ja.md)

The role_channels and channel_group of core §7.4 act as constraints whichever way pins are chosen.
The role_channels of the same role are a union. A role included in a group must match that group exactly, and
a role not included in a group is chosen by role_channels alone. A role in both must satisfy both constraints.

`uv run pytest -q tests/test_virtual_bench_pin_constraints.py` checks the logical Endpoint.
The test profile's declarations are set before the Endpoint is created. It checks with plan_apply: I2C with group only / candidates only / intersection / one-side role,
and SPI with 2 roles fixed by group and 2 roles given by candidates (settings have no plan).
A valid setting is applied first, and the plan, settings and idle state kept on refusal are also observed.
The counterexamples are a mix of different groups, a pin in the group but not in the candidates, and a pin allowed only for another role.

It has setting cases and faults that bypass the constraint check. The expected accept/refuse is a table derived from the SPEC, not generated from the implementation's decision function.
It also checks that an I2C configuration that picks a role outside the group from candidates is not refused by mistake.
A group compares only the roles that the group lists. The same holds for a group that fixes all roles, and the role_channels candidates are also checked.

This check covers the selection constraints and invariance on refusal;
it does not prove conformance to every interface schema. All interfaces / all declaration shapes / all pin selection methods,
concurrency, physical wiring and electrical conditions are not checked. `full_conformance=false`.

A role not in the chosen group needs role_channels candidates for that role.
Using, for a role without a declaration, a pin that another group lists for another role is not allowed. This refusal is also checked.
