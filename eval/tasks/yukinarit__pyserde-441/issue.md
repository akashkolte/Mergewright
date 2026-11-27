serde.compat.SerdeError: Can not serialize with untagged union of dict[str, Any]
`0.12.3` on Python 3.11.4:

```py
from dataclasses import dataclass
from typing import Any

from serde import Untagged, field, serde
from serde.yaml import from_yaml, to_yaml


@dataclass
class FooWithString:
    foo: str


@dataclass
class BarWithDict:
    bar: dict[str, Any]  # remove [str, Any] to fix


@serde(tagging=Untagged)
@dataclass
class Klass:
    foobars: list[FooWithString | BarWithDict]


good_yaml = """
foobars:
  - foo: string
  - bar:
      key: value
"""

if __name__ == "__main__":
    config = from_yaml(Klass, good_yaml)
    print(to_yaml(config))
```

```
serde.compat.SerdeError: Can not serialize BarWithDict(bar={'key': 'value'}) of type BarWithDict for Union[FooWithString, BarWithDict]
```

---

Weirdly the more trivial case `list[BarWithDict]` works fine, as does just `BarWithDict`.
It's something specifically caused by it being in an untagged union list 🤔 
