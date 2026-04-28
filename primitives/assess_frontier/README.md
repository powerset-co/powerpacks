# assess_frontier

Assess a direct result or merged slice frontier before hydration.

Inputs:

- candidate IDs or merged frontier
- counts
- slice yield and overlap, if slices were used
- decomposition context

Expected output:

- frontier size
- quality signal
- whether the frontier is too broad, too narrow, or coherent
- recommended next action
- reasons

This primitive should not run expensive scoring.
