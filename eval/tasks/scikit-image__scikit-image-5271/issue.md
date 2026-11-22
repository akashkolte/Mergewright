Question: Why is default structuring element of 1-connectivity used by ndimage.label for getting the default markers(labeled local minima) for watershed segmentation?
## Description

In the implemented watershed.py segmentation, one can pass a connectivity argument, when `local_minima` is called by watershed (when `markers=none`) it passes the same connectivity value onto the local minima function, this generates the `markers_bool`, which then is used by `ndimage.label()` to generate the labeled markers. `ndimage.label` can take a structure argument and it defaults to 1-connectivity structure if none are given. IMO it is more intuitive to have `ndimage.label` also use the same connectivity which is passed by the user to the watershed function instead of always using the default 1-connectivity.
Below is the line in the source I am referring to:
https://github.com/scikit-image/scikit-image/blob/663e9e4b0f2ba88249bfef45bc65700555a3f24b/skimage/segmentation/_watershed.py#L76

Instead of the above would it not be better to have?
```python
if connectivity == 2:
    markers = ndi.label(markers_bool, structure=[[1, 1, 1], [1, 1, 1], [1, 1, 1]])[0]
```
