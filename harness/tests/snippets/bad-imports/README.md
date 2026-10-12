# bad-imports (test only)

Harness test fixture: imports `subprocess`, which the snippet static check must reject. The loader itself
does not run that check.
