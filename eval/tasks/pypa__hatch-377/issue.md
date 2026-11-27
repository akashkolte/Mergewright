'hatch env show' does not list dependencies from extra-dependencies
I've got this in pyproject.toml:

```toml
[tool.hatch.envs.lint]
detached = true
dependencies = [
    "black",
    "flake8",
]

[tool.hatch.envs.lint.scripts]
lint = [
     "flake8 --ignore=E501,E402,E231",
     "black --check .",
     "bash -c 'shellcheck workflow-support/*.sh'",
]

[tool.hatch.envs.lint-action]
template = "lint"
extra-dependencies = [
    "lintly23",
    "markupsafe==2.0.1",
]

[tool.hatch.envs.lint-action.scripts]
lint = [
    "bash -c 'flake8 --ignore=E501,E402,E231 | lintly --format=flake8 --no-request-changes'",
    "bash -c 'black --check . 2>&1 >/dev/null | lintly --format=black --no-request-changes'",
    "bash -c 'shellcheck workflow-support/*.sh'",
]
```

But `hatch env show` reports this:

```
                    Standalone                    
┏━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Name        ┃ Type    ┃ Dependencies ┃ Scripts ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ default     │ virtual │              │         │
├─────────────┼─────────┼──────────────┼─────────┤
│ lint        │ virtual │ black        │ lint    │
│             │         │ flake8       │         │
├─────────────┼─────────┼──────────────┼─────────┤
│ lint-action │ virtual │ black        │ lint    │
│             │         │ flake8       │         │
└─────────────┴─────────┴──────────────┴─────────┘
```

The inherited dependencies from `lint` to `lint-action` are correctly displayed, but the additional dependencies in `lint-action` are not. They *are* correctly installed into the lint-action enviroment.