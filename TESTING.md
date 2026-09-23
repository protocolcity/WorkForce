# WorkForce verification

Install the checkout in an isolated Python environment, then run:

```sh
python -m pip install -e . pytest
WORKFORCE_NO_DESK=1 python -m pytest tests -q -rs
```

For an alternate interpreter without an editable install, use an **absolute**
checkout path in PYTHONPATH so fake-provider subprocesses retain the candidate
import after changing working directory. A relative `.` is insufficient.

Tests clear inherited host roster/store/identity context, disable live desk
notifications and use disposable directories. Provider calls are deterministic
fake commands or fixtures; the normal suite does not purchase inference.
Live provider qualification is a separate bounded operator action.

| Boundary | Regression evidence |
|---|---|
| Dispatch, budget, empty/gated queue, lock refusal | test_engine.py, test_capacity.py, test_capacity_policy.py |
| Prepared checkout, interrupted run and explicit recovery | test_task_runner.py, test_engine_recovery.py |
| Authentication/tool adapter and generated contracts | test_adapters.py, test_hire.py |
| Qualification versus execution evidence | test_provider_qualification.py |
| Supervisor proposal, scope and revalidation | test_supervisor.py, test_supervisor_api.py |
| Review, delivery and installed result boundaries | test_integrator.py |
| Selected runtime and restart preservation | test_runtimes.py |

CI runs the complete suite on Python 3.9 and 3.11. Platform-specific checks must
state why they skip; removed private-host paper/export tests are not part of the
public product contract. Keep clock-dependent fixtures pinned to an explicit
observation time. A failure is investigated; do not add retries to hide it.

Before release, inspect wheel/sdist contents, install outside the checkout,
verify supported CLI/API behavior and preserve runtime data across replacement.
After activation verify actual package/process identity. Passing source tests
alone does not establish a deployed worker or a successful work order.
