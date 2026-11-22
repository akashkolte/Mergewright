calc_rdm_movie's returned RDMs doesn't have its .dissimilarity_measure set
**Describe the bug**
`calc_rdm_movie` accepts a `method` parameter, which it uses for all the matrices in the `RDMs` object it returns, guaranteeing that they all use the same method. As such, it could (should) set the returned `RDMs.dissimilarity_measure` to be the `method`.

**To Reproduce**
(`data` as in the [Temporal RSA demo](https://rsatoolbox.readthedocs.io/en/stable/demo_temporal.html))
```python
rdms_data = calc_rdm_movie(data, method="correlation", descriptor="conditions")
print(rdms_data.dissimilarity_measure)  # None
```

**Expected behavior**
Prints `"correlation"`.

**Versions**
- the version of *rsatoolbox* you used
    - 0.1.3
- the version of python you used
    - 3.11.5
