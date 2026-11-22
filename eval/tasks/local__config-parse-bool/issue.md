# DEBUG=0 turns debug mode on

With `DEBUG=0` (or `DEBUG=false`) in the environment, the app starts in debug mode.
`parse_bool("0")` and `parse_bool("false")` both return `True`.

Every word in `FALSE_WORDS` should parse as `False`.
