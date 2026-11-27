# slugify() leaves repeated dashes in the slug

`slugify("Hello,  World -- again")` returns `"hello---world-----again"`. A run of spaces or
punctuation between two words should become a single separator: `"hello-world-again"`.

Simple titles such as `"Hello World"` are fine.
