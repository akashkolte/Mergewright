Expression `2>0` not parsed correctly
When I enter the expression `2>0` in a Xonsh shell, I would expect it to evaluate it as a boolean and print `True` - that is what Python does. Instead, Xonsh attempts to parse it as a subprocess command and gives an error:
```
xonsh.tools.XonshError: xonsh: subprocess mode: command not found: 2>
```

I'll try to work on a fix for this issue and make a PR.

## xonfig

```
+------------------+-------------------------+
| xonsh            | 0.13.3                  |
| Git SHA          | c63f75ef                |
| Commit Date      | Nov 14 13:22:10 2022    |
| Python           | 3.9.13                  |
| PLY              | 3.11                    |
| have readline    | True                    |
| prompt toolkit   | 3.0.29                  |
| shell type       | prompt_toolkit          |
| history backend  | sqlite                  |
| pygments         | 2.11.2                  |
| on posix         | True                    |
| on linux         | True                    |
| distro           | fedora                  |
| on wsl           | False                   |
| on darwin        | False                   |
| on windows       | False                   |
| on cygwin        | False                   |
| on msys2         | False                   |
| is superuser     | False                   |
| default encoding | utf-8                   |
| xonsh encoding   | utf-8                   |
| encoding errors  | surrogateescape         |
| xontrib          | []                      |
| RC file 1        | /home/yaxollum/.xonshrc |
+------------------+-------------------------+
```

## For community
⬇️  **Please click the 👍 reaction instead of leaving a `+1` or 👍  comment**
