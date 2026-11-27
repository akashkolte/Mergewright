Add array value parsing to CodeVar
(See also #6424) Currently when CodeVar's `__eval_python_stack()` analyzes an array value (e.g. `[1, 2, 3]`), it tokenizes the corresponding string into undefined symbols (`["[1", "2", "3]"]`). Instead each array element should be evaluated recursively.  This would cover nested arrays and arrays containing expressions (`["x + y", ...]`), but not expressions _referencing_ arrays (so "x + array[0]" would still fail) (would be nice in the future). A test should be added to cover a variety of useful examples.

#5807 will make use of this
