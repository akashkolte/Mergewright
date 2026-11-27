# median() returns the wrong value for an even number of values

`median([4, 1, 3, 2])` returns `3`. The median of an even-length list is the mean of the two middle values, so the expected result is `2.5`.

Odd-length lists work correctly: `median([3, 1, 2])` returns `2`.
